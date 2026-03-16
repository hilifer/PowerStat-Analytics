"""PDF 附件解析器：提取表格和文本中的电表数据。"""

import re
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class PDFParser:
    """PDF 文件解析器，提取表格及文本信息。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """解析 PDF 文件，返回电表记录列表。"""
        filepath = Path(filepath)
        if not filepath.exists():
            log.error("PDF 文件不存在: %s", filepath)
            return []

        try:
            import pdfplumber
        except ImportError:
            log.error("pdfplumber 未安装，无法解析 PDF")
            return []

        results = []
        try:
            with pdfplumber.open(str(filepath)) as pdf:
                for page_num, page in enumerate(pdf.pages, 1):
                    # 优先提取表格
                    tables = page.extract_tables()
                    for table_idx, table in enumerate(tables):
                        records = self._parse_table(
                            table, filepath, f"page{page_num}_table{table_idx}", source_info
                        )
                        results.extend(records)

                    # 如果没有表格，尝试从文本提取
                    if not tables:
                        text = page.extract_text() or ""
                        if text.strip():
                            records = self._parse_text(text, filepath, f"page{page_num}", source_info)
                            results.extend(records)

        except Exception as e:
            log.error("PDF 解析失败 [%s]: %s", filepath, e, exc_info=True)

        log.info("  PDF 解析完成: %s, 提取 %d 条记录", filepath.name, len(results))
        return results

    def _parse_table(self, table: list[list], filepath: Path,
                     source_sheet: str, source_info: dict) -> list[dict]:
        """解析 PDF 中提取的表格。"""
        if not table or len(table) < 2:
            return []

        import pandas as pd

        headers = [str(h).strip() if h else "" for h in table[0]]
        rows = table[1:]
        df = pd.DataFrame(rows, columns=headers)

        # 复用 Excel 解析逻辑的列映射
        col_map = self._map_columns(df.columns.tolist())

        if not col_map.get("meter_number") and not col_map.get("user_id"):
            return []

        results = []
        for _, row in df.iterrows():
            record = self._extract_record_from_row(row, col_map, filepath, source_sheet, source_info)
            if record:
                results.append(record)
        return results

    def _parse_text(self, text: str, filepath: Path,
                    source_sheet: str, source_info: dict) -> list[dict]:
        """从纯文本中提取电表信息（正则匹配）。"""
        results = []

        # 提取用户编号
        user_id = None
        for pattern in self.field_mapping.get("user_id", []):
            match = re.search(rf'{pattern}\s*[:：]?\s*(\S+)', text)
            if match:
                user_id = match.group(1).strip()
                break

        # 提取电表号
        meter_number = None
        for pattern in self.field_mapping.get("meter_number", []):
            match = re.search(rf'{pattern}\s*[:：]?\s*(\S+)', text)
            if match:
                meter_number = match.group(1).strip()
                break

        if not meter_number and not user_id:
            return results

        reading_month = self._infer_month(source_info, filepath.name, source_sheet)

        record = {
            "meter_number": meter_number or "",
            "asset_number": None,
            "user_id": user_id or "",
            "meter_type": "未知",
            "multiplier": 1.0,
            "project_name": None,
            "reading_month": reading_month,
            "sharp_peak": self._extract_number(text, ["尖峰?", "反向尖"]),
            "peak": self._extract_number(text, ["(?<!尖)峰", "反向峰", "正向峰"]),
            "flat": self._extract_number(text, ["平", "反向平", "正向平"]),
            "valley": self._extract_number(text, ["谷", "反向谷", "正向谷"]),
            "total_kwh": None,
            "source_file": filepath.name,
            "source_sheet": source_sheet,
        }
        results.append(record)
        return results

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

    def _map_columns(self, columns: list[str]) -> dict:
        """列名映射（同 ExcelParser 逻辑）。"""
        col_map = {}
        simple_fields = ["meter_number", "asset_number", "user_id", "multiplier", "project_name"]
        for field in simple_fields:
            aliases = self.field_mapping.get(field, [])
            if isinstance(aliases, list):
                for col in columns:
                    col_clean = str(col).strip()
                    if col_clean in aliases or any(a in col_clean for a in aliases):
                        col_map[field] = col
                        break

        for direction in ("reverse_readings", "forward_readings"):
            dir_mapping = self.field_mapping.get(direction, {})
            for sub_field, aliases in dir_mapping.items():
                key = f"{direction}_{sub_field}"
                for col in columns:
                    col_clean = str(col).strip()
                    if isinstance(aliases, list) and any(a in col_clean for a in aliases):
                        col_map[key] = col
                        break
        return col_map

    def _extract_record_from_row(self, row, col_map: dict, filepath: Path,
                                 source_sheet: str, source_info: dict) -> Optional[dict]:
        """从表格行提取记录。"""
        import pandas as pd

        def get_val(field):
            col = col_map.get(field)
            if col is None:
                return None
            val = row.get(col)
            if pd.isna(val) if hasattr(pd, 'isna') else val is None:
                return None
            return val

        def get_float(field):
            val = get_val(field)
            if val is None:
                return None
            try:
                return float(str(val).replace(",", "").strip())
            except (ValueError, TypeError):
                return None

        meter_number = get_val("meter_number")
        user_id = get_val("user_id")
        if not meter_number and not user_id:
            return None

        reading_month = self._infer_month(source_info, filepath.name, source_sheet)

        return {
            "meter_number": str(meter_number).strip() if meter_number else "",
            "asset_number": str(get_val("asset_number") or "").strip() or None,
            "user_id": str(user_id).strip() if user_id else "",
            "meter_type": "未知",
            "multiplier": get_float("multiplier") or 1.0,
            "project_name": str(get_val("project_name") or "").strip() or None,
            "reading_month": reading_month,
            "sharp_peak": get_float("reverse_readings_sharp_peak") or get_float("forward_readings_sharp"),
            "peak": get_float("reverse_readings_peak") or get_float("forward_readings_peak"),
            "flat": get_float("reverse_readings_flat") or get_float("forward_readings_flat"),
            "valley": get_float("reverse_readings_valley") or get_float("forward_readings_valley"),
            "total_kwh": None,
            "source_file": filepath.name,
            "source_sheet": source_sheet,
        }

    def _infer_month(self, source_info: dict, filename: str, context: str) -> str:
        """推断月份。"""
        for text in [filename, context]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2030 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"
