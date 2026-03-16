"""CSV / TXT 解析器：处理纯文本表格数据。"""

import re
from pathlib import Path

import pandas as pd

from src.config_loader import config
from src.logger import log


class CSVParser:
    """CSV 和 TXT 表格数据解析器。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """解析 CSV 或 TXT 文件。"""
        filepath = Path(filepath)
        if not filepath.exists():
            return []

        # 尝试多种编码和分隔符
        df = None
        for encoding in ("utf-8", "gbk", "gb2312", "utf-8-sig"):
            for sep in (",", "\t", "|", ";"):
                try:
                    df = pd.read_csv(str(filepath), encoding=encoding, sep=sep,
                                     engine="python", on_bad_lines="skip")
                    if len(df.columns) >= 2 and len(df) >= 1:
                        break
                    df = None
                except Exception:
                    df = None
            if df is not None:
                break

        if df is None or df.empty:
            return []

        results = []
        col_map = self._map_columns(df.columns.tolist())

        if not col_map.get("meter_number") and not col_map.get("user_id"):
            return []

        reading_month = self._infer_month(source_info, filepath.name)

        for _, row in df.iterrows():
            record = self._extract_record(row, col_map, filepath, reading_month, source_info)
            if record:
                results.append(record)

        if results:
            log.info("  CSV 解析: %s, 提取 %d 条记录", filepath.name, len(results))
        return results

    def _map_columns(self, columns: list[str]) -> dict:
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

    def _extract_record(self, row, col_map, filepath, reading_month, source_info):
        def get_val(f):
            col = col_map.get(f)
            if col is None:
                return None
            val = row.get(col)
            return None if pd.isna(val) else val

        def get_float(f):
            val = get_val(f)
            if val is None:
                return None
            try:
                return float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                return None

        mn = get_val("meter_number")
        uid = get_val("user_id")
        if not mn and not uid:
            return None

        return {
            "meter_number": str(mn).strip() if mn else "",
            "asset_number": str(get_val("asset_number") or "").strip() or None,
            "user_id": str(uid).strip() if uid else "",
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
            "source_sheet": "csv",
        }

    def _infer_month(self, source_info, filename):
        for text in [filename, (source_info or {}).get("email_subject", "")]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2030 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"
