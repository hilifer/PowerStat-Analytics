"""Excel 附件解析器：支持多 Sheet、自动表头匹配。

从 Excel 文件中提取电表信息和抄表数据，按配置的字段映射规则
智能识别列名与实际字段的对应关系。

增强功能：
- 全表扫描提取用户编号、折扣、配对电表号等非表头信息
- 支持配对电表关系提取（发电表号 ↔ 上网表号）
- 从行标签中提取电表类型（正向尖峰、上网表 等）
- 智能补全：跨文件通过用户编号/电表号关联数据
"""

import re
from pathlib import Path
from typing import Optional

import openpyxl
import pandas as pd

from src.config_loader import config
from src.logger import log
from src.parsers.validators import clean_id, validate_record


class ExcelParser:
    """Excel 附件解析器。"""

    # 从文件名中提取项目名的模式
    PROJECT_PATTERNS = [
        r'([\u4e00-\u9fff]{2,}(?:光伏|工业园|产业园|项目|电站)[\u4e00-\u9fff]*)',
        r'([\u4e00-\u9fff]{2,}(?:公司|有限|集团|工厂)[\u4e00-\u9fff]*)',
    ]

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}
        self.reconciliation_cfg = config.get("reconciliation") or {}

    def parse(self, filepath: str, source_info: dict = None) -> list[dict]:
        """解析 Excel 文件，返回以电表为核心的记录列表。"""
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
                df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
                records = self._parse_dataframe_smart(df, sheet_name, filepath, source_info)
                results.extend(records)
        except Exception as e:
            log.error("解析 XLS 失败 [%s]: %s", filepath, e, exc_info=True)
        return results

    def _parse_sheet(self, sheet, sheet_name: str, filepath: Path,
                     source_info: dict) -> list[dict]:
        """解析单个 Sheet。"""
        data = list(sheet.values)
        if not data:
            return []

        # 转 DataFrame（无表头模式，自己找表头）
        df = pd.DataFrame(data)
        return self._parse_dataframe_smart(df, sheet_name, filepath, source_info)

    def _parse_dataframe_smart(self, df: pd.DataFrame, sheet_name: str,
                                filepath: Path, source_info: dict) -> list[dict]:
        """智能解析 DataFrame：先扫描全表提取元信息，再解析数据行。"""
        if df.empty:
            return []

        # 第一遍：全表扫描提取元信息（用户编号、折扣、配对关系等）
        meta = self._scan_meta_info(df, sheet_name, filepath)
        log.debug("  元信息: %s", {k: v for k, v in meta.items() if v})

        # 找表头行
        header_row_idx = self._find_header_row(df)
        if header_row_idx is None:
            log.debug("  Sheet '%s' 未找到有效表头，跳过", sheet_name)
            return []

        # 用表头行作为列名
        headers = [str(h).strip() if h is not None else "" for h in df.iloc[header_row_idx]]
        data_df = df.iloc[header_row_idx + 1:].copy()
        data_df.columns = headers

        return self._parse_dataframe(data_df, sheet_name, filepath, source_info, meta)

    def _scan_meta_info(self, df: pd.DataFrame, sheet_name: str, filepath: Path) -> dict:
        """全表扫描提取元信息：用户编号、折扣、配对电表号等。

        扫描所有单元格（不仅仅是表头行），提取散落在各处的关联信息。
        """
        meta = {
            "user_ids": [],          # 扫描到的用户编号
            "discount": None,        # 折扣系数
            "grid_meters": {},       # 上网表号 -> 行号映射
            "gen_meters": {},        # 发电表号 -> 行号映射
            "asset_numbers": {},     # 资产编号映射
            "prices": {},            # 扫描到的电价
            "paired_meters": [],     # 配对关系 [(发电表号, 上网表号)]
            "project_name": None,    # 从标题行提取的项目名
        }

        scan_keywords = self.reconciliation_cfg.get("scan_cells_for", [])
        discount_patterns = self.reconciliation_cfg.get("discount_patterns", [])

        for row_idx in range(min(len(df), 50)):  # 扫描前50行
            for col_idx in range(min(len(df.columns), 30)):  # 前30列
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()
                if not cell_str:
                    continue

                # 提取用户编号（格式: "用户号：0950000088133431"）
                for kw in ["用户号", "用户编号", "用电户号", "户号", "Account"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{8,20})', cell_str)
                        for n in nums:
                            if n not in meta["user_ids"]:
                                meta["user_ids"].append(n)
                                log.debug("    扫描到用户编号: %s (行%d)", n, row_idx)

                # 独立数字单元格也可能是用户编号（10位以上纯数字）
                if re.match(r'^\d{10,20}$', cell_str):
                    # 排除日期和普通数值
                    if not re.match(r'^20\d{2}(0[1-9]|1[0-2])', cell_str):
                        if cell_str not in meta["user_ids"]:
                            # 检查同行是否有关联标签
                            row_text = " ".join(str(df.iloc[row_idx, c]) for c in range(min(len(df.columns), 30))
                                                if df.iloc[row_idx, c] is not None and not pd.isna(df.iloc[row_idx, c]))
                            if any(kw in row_text for kw in ["用户", "户号", "编号", "上网", "发电", "资产"]):
                                meta["user_ids"].append(cell_str)

                # 提取折扣信息（格式: "9.8折，折后"）
                for pattern in discount_patterns:
                    m = re.search(pattern, cell_str)
                    if m:
                        try:
                            d = float(m.group(1))
                            if 0 < d <= 10:
                                # 如果是 "9.8折" 格式，转为 0.98
                                meta["discount"] = d / 10.0 if d > 1 else d
                                log.debug("    扫描到折扣: %s -> %.2f (行%d)", cell_str, meta["discount"], row_idx)
                        except ValueError:
                            pass

                # 提取配对电表号和资产编号（格式: "上网电表号：xxx" "发电表号：xxx"）
                for kw in ["上网表", "上网电表", "并网表"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{6,20})', cell_str)
                        for n in nums:
                            meta["grid_meters"][n] = row_idx
                            log.debug("    扫描到上网表号: %s (行%d)", n, row_idx)

                for kw in ["发电表", "发电电表", "逆变表"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{6,20})', cell_str)
                        for n in nums:
                            meta["gen_meters"][n] = row_idx
                            log.debug("    扫描到发电表号: %s (行%d)", n, row_idx)

                for kw in ["资产编号", "资产号", "资产表"]:
                    if kw in cell_str:
                        nums = re.findall(r'[A-Za-z0-9]{10,30}', cell_str)
                        for n in nums:
                            meta["asset_numbers"][n] = row_idx

                # 从标题行提取项目名（通常在前3行）
                if row_idx < 3:
                    for pattern in self.PROJECT_PATTERNS:
                        m = re.search(pattern, cell_str)
                        if m:
                            proj = m.group(1)
                            proj = re.sub(r'(?:统计表|分配表|汇总表|电费表|明细表|电费单)$', '', proj)
                            if len(proj) >= 2:
                                meta["project_name"] = proj

        # 配对：同一行的发电表和上网表
        gen_by_row = {}
        for meter, row in meta["gen_meters"].items():
            gen_by_row.setdefault(row, []).append(meter)
        grid_by_row = {}
        for meter, row in meta["grid_meters"].items():
            grid_by_row.setdefault(row, []).append(meter)

        # 找同行或相邻行的配对
        for row, gen_list in gen_by_row.items():
            for check_row in [row, row - 1, row + 1]:
                if check_row in grid_by_row:
                    for gm in gen_list:
                        for grd in grid_by_row[check_row]:
                            meta["paired_meters"].append((gm, grd))

        return meta

    def _find_header_row(self, df: pd.DataFrame, max_scan: int = 20) -> Optional[int]:
        """扫描前若干行，找到最可能的表头行。"""
        all_keywords = set()
        for field, aliases in self.field_mapping.items():
            if isinstance(aliases, list):
                all_keywords.update(aliases)
            elif isinstance(aliases, dict):
                for sub_aliases in aliases.values():
                    if isinstance(sub_aliases, list):
                        all_keywords.update(sub_aliases)

        best_idx = None
        best_score = 0

        for i in range(min(len(df), max_scan)):
            row = df.iloc[i]
            row_strs = [str(c).strip() for c in row if c is not None and not pd.isna(c)]
            score = sum(1 for cell in row_strs if any(kw in cell for kw in all_keywords))
            if score > best_score:
                best_score = score
                best_idx = i

        return best_idx if best_score >= 2 else None

    def _parse_dataframe(self, df: pd.DataFrame, sheet_name: str,
                         filepath: Path, source_info: dict,
                         meta: dict = None) -> list[dict]:
        """从 DataFrame 提取电表记录。"""
        if meta is None:
            meta = {}
        results = []
        col_map = self._map_columns(df.columns.tolist())

        if not col_map.get("meter_number"):
            log.debug("  Sheet '%s' 缺少电表号列，跳过", sheet_name)
            return []

        log.debug("  列映射: %s", col_map)

        for idx, row in df.iterrows():
            try:
                record = self._extract_record(row, col_map, sheet_name, filepath, source_info, meta)
                if record:
                    results.append(record)
            except Exception as e:
                log.debug("  行 %d 提取失败: %s", idx, e)

        # 用元信息补全所有记录
        self._enrich_with_meta(results, meta)

        return results

    def _map_columns(self, columns: list[str]) -> dict:
        """将 DataFrame 列名映射到标准字段名。"""
        col_map = {}

        simple_fields = ["meter_number", "asset_number", "user_id", "multiplier",
                         "project_name", "discount", "unit_price", "amount",
                         "grid_meter_number", "gen_meter_number", "usage"]
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

        # 额外检测：上月表数/本月表数 配对
        for col in columns:
            col_clean = str(col).strip()
            if "上月表数" in col_clean or "上月" in col_clean:
                col_map.setdefault("prev_reading", col)
            elif "本月表数" in col_clean or "本月" in col_clean:
                col_map.setdefault("curr_reading", col)
            elif "今次读数" in col_clean or "Current" in col_clean:
                col_map.setdefault("curr_reading", col)
            elif "前次读数" in col_clean or "Pre" in col_clean:
                col_map.setdefault("prev_reading", col)
            elif "类别" in col_clean or "类型" in col_clean:
                col_map.setdefault("category", col)

        return col_map

    def _extract_record(self, row, col_map: dict, sheet_name: str,
                        filepath: Path, source_info: dict,
                        meta: dict = None) -> Optional[dict]:
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

        # 检查是否为汇总行
        first_cells = [str(row.iloc[i]).strip() if i < len(row) and not pd.isna(row.iloc[i]) else ""
                       for i in range(min(3, len(row)))]
        summary_keywords = {"合计", "总计", "小计", "总合计", "汇总", "合 计", "总 计"}
        if any(kw in cell for cell in first_cells for kw in summary_keywords):
            return None

        # 判断电表类型
        meter_type = self._detect_meter_type(sheet_name, filepath.name, col_map)

        # 从行内类别列检测电表类型
        category = str(get_val("category") or "")
        if "尖峰" in category or "正有功" in category:
            pass  # 子类别，不改变 meter_type
        row_text = " ".join(str(v) for v in first_cells)
        if "上网" in row_text and meter_type == "未知":
            meter_type = "上网表"
        elif "发电" in row_text and meter_type == "未知":
            meter_type = "发电表"

        # 提取表码数据
        sharp_peak, peak_val, flat_val, valley_val = None, None, None, None

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

        # 总电量：优先用 usage 列，否则从电表用量算
        total_kwh = get_float("usage")

        # 推断月份
        reading_month = self._infer_month(source_info, filepath.name, sheet_name)

        # 项目名：优先从数据列获取 → 元信息 → 文件名推断
        project_name = str(get_val("project_name") or "").strip() or None
        if not project_name and meta:
            project_name = meta.get("project_name")
        if not project_name:
            project_name = self._infer_project(filepath.name, sheet_name)

        # 折扣
        discount = get_float("discount")
        if discount is None and meta:
            discount = meta.get("discount")

        # 电价
        unit_price = get_float("unit_price")

        # 配对的电表号（同一行的上网表号/发电表号）
        grid_meter = str(get_val("grid_meter_number") or "").strip() or None
        gen_meter = str(get_val("gen_meter_number") or "").strip() or None

        # 用户编号：优先列数据，其次元信息
        user_id = get_val("user_id")
        if not user_id and meta and meta.get("user_ids"):
            user_id = meta["user_ids"][0]  # 取第一个扫描到的

        record = {
            "meter_number": get_val("meter_number"),
            "asset_number": get_val("asset_number"),
            "user_id": user_id,
            "meter_type": meter_type,
            "multiplier": get_float("multiplier"),
            "discount": discount,
            "project_name": project_name,
            "reading_month": reading_month,
            "sharp_peak": sharp_peak,
            "peak": peak_val,
            "flat": flat_val,
            "valley": valley_val,
            "total_kwh": total_kwh,
            "unit_price": unit_price,
            "grid_meter_number": grid_meter,
            "gen_meter_number": gen_meter,
            "source_file": filepath.name,
            "source_sheet": sheet_name,
        }

        # 统一验证
        return validate_record(record)

    def _enrich_with_meta(self, records: list[dict], meta: dict):
        """用元信息补全记录中的缺失字段。"""
        if not meta:
            return

        for rec in records:
            # 补全用户编号
            if not rec.get("user_id") and meta.get("user_ids"):
                rec["user_id"] = meta["user_ids"][0]

            # 补全折扣
            if not rec.get("discount") and meta.get("discount"):
                rec["discount"] = meta["discount"]

            # 补全项目名
            if not rec.get("project_name") and meta.get("project_name"):
                rec["project_name"] = meta["project_name"]

            # 通过配对关系补全关联电表号
            meter_num = rec.get("meter_number")
            if meter_num:
                for gen, grid in meta.get("paired_meters", []):
                    if meter_num == gen:
                        rec["grid_meter_number"] = grid
                    elif meter_num == grid:
                        rec["gen_meter_number"] = gen

    def _infer_project(self, filename: str, sheet_name: str) -> Optional[str]:
        """从文件名或 Sheet 名推断项目名称。"""
        for text in [filename, sheet_name]:
            for pattern in self.PROJECT_PATTERNS:
                match = re.search(pattern, text)
                if match:
                    proj = match.group(1)
                    proj = re.sub(r'(?:统计表|分配表|汇总表|电费表|明细表|电费单)$', '', proj)
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

        has_reverse = any(k.startswith("reverse_readings") for k in col_map)
        has_forward = any(k.startswith("forward_readings") for k in col_map)
        if has_reverse and not has_forward:
            return "上网表"
        if has_forward and not has_reverse:
            return "发电表"

        return "未知"

    def _infer_month(self, source_info: dict, filename: str, sheet_name: str) -> str:
        """从文件名、Sheet 名或邮件日期推断数据所属月份。"""
        for text in [filename, sheet_name]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})(?:月?)', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2035 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"

        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")

        return "unknown"
