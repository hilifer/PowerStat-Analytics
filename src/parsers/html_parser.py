"""HTML 邮件正文解析器：从邮件正文中提取表格和电表数据。"""

import re
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class HTMLParser:
    """HTML 邮件正文 / 文本解析器。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}

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
        """从 HTML 中提取 <table> 表格数据。"""
        results = []
        try:
            import pandas as pd
            tables = pd.read_html(html, flavor="lxml")
            if not tables:
                tables = pd.read_html(html)
        except ImportError:
            # 没有 lxml 时用正则手动提取
            tables = self._regex_extract_tables(html)
            if not tables:
                return []
        except Exception:
            tables = self._regex_extract_tables(html)
            if not tables:
                return []

        import pandas as pd
        for ti, df in enumerate(tables):
            if df.empty or len(df) < 1:
                continue
            records = self._extract_from_dataframe(
                df, filepath, f"html_table_{ti}", source_info
            )
            results.extend(records)

        return results

    def _regex_extract_tables(self, html: str) -> list:
        """用正则从 HTML 提取表格，返回 DataFrame 列表。"""
        import pandas as pd

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
                headers = rows_data[0]
                data = rows_data[1:]
                # 对齐列数
                max_cols = max(len(r) for r in [headers] + data)
                headers += [""] * (max_cols - len(headers))
                data = [r + [""] * (max_cols - len(r)) for r in data]
                try:
                    df = pd.DataFrame(data, columns=headers)
                    dfs.append(df)
                except Exception:
                    pass
        return dfs

    def _parse_text_content(self, text: str, filepath: Path,
                            source_info: dict) -> list[dict]:
        """从纯文本中提取电表关键信息。"""
        # 去除 HTML 标签
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = re.sub(r'\s+', ' ', clean)

        results = []

        # 提取所有可能的数据对
        user_ids = self._find_values(clean, self.field_mapping.get("user_id", []))
        meter_numbers = self._find_values(clean, self.field_mapping.get("meter_number", []))

        if not user_ids and not meter_numbers:
            return []

        reading_month = self._infer_month(source_info, filepath.name)

        # 提取电量数值
        for i, (uid, mn) in enumerate(
            self._zip_longest(user_ids, meter_numbers)
        ):
            record = {
                "meter_number": mn or "",
                "asset_number": None,
                "user_id": uid or "",
                "meter_type": "未知",
                "multiplier": 1.0,
                "project_name": self._extract_project(text, source_info),
                "reading_month": reading_month,
                "sharp_peak": None,
                "peak": None,
                "flat": None,
                "valley": None,
                "total_kwh": None,
                "source_file": filepath.name,
                "source_sheet": "邮件正文",
            }
            results.append(record)

        return results

    def _extract_from_dataframe(self, df, filepath: Path,
                                 source_sheet: str, source_info: dict) -> list[dict]:
        """从 DataFrame 提取电表记录（复用 ExcelParser 的列映射逻辑）。"""
        import pandas as pd

        results = []
        col_map = {}

        simple_fields = ["meter_number", "asset_number", "user_id", "multiplier", "project_name"]
        for field in simple_fields:
            aliases = self.field_mapping.get(field, [])
            if isinstance(aliases, list):
                for col in df.columns:
                    col_clean = str(col).strip()
                    if col_clean in aliases or any(a in col_clean for a in aliases):
                        col_map[field] = col
                        break

        if not col_map.get("meter_number") and not col_map.get("user_id"):
            return []

        reading_month = self._infer_month(source_info, filepath.name)

        for _, row in df.iterrows():
            def get_val(field):
                col = col_map.get(field)
                if col is None:
                    return None
                val = row.get(col)
                if pd.isna(val):
                    return None
                return val

            mn = get_val("meter_number")
            uid = get_val("user_id")
            if not mn and not uid:
                continue

            record = {
                "meter_number": str(mn).strip() if mn else "",
                "asset_number": str(get_val("asset_number") or "").strip() or None,
                "user_id": str(uid).strip() if uid else "",
                "meter_type": "未知",
                "multiplier": 1.0,
                "project_name": str(get_val("project_name") or "").strip() or None,
                "reading_month": reading_month,
                "sharp_peak": None,
                "peak": None,
                "flat": None,
                "valley": None,
                "total_kwh": None,
                "source_file": filepath.name,
                "source_sheet": source_sheet,
            }
            results.append(record)

        return results

    def _find_values(self, text: str, aliases: list) -> list[str]:
        """按关键词别名从文本中提取对应的值。"""
        values = []
        for alias in aliases:
            pattern = rf'{re.escape(alias)}\s*[:：]?\s*(\S+)'
            for m in re.finditer(pattern, text):
                val = m.group(1).strip("，。、,.")
                if val and len(val) >= 3:
                    values.append(val)
        return values

    def _zip_longest(self, a: list, b: list):
        """zip 两个列表，短的用 None 补齐。"""
        max_len = max(len(a), len(b)) if a or b else 0
        for i in range(max_len):
            yield (a[i] if i < len(a) else None, b[i] if i < len(b) else None)

    def _extract_project(self, text: str, source_info: dict) -> Optional[str]:
        """从文本或邮件主题中提取项目名。"""
        aliases = self.field_mapping.get("project_name", [])
        for alias in aliases:
            match = re.search(rf'{re.escape(alias)}\s*[:：]?\s*(\S+)', text)
            if match:
                return match.group(1).strip("，。、,.")
        # 从邮件主题推断
        if source_info and source_info.get("email_subject"):
            return source_info["email_subject"]
        return None

    def _infer_month(self, source_info: dict, filename: str) -> str:
        """推断月份。"""
        for text in [filename, (source_info or {}).get("email_subject", "")]:
            match = re.search(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text)
            if match:
                return f"{match.group(1)}-{match.group(2).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"
