"""PDF 附件解析器：提取表格和文本中的电表数据。

从 PDF 中提取的表格复用 ExcelParser 的智能 DataFrame 解析，
确保配对电表、列名消歧、asset_number 回退等逻辑统一。
"""

import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config_loader import config
from src.logger import log
from src.parsers.validators import validate_record


class PDFParser:
    """PDF 文件解析器，提取表格及文本信息。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}
        self._excel_parser = None

    def _get_excel_parser(self):
        """延迟导入 ExcelParser，复用其 DataFrame 智能解析。"""
        if self._excel_parser is None:
            from src.parsers.excel_parser import ExcelParser
            self._excel_parser = ExcelParser()
        return self._excel_parser

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """解析 PDF 文件，返回电表记录列表。"""
        filepath = Path(filepath)
        if not filepath.exists():
            log.error("PDF 文件不存在: %s", filepath)
            return []

        try:
            import pdfplumber
        except ImportError:
            log.error("pdfplumber 未安装，无法解析 PDF。请运行: pip install pdfplumber")
            return []

        results = []
        all_text_parts = []

        try:
            with pdfplumber.open(str(filepath)) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    # 提取表格
                    tables = page.extract_tables()
                    for table_idx, table in enumerate(tables):
                        records = self._parse_table(
                            table, filepath, f"page{page_num}_table{table_idx}", source_info
                        )
                        results.extend(records)

                    # 同时收集文本（即使有表格也收集，用于补充提取）
                    text = page.extract_text() or ""
                    if text.strip():
                        all_text_parts.append(text)

                    # 如果该页没有从表格提取到记录，尝试从文本提取
                    if not any(True for _ in tables):
                        records = self._parse_text(text, filepath, f"page{page_num}", source_info)
                        results.extend(records)

        except Exception as e:
            log.error("PDF 解析失败 [%s]: %s", filepath, e, exc_info=True)

        # 从全文中补充提取（项目名、用户编号等）
        if all_text_parts:
            full_text = "\n".join(all_text_parts)
            self._enrich_from_full_text(results, full_text, filepath, source_info)

        log.info("  PDF 解析完成: %s, 提取 %d 条记录", filepath.name, len(results))
        return results

    def _parse_table(self, table: list[list], filepath: Path,
                     source_sheet: str, source_info: dict) -> list[dict]:
        """解析 PDF 中提取的表格 — 复用 ExcelParser 的智能解析。"""
        if not table or len(table) < 2:
            return []

        # 构建 DataFrame（包含表头行，让 ExcelParser 自动检测）
        df = pd.DataFrame(table)

        # 委托给 ExcelParser 的智能解析
        parser = self._get_excel_parser()
        return parser._parse_dataframe_smart(df, source_sheet, filepath, source_info)

    def _parse_text(self, text: str, filepath: Path,
                    source_sheet: str, source_info: dict) -> list[dict]:
        """从纯文本中提取电表信息（正则匹配）。"""
        if not text or not text.strip():
            return []

        results = []

        # 提取用户编号
        user_id = self._extract_field(text, "user_id")
        # 提取电表号
        meter_number = self._extract_field(text, "meter_number")
        # 提取资产号
        asset_number = self._extract_field(text, "asset_number")

        # 如果没有电表号但有资产号，用资产号
        if not meter_number and asset_number:
            meter_number = asset_number

        if not meter_number and not user_id:
            return results

        reading_month = self._infer_month(source_info, filepath.name, source_sheet)
        project_name = self._extract_project(text, filepath.name, source_info)

        record = validate_record({
            "meter_number": meter_number,
            "asset_number": asset_number,
            "user_id": user_id,
            "meter_type": self._detect_type_from_text(text),
            "multiplier": self._extract_number(text, ["倍率", "CT倍率", "变比"]),
            "project_name": project_name,
            "reading_month": reading_month,
            "sharp_peak": self._extract_number(text, ["尖峰?", "正向尖"]),
            "peak": self._extract_number(text, ["(?<!尖)峰", "正向峰"]),
            "flat": self._extract_number(text, ["平段?", "正向平"]),
            "valley": self._extract_number(text, ["谷段?", "正向谷"]),
            "total_kwh": self._extract_number(text, ["总电量", "总用电", "合计"]),
            "unit_price": self._extract_number(text, ["单价", "电价"]),
            "discount": self._extract_discount(text),
            "source_file": filepath.name,
            "source_sheet": source_sheet,
        })
        if record:
            results.append(record)
        return results

    def _extract_field(self, text: str, field_name: str) -> Optional[str]:
        """从文本中按字段别名提取值。"""
        aliases = self.field_mapping.get(field_name, [])
        if not isinstance(aliases, list):
            return None
        for alias in aliases:
            match = re.search(rf'{re.escape(alias)}\s*[:：]?\s*(\S+)', text)
            if match:
                val = match.group(1).strip("，。、,.")
                if val and len(val) >= 3:
                    return val
        return None

    def _extract_number(self, text: str, keywords: list[str]) -> Optional[float]:
        """从文本中按关键词提取数值。"""
        for kw in keywords:
            match = re.search(rf'{kw}\s*[:：]?\s*(\d+\.?\d*)', text)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass
        return None

    def _extract_discount(self, text: str) -> Optional[float]:
        """提取折扣。"""
        m = re.search(r'(\d+\.?\d*)\s*折', text)
        if m:
            try:
                d = float(m.group(1))
                if 0 < d <= 10:
                    return d / 10.0 if d > 1 else d
            except ValueError:
                pass
        return None

    def _detect_type_from_text(self, text: str) -> str:
        """从文本中判断电表类型。"""
        for mtype, rule in self.meter_type_rules.items():
            keywords = rule.get("keywords", [])
            if any(kw in text for kw in keywords):
                if mtype == "grid_meter":
                    return "上网表"
                elif mtype == "generation_meter":
                    return "发电表"
        return "未知"

    def _extract_project(self, text: str, filename: str, source_info: dict) -> Optional[str]:
        """提取项目名（从文本内容优先，文件名兜底）。"""
        parser = self._get_excel_parser()
        proj = parser._extract_project_from_text(text)
        if proj:
            return proj
        proj = parser._extract_project_from_text(filename)
        if proj:
            return proj
        return None

    def _enrich_from_full_text(self, records: list[dict], full_text: str,
                                filepath: Path, source_info: dict):
        """用全文信息补充记录中缺失的字段。"""
        if not records:
            return

        # 从全文提取项目名
        project = self._extract_project(full_text, filepath.name, source_info)
        # 从全文提取用户编号
        user_id = self._extract_field(full_text, "user_id")
        # 从全文提取折扣
        discount = self._extract_discount(full_text)

        for rec in records:
            if not rec.get("project_name") and project:
                rec["project_name"] = project
            if not rec.get("user_id") and user_id:
                rec["user_id"] = user_id
            if not rec.get("discount") and discount:
                rec["discount"] = discount

    def _infer_month(self, source_info: dict, filename: str, context: str) -> str:
        """推断月份。"""
        # 从文件名和上下文提取
        for text in [filename, context]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2035 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"
