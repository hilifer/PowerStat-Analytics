"""Excel 附件解析器：支持多 Sheet、自动表头匹配。

从 Excel 文件中提取电表信息和抄表数据，按配置的字段映射规则
智能识别列名与实际字段的对应关系。
"""

import re
from pathlib import Path
from typing import Optional

import openpyxl
import pandas as pd

from src.config_loader import config
from src.logger import log


class ExcelParser:
    """Excel 附件解析器。"""

    # 需要过滤的汇总/无效行关键词
    SKIP_KEYWORDS = {"合计", "总计", "小计", "总合计", "汇总", "合 计", "总 计"}

    # 常见项目名关键词（从文件名中提取项目名）
    PROJECT_PATTERNS = [
        r'([\u4e00-\u9fff]{2,}(?:光伏|工业园|产业园|项目|电站)[\u4e00-\u9fff]*)',
    ]

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """
        解析 Excel 文件，返回以电表为核心的记录列表。

        每条记录结构:
        {
            "meter_number": str,
            "asset_number": str,
            "user_id": str,
            "meter_type": str,
            "multiplier": float,
            "project_name": str,
            "reading_month": str,
            "sharp_peak": float,
            "peak": float,
            "flat": float,
            "valley": float,
            "total_kwh": float,
            "source_file": str,
            "source_sheet": str,
        }
        """
        filepath = Path(filepath)
        if not filepath.exists():
            log.error("文件不存在: %s", filepath)
            return []

        ext = filepath.suffix.lower()
        if ext == ".xls":
            return self._parse_xls(filepath, source_info)

        results = []
        try:
            wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                log.info("  解析 Sheet: %s", sheet_name)
                sheet_records = self._parse_sheet(wb[sheet_name], sheet_name, filepath, source_info)
                results.extend(sheet_records)
            wb.close()
        except Exception as e:
            log.error("解析 Excel 失败 [%s]: %s", filepath, e, exc_info=True)

        log.info("  Excel 解析完成: %s, 提取 %d 条记录", filepath.name, len(results))
        return results

    def _parse_xls(self, filepath: Path, source_info: dict) -> list[dict]:
        """使用 pandas + xlrd 解析旧格式 .xls 文件。"""
        results = []
        try:
            xls = pd.ExcelFile(str(filepath), engine="xlrd")
            for sheet_name in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet_name)
                records = self._parse_dataframe(df, sheet_name, filepath, source_info)
                results.extend(records)
        except Exception as e:
            log.error("解析 XLS 失败 [%s]: %s", filepath, e, exc_info=True)
        return results

    def _parse_sheet(self, sheet, sheet_name: str, filepath: Path,
                     source_info: dict) -> list[dict]:
        """解析单个 Sheet。"""
        # 将 sheet 转为 DataFrame
        data = list(sheet.values)
        if not data:
            return []

        # 智能定位表头行（找到包含电表号/用户编号等关键词的行）
        header_row_idx = self._find_header_row(data)
        if header_row_idx is None:
            log.debug("  Sheet '%s' 未找到有效表头，跳过", sheet_name)
            return []

        headers = [str(h).strip() if h else "" for h in data[header_row_idx]]
        rows = data[header_row_idx + 1:]

        df = pd.DataFrame(rows, columns=headers)
        return self._parse_dataframe(df, sheet_name, filepath, source_info)

    def _find_header_row(self, data: list[tuple], max_scan: int = 20) -> Optional[int]:
        """扫描前若干行，找到最可能的表头行。"""
        all_keywords = set()
        for aliases in self.field_mapping.values():
            if isinstance(aliases, list):
                all_keywords.update(aliases)
            elif isinstance(aliases, dict):
                for sub_aliases in aliases.values():
                    if isinstance(sub_aliases, list):
                        all_keywords.update(sub_aliases)

        best_idx = None
        best_score = 0

        for i, row in enumerate(data[:max_scan]):
            if not row:
                continue
            row_strs = [str(c).strip() for c in row if c is not None]
            score = sum(1 for cell in row_strs if any(kw in cell for kw in all_keywords))
            if score > best_score:
                best_score = score
                best_idx = i

        return best_idx if best_score >= 2 else None

    def _parse_dataframe(self, df: pd.DataFrame, sheet_name: str,
                         filepath: Path, source_info: dict) -> list[dict]:
        """从 DataFrame 提取电表记录。"""
        results = []
        col_map = self._map_columns(df.columns.tolist())

        if not col_map.get("meter_number"):
            log.debug("  Sheet '%s' 缺少电表号列，跳过", sheet_name)
            return []

        log.debug("  列映射: %s", col_map)

        for idx, row in df.iterrows():
            try:
                record = self._extract_record(row, col_map, sheet_name, filepath, source_info)
                if record and record.get("meter_number"):
                    results.append(record)
            except Exception as e:
                log.debug("  行 %d 提取失败: %s", idx, e)

        return results

    def _map_columns(self, columns: list[str]) -> dict:
        """将 DataFrame 列名映射到标准字段名。"""
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

        # 表码数据映射
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

    def _extract_record(self, row, col_map: dict, sheet_name: str,
                        filepath: Path, source_info: dict) -> Optional[dict]:
        """从单行提取一条电表记录。"""
        def get_val(field_name):
            col = col_map.get(field_name)
            if col is None:
                return None
            val = row.get(col)
            if pd.isna(val):
                return None
            return val

        def get_float(field_name):
            val = get_val(field_name)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        meter_number = get_val("meter_number")
        user_id = get_val("user_id")

        if not meter_number and not user_id:
            return None

        meter_number = self._clean_id_value(str(meter_number).strip()) if meter_number else ""
        user_id = self._clean_id_value(str(user_id).strip()) if user_id else ""

        # 跳过汇总行（合计/总计/小计等）
        if self._is_summary_row(meter_number, user_id, row):
            return None

        # 电表号必须存在且有效（纯数字/字母，长度≥6）
        if not meter_number or len(meter_number) < 6:
            return None
        if not re.match(r'^[0-9A-Za-z\-\.]+$', meter_number):
            log.debug("  跳过无效电表号: %s", meter_number)
            return None

        # 用户编号可为空，但如果有值则须有效
        if user_id and not re.match(r'^[0-9A-Za-z\-\.]+$', user_id):
            log.debug("  跳过无效用户编号: %s，清空", user_id)
            user_id = ""

        # 判断电表类型
        meter_type = self._detect_meter_type(sheet_name, filepath.name, col_map)

        # 提取表码数据（根据类型选择方向）
        sharp_peak = None
        peak_val = None
        flat_val = None
        valley_val = None

        if meter_type == "上网表":
            sharp_peak = get_float("reverse_readings_sharp_peak")
            peak_val = get_float("reverse_readings_peak")
            flat_val = get_float("reverse_readings_flat")
            valley_val = get_float("reverse_readings_valley")
        elif meter_type == "发电表":
            sharp_peak = get_float("forward_readings_sharp")
            peak_val = get_float("forward_readings_peak")
            flat_val = get_float("forward_readings_flat")
            valley_val = get_float("forward_readings_valley")

        # 如果按方向没找到，尝试通用列名
        if all(v is None for v in [sharp_peak, peak_val, flat_val, valley_val]):
            for direction in ("reverse_readings", "forward_readings"):
                sp = get_float(f"{direction}_sharp_peak") or get_float(f"{direction}_sharp")
                pk = get_float(f"{direction}_peak")
                fl = get_float(f"{direction}_flat")
                vl = get_float(f"{direction}_valley")
                if any(v is not None for v in [sp, pk, fl, vl]):
                    sharp_peak, peak_val, flat_val, valley_val = sp, pk, fl, vl
                    break

        # 推断月份
        reading_month = self._infer_month(source_info, filepath.name, sheet_name)

        # 项目名：优先从数据列获取，其次从文件名推断
        project_name = str(get_val("project_name") or "").strip() or None
        if not project_name:
            project_name = self._infer_project(filepath.name, sheet_name)

        # 资产编号清理
        asset_number = self._clean_id_value(str(get_val("asset_number") or "").strip())
        if asset_number and not re.match(r'^[0-9A-Za-z\-\.]+$', asset_number):
            asset_number = None

        # 倍率
        multiplier = get_float("multiplier")

        record = {
            "meter_number": meter_number,
            "asset_number": asset_number or None,
            "user_id": user_id or None,
            "meter_type": meter_type,
            "multiplier": multiplier,
            "project_name": project_name,
            "reading_month": reading_month,
            "sharp_peak": sharp_peak,
            "peak": peak_val,
            "flat": flat_val,
            "valley": valley_val,
            "total_kwh": None,
            "source_file": filepath.name,
            "source_sheet": sheet_name,
        }
        return record

    @staticmethod
    def _clean_id_value(val: str) -> str:
        """清理电表号/用户编号中的前缀和多余字符。"""
        if not val:
            return val
        # 去除常见前缀如 "用户号：", "用户编号:", "资产编号" 等
        val = re.sub(r'^(?:用户编号|用户号|户号|客户编号|电表号|表号|资产编号)\s*[:：]?\s*', '', val)
        # 去除引号
        val = val.strip("'\"''""")
        # 去除浮点数的 .0 后缀（Excel 数字列导出常见）
        val = re.sub(r'\.0+$', '', val)
        return val.strip()

    def _is_summary_row(self, meter_number: str, user_id: str, row) -> bool:
        """判断是否为汇总行。"""
        # 检查电表号和用户编号是否包含汇总关键词
        for val in [meter_number, user_id]:
            if val and any(kw in val for kw in self.SKIP_KEYWORDS):
                return True
        # 检查整行所有字符串值
        for val in row:
            if isinstance(val, str) and val.strip() in self.SKIP_KEYWORDS:
                return True
        return False

    def _infer_project(self, filename: str, sheet_name: str) -> Optional[str]:
        """从文件名或 Sheet 名推断项目名称。"""
        for text in [filename, sheet_name]:
            for pattern in self.PROJECT_PATTERNS:
                match = re.search(pattern, text)
                if match:
                    proj = match.group(1)
                    # 去掉尾部的"统计表"、"分配表"等
                    proj = re.sub(r'(?:统计表|分配表|汇总表|电费表|明细表)$', '', proj)
                    if len(proj) >= 2:
                        return proj
        return None

    def _detect_meter_type(self, sheet_name: str, filename: str, col_map: dict) -> str:
        """根据文件名、Sheet 名、列名判断电表类型。"""
        context = f"{filename} {sheet_name} {' '.join(col_map.keys())}"

        for mtype, rule in self.meter_type_rules.items():
            keywords = rule.get("keywords", [])
            if any(kw in context for kw in keywords):
                if mtype == "grid_meter":
                    return "上网表"
                elif mtype == "generation_meter":
                    return "发电表"

        # 根据列名方向推断
        has_reverse = any(k.startswith("reverse_readings") for k in col_map)
        has_forward = any(k.startswith("forward_readings") for k in col_map)
        if has_reverse and not has_forward:
            return "上网表"
        if has_forward and not has_reverse:
            return "发电表"

        return "未知"

    def _infer_month(self, source_info: dict, filename: str, sheet_name: str) -> str:
        """从文件名、Sheet 名或邮件日期推断数据所属月份。"""
        # 尝试从文件名/Sheet 名提取 YYYY-MM 或 YYYYMM
        for text in [filename, sheet_name]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2030 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"

        # 从邮件日期推断
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")

        return "unknown"
