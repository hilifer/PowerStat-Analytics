"""HTML 邮件正文解析器：从邮件正文中提取表格和电表数据。

HTML 表格复用 ExcelParser 的智能 DataFrame 解析逻辑。
"""

import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config_loader import config
from src.logger import log
from src.parsers.validators import validate_record


class HTMLParser:
    """HTML 邮件正文 / 文本解析器。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self._excel_parser = None

    def _get_excel_parser(self):
        if self._excel_parser is None:
            from src.parsers.excel_parser import ExcelParser
            self._excel_parser = ExcelParser()
        return self._excel_parser

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """解析 HTML 或纯文本文件，提取电表数据。"""
        filepath = Path(filepath)
        if not filepath.exists():
            return []

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log.error("读取文件失败 [%s]: %s", filepath, e)
            return []

        if not content.strip():
            return []

        # 如果是 HTML，先尝试提取表格
        results = []
        if filepath.suffix.lower() in (".html", ".htm") or "<table" in content.lower():
            results = self._parse_html_tables(content, filepath, source_info)

        # 无论是否 HTML，都尝试从全文提取关键信息
        if not results:
            results = self._parse_text_content(content, filepath, source_info)

        if results:
            log.info("  邮件正文解析: %s, 提取 %d 条记录", filepath.name, len(results))

        return results

    def _parse_html_tables(self, html: str, filepath: Path,
                           source_info: dict) -> list[dict]:
        """从 HTML 中提取 <table> 表格数据，复用 ExcelParser 智能解析。"""
        results = []
        try:
            tables = pd.read_html(html, flavor="lxml")
            if not tables:
                tables = pd.read_html(html)
        except ImportError:
            tables = self._regex_extract_tables(html)
            if not tables:
                return []
        except Exception:
            tables = self._regex_extract_tables(html)
            if not tables:
                return []

        parser = self._get_excel_parser()
        for ti, df in enumerate(tables):
            if df.empty or len(df) < 1:
                continue
            # 将 DataFrame 转为无表头格式，让 ExcelParser 自动检测
            # pd.read_html 默认把第一行当表头，需要重置
            full_df = pd.DataFrame([df.columns.tolist()] + df.values.tolist())
            records = parser._parse_dataframe_smart(
                full_df, f"html_table_{ti}", filepath, source_info
            )
            results.extend(records)

        return results

    def _regex_extract_tables(self, html: str) -> list:
        """用正则从 HTML 提取表格，返回 DataFrame 列表。"""
        dfs = []
        table_pattern = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
        row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
        cell_pattern = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
        tag_strip = re.compile(r'<[^>]+>')

        for table_match in table_pattern.finditer(html):
            table_html = table_match.group(1)
            rows_data = []
            for row_match in row_pattern.finditer(table_html):
                cells = cell_pattern.findall(row_match.group(1))
                cells = [tag_strip.sub("", c).strip() for c in cells]
                if cells:
                    rows_data.append(cells)
            if len(rows_data) >= 2:
                max_cols = max(len(r) for r in rows_data)
                rows_data = [r + [""] * (max_cols - len(r)) for r in rows_data]
                try:
                    df = pd.DataFrame(rows_data)
                    dfs.append(df)
                except Exception:
                    pass
        return dfs

    def _parse_text_content(self, text: str, filepath: Path,
                            source_info: dict) -> list[dict]:
        """从纯文本中提取电表关键信息。"""
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean)

        results = []

        user_ids = self._find_values(clean, self.field_mapping.get("user_id", []))
        meter_numbers = self._find_values(clean, self.field_mapping.get("meter_number", []))
        asset_numbers = self._find_values(clean, self.field_mapping.get("asset_number", []))

        # asset_number 也可以作为 meter_number
        if not meter_numbers and asset_numbers:
            meter_numbers = asset_numbers

        if not user_ids and not meter_numbers:
            return []

        reading_month = self._infer_month(source_info, filepath.name)
        project_name = self._extract_project(text, filepath.name, source_info)

        for uid, mn in self._zip_longest(user_ids, meter_numbers):
            record = validate_record({
                "meter_number": mn,
                "asset_number": None,
                "user_id": uid,
                "meter_type": "未知",
                "multiplier": 1.0,
                "project_name": project_name,
                "reading_month": reading_month,
                "sharp_peak": None,
                "peak": None,
                "flat": None,
                "valley": None,
                "total_kwh": None,
                "source_file": filepath.name,
                "source_sheet": "邮件正文",
            })
            if record:
                results.append(record)

        return results

    def _find_values(self, text: str, aliases: list) -> list[str]:
        """按关键词别名从文本中提取对应的值。"""
        values = []
        if not isinstance(aliases, list):
            return values
        for alias in aliases:
            pattern = rf'{re.escape(alias)}\s*[:：]?\s*(\S+)'
            for m in re.finditer(pattern, text):
                val = m.group(1).strip("，。、,.")
                if val and len(val) >= 3:
                    values.append(val)
        return values

    def _zip_longest(self, a: list, b: list):
        max_len = max(len(a), len(b)) if a or b else 0
        for i in range(max_len):
            yield (a[i] if i < len(a) else None, b[i] if i < len(b) else None)

    def _extract_project(self, text: str, filename: str, source_info: dict) -> Optional[str]:
        """提取项目名（复用 ExcelParser 逻辑，不直接用邮件主题）。"""
        parser = self._get_excel_parser()
        # 从文本内容提取
        proj = parser._extract_project_from_text(text[:2000])
        if proj:
            return proj
        # 从文件名提取
        proj = parser._extract_project_from_text(filename)
        if proj:
            return proj
        # 从邮件主题提取（通过 _extract_project_from_text 过滤月份前缀）
        if source_info and source_info.get("email_subject"):
            proj = parser._extract_project_from_text(source_info["email_subject"])
            if proj:
                return proj
        return None

    def _infer_month(self, source_info: dict, filename: str) -> str:
        for text in [filename, (source_info or {}).get("email_subject", "")]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2035 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"
