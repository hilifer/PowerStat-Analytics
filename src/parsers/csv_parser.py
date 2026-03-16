"""CSV / TXT 解析器：处理纯文本表格数据。

复用 ExcelParser 的智能 DataFrame 解析逻辑，
确保配对电表、列名消歧、asset_number 回退等功能统一。
"""

import re
from pathlib import Path

import pandas as pd

from src.config_loader import config
from src.logger import log


class CSVParser:
    """CSV 和 TXT 表格数据解析器。"""

    def __init__(self):
        self._excel_parser = None

    def _get_excel_parser(self):
        if self._excel_parser is None:
            from src.parsers.excel_parser import ExcelParser
            self._excel_parser = ExcelParser()
        return self._excel_parser

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
                                     header=None, engine="python", on_bad_lines="skip")
                    if len(df.columns) >= 2 and len(df) >= 1:
                        break
                    df = None
                except Exception:
                    df = None
            if df is not None:
                break

        if df is None or df.empty:
            return []

        # 复用 ExcelParser 的智能解析（自动检测表头、列映射、配对提取）
        parser = self._get_excel_parser()
        results = parser._parse_dataframe_smart(df, "csv", filepath, source_info)

        if results:
            log.info("  CSV 解析: %s, 提取 %d 条记录", filepath.name, len(results))
        return results
