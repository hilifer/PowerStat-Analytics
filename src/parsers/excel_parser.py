"""Excel 附件解析器：支持多 Sheet、自动表头匹配。

从 Excel 文件中提取电表信息和抄表数据，按配置的字段映射规则
智能识别列名与实际字段的对应关系。

核心能力：
- 配对电表支持：同一行有发电表号+上网表号时，自动拆分为2条记录
- 全表扫描提取用户编号、折扣、配对电表号等非表头信息
- 从行标签中提取电表类型（正向尖峰、上网表 等）
- 位置感知列映射：处理重复列名（如正向/反向都有 尖/峰/平/谷）
- 用文件内部数据建立关系（统计日期、用户编号等），不依赖文件名
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
        df = pd.DataFrame(data)
        return self._parse_dataframe_smart(df, sheet_name, filepath, source_info)

    def _parse_dataframe_smart(self, df: pd.DataFrame, sheet_name: str,
                                filepath: Path, source_info: dict) -> list[dict]:
        """智能解析 DataFrame：自动检测格式，选择最佳解析策略。

        支持的格式：
        1. 转置表（行=尖峰平谷，右侧卡片区有电表号）
        2. 标准横向表（含多月分段子表）
        3. 纵向卡片格式（标签-值配对）
        """
        if df.empty:
            return []

        # 第一遍：全表扫描提取元信息（用户号、配对关系、项目名等）
        meta = self._scan_meta_info(df, sheet_name, filepath)
        log.info("  元信息: %s", {k: v for k, v in meta.items() if v})

        # 策略1：检测转置表（行=正有功尖峰/峰/平/谷，如发电统计表）
        transposed = self._try_parse_transposed_table(df, sheet_name, filepath, source_info, meta)
        if transposed:
            return transposed

        # 策略2：标准横向表（找到表头行后解析数据行）
        header_row_idx = self._find_header_row(df)
        if header_row_idx is not None:
            log.info("  策略2: 找到表头行 %d", header_row_idx)
            results = self._parse_standard_table(df, header_row_idx, sheet_name, filepath, source_info, meta)
            if results:
                return results
            log.info("  策略2: 标准表解析返回空")
        else:
            log.info("  策略2: 未找到表头行")

        # 策略3：纵向卡片格式（标签-值配对）
        card_records = self._try_parse_card_layout(df, sheet_name, filepath, source_info, meta)
        if card_records:
            return card_records

        log.warning("  Sheet '%s' 三种策略均未能提取有效数据 (rows=%d, cols=%d)",
                     sheet_name, len(df), len(df.columns))
        # 打印前几行帮助调试
        for i in range(min(8, len(df))):
            row_vals = [str(df.iloc[i, j]).strip() for j in range(min(12, len(df.columns)))
                        if df.iloc[i, j] is not None and not pd.isna(df.iloc[i, j])
                        and str(df.iloc[i, j]).strip()]
            if row_vals:
                log.warning("    行%d: %s", i, " | ".join(row_vals[:8]))
        return []

    def _parse_standard_table(self, df: pd.DataFrame, header_row_idx: int,
                               sheet_name: str, filepath: Path,
                               source_info: dict, meta: dict) -> list[dict]:
        """解析标准横向表格，支持多月分段子表。"""
        # 用表头行作为列名 — 处理重复列名（位置感知消歧）
        raw_headers = [str(h).strip() if h is not None else "" for h in df.iloc[header_row_idx]]
        headers = self._disambiguate_headers(raw_headers)
        log.debug("  消歧后表头: %s", headers)

        data_df = df.iloc[header_row_idx + 1:].copy()
        data_df.columns = headers

        # 检测多月分段（同 Sheet 包含多个月份子表）
        sections = self._split_sections(data_df, sheet_name, filepath)
        if sections:
            all_results = []
            for section_month, section_df in sections:
                section_meta = dict(meta)
                if section_month:
                    section_meta["reading_dates"] = [section_month]
                results = self._parse_dataframe(section_df, sheet_name, filepath, source_info, section_meta)
                all_results.extend(results)
                if results:
                    log.info("  分段 [%s] %s: %d 条记录", sheet_name, section_month or "默认", len(results))
            return all_results

        return self._parse_dataframe(data_df, sheet_name, filepath, source_info, meta)

    def _split_sections(self, df: pd.DataFrame, sheet_name: str, filepath: Path) -> list:
        """检测并分割多月子表。

        识别标志：数据行中出现包含 "YYYY年N月" 的标题行（通常只有第一列有值，
        其余列为空或合并单元格），标题行之后是相同结构的数据行。

        返回 [(month_str, sub_df), ...] 或 空列表（不是多月格式）
        """
        sections = []
        month_pattern = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')
        title_rows = []

        for idx in range(len(df)):
            row = df.iloc[idx]
            row_strs = [str(c).strip() for c in row if c is not None and not pd.isna(c)]
            row_text = " ".join(row_strs)

            # 标题行特征：包含"年月"，且非空值少（大部分是合并单元格）
            if month_pattern.search(row_text):
                non_empty = sum(1 for c in row if c is not None and not pd.isna(c)
                                and str(c).strip())
                if non_empty <= 3:
                    m = month_pattern.search(row_text)
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        month_str = f"{y}-{str(mo).zfill(2)}"
                        title_rows.append((idx, month_str))

        # 至少2个月才算多月分段
        if len(title_rows) < 2:
            return []

        log.info("  检测到多月分段 [%s]: %d 个月份", sheet_name, len(title_rows))

        for i, (title_idx, month_str) in enumerate(title_rows):
            start = title_idx + 1  # 标题行之后
            end = title_rows[i + 1][0] if i + 1 < len(title_rows) else len(df)
            if start < end:
                sub_df = df.iloc[start:end].copy()
                sub_df.columns = df.columns
                # 过滤掉合计行和空行
                sub_df = sub_df[sub_df.apply(
                    lambda r: any(str(c).strip() not in ("", "nan", "None", "合计", "总计", "小计")
                                  for c in r if c is not None), axis=1
                )]
                if not sub_df.empty:
                    sections.append((month_str, sub_df))

        return sections

    def _try_parse_transposed_table(self, df: pd.DataFrame, sheet_name: str,
                                     filepath: Path, source_info: dict,
                                     meta: dict) -> list[dict]:
        """解析转置表格（行=时段如尖峰/峰/平/谷，列=数据指标）。

        典型布局：
        | 类别      | 上月表数 | 本月表数 | 电表用理 | 倍率  | 发电量   | ... | 用户号      |
        | 正有功尖峰| 68.92   | 118.77  | 49.85   | 60.00 | 2991.00 | ... | 094803...   |
        | 正有功峰  | 113.40  | 682.36  | 47.01   | 60.00 | 2820.60 | ... | 发电表号    |
        | 正有功平  | 119.44  | ...     | 78.58   | ...   | ...     | ... | 094803...   |
        | 正有功谷  | 3.6     | ...     | 1.35    | ...   | 81.00   | ... | 上网表号    |
        | 正有功总  | 258.37  | ...     | 176.78  | ...   | 10606.8 | ... | 094803...   |

        右侧列包含卡片式电表信息（用户号/发电表号/上网表号等标签+值交替排列）。
        """
        # 时段关键词（行标识）
        period_keywords = {
            "尖峰": "sharp_peak", "尖": "sharp_peak",
            "正有功尖峰": "sharp_peak", "正有功尖": "sharp_peak",
            "峰": "peak", "正有功峰": "peak",
            "平": "flat", "正有功平": "flat",
            "谷": "valley", "正有功谷": "valley",
            "总": "total", "正有功总": "total", "合计": "total",
        }

        # 扫描第一列，检测是否为转置格式
        first_col_periods = {}  # {row_idx: period_key}
        category_col = None

        for col_idx in range(min(3, len(df.columns))):
            period_count = 0
            temp_periods = {}
            for row_idx in range(min(len(df), 30)):
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()
                for kw, pkey in period_keywords.items():
                    if cell_str == kw or cell_str.startswith(kw):
                        temp_periods[row_idx] = pkey
                        period_count += 1
                        break
            if period_count >= 3:
                first_col_periods = temp_periods
                category_col = col_idx
                break

        if len(first_col_periods) < 3:
            return []

        log.info("  检测到转置表格 [%s]: %d 个时段行", sheet_name, len(first_col_periods))

        # 从右侧列提取卡片信息（用户号/发电表号/上网表号等）
        card_info = self._extract_card_from_side_columns(df, meta)
        log.info("  转置表卡片信息: %s", {k: v for k, v in card_info.items() if v})

        # 找到数据列组：正向数据和反向数据
        # 扫描表头区域确定列分组
        col_groups = self._identify_transposed_column_groups(df, category_col)

        # 提取各时段读数
        forward_readings = {"sharp_peak": None, "peak": None, "flat": None, "valley": None}
        reverse_readings = {"sharp_peak": None, "peak": None, "flat": None, "valley": None}
        forward_total = None
        reverse_total = None
        multiplier = None

        for row_idx, period in first_col_periods.items():
            row = df.iloc[row_idx]
            # 从正向数据组取值
            fwd_val = self._get_transposed_value(row, col_groups.get("forward_usage"))
            rev_val = self._get_transposed_value(row, col_groups.get("reverse_usage"))
            # 备选：电表用理列
            fwd_meter_usage = self._get_transposed_value(row, col_groups.get("forward_meter_usage"))
            rev_meter_usage = self._get_transposed_value(row, col_groups.get("reverse_meter_usage"))

            fwd = fwd_val or fwd_meter_usage
            rev = rev_val or rev_meter_usage

            if period == "total":
                forward_total = fwd
                reverse_total = rev
            elif period in forward_readings:
                forward_readings[period] = fwd
                reverse_readings[period] = rev

            # 提取倍率（通常每行相同）
            if multiplier is None:
                mult_val = self._get_transposed_value(row, col_groups.get("multiplier"))
                if mult_val and mult_val > 1:
                    multiplier = mult_val

        # 推断月份
        reading_month = self._infer_month(source_info, filepath.name, sheet_name)
        if reading_month == "unknown" and meta.get("reading_dates"):
            reading_month = meta["reading_dates"][0]

        # 从标题行提取月份
        for row_idx in range(min(5, len(df))):
            for col_idx in range(min(len(df.columns), 5)):
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()
                m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', cell_str)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        reading_month = f"{y}-{str(mo).zfill(2)}"
                        break

        project_name = meta.get("project_name") or card_info.get("project_name")
        user_id = card_info.get("user_id")
        if not user_id and meta.get("user_ids"):
            user_id = meta["user_ids"][0]

        results = []
        from src.parsers.validators import validate_record

        base = {
            "user_id": user_id,
            "multiplier": multiplier or card_info.get("multiplier"),
            "discount": card_info.get("discount"),
            "project_name": project_name,
            "reading_month": reading_month,
            "source_file": filepath.name,
            "source_sheet": sheet_name,
        }

        # 发电表记录（正向数据）
        gen_meter = card_info.get("gen_meter")
        # 兜底：从 meta 获取电表号
        if not gen_meter and meta.get("gen_meters"):
            gen_meter = list(meta["gen_meters"].keys())[0]
        if gen_meter and any(v is not None for v in forward_readings.values()):
            rec = {**base,
                   "meter_number": gen_meter,
                   "asset_number": card_info.get("gen_asset"),
                   "meter_type": "发电表",
                   "sharp_peak": forward_readings["sharp_peak"],
                   "peak": forward_readings["peak"],
                   "flat": forward_readings["flat"],
                   "valley": forward_readings["valley"],
                   "total_kwh": forward_total}
            validated = validate_record(rec)
            if validated:
                results.append(validated)

        # 上网表记录（反向数据）
        grid_meter = card_info.get("grid_meter")
        if not grid_meter and meta.get("grid_meters"):
            grid_meter = list(meta["grid_meters"].keys())[0]
        if grid_meter and any(v is not None for v in reverse_readings.values()):
            rec = {**base,
                   "meter_number": grid_meter,
                   "asset_number": card_info.get("grid_asset"),
                   "meter_type": "上网表",
                   "sharp_peak": reverse_readings["sharp_peak"],
                   "peak": reverse_readings["peak"],
                   "flat": reverse_readings["flat"],
                   "valley": reverse_readings["valley"],
                   "total_kwh": reverse_total}
            validated = validate_record(rec)
            if validated:
                results.append(validated)

        # 如果没有从卡片获取到分离的电表号，但有主电表号
        if not results and (card_info.get("meter_number") or meta.get("gen_meters")):
            mn = card_info.get("meter_number")
            if not mn and meta.get("gen_meters"):
                mn = list(meta["gen_meters"].keys())[0]
            if mn:
                rec = {**base,
                       "meter_number": mn,
                       "asset_number": card_info.get("asset_number"),
                       "meter_type": "未知",
                       "sharp_peak": forward_readings["sharp_peak"],
                       "peak": forward_readings["peak"],
                       "flat": forward_readings["flat"],
                       "valley": forward_readings["valley"],
                       "total_kwh": forward_total}
                validated = validate_record(rec)
                if validated:
                    results.append(validated)

        # 提取单价信息（如果有）
        prices = card_info.get("prices", {})
        if prices:
            for rec in results:
                if not rec.get("unit_price"):
                    rec["prices"] = prices

        if results:
            log.info("  转置表提取 [%s]: %d 条记录", sheet_name, len(results))
            return results

        # 转置表格式检测到了但未提取到记录 → 不阻塞其他策略
        log.info("  转置表格式检测到但未提取到有效记录 (gen=%s, grid=%s, meter=%s, fwd_data=%s)",
                 gen_meter, grid_meter, card_info.get("meter_number"),
                 any(v is not None for v in forward_readings.values()))
        return []  # 返回空让其他策略尝试

    def _extract_card_from_side_columns(self, df: pd.DataFrame, meta: dict) -> dict:
        """从表格右侧列提取卡片式电表信息。

        扫描所有单元格，查找标签-值配对（横向或纵向）。
        """
        from src.parsers.validators import clean_id

        card_labels = {
            "用户号": "user_id", "用户编号": "user_id", "户号": "user_id",
            "发电表号": "gen_meter", "发电表": "gen_meter",
            "上网表号": "grid_meter", "上网表": "grid_meter",
            "发电表资产编号": "gen_asset", "发电表资产号": "gen_asset",
            "发电资产编号": "gen_asset",
            "上网表资产编号": "grid_asset", "上网表资产号": "grid_asset",
            "上网资产号": "grid_asset",
            "电表号": "meter_number", "表号": "meter_number",
            "电表资产号": "asset_number", "资产编号": "asset_number",
            "倍率": "multiplier", "CT倍率": "multiplier",
            "折扣": "discount",
            "项目名称": "project_name", "项目": "project_name",
        }

        info = {}
        max_rows = min(len(df), 50)
        max_cols = min(len(df.columns), 30)

        for row_idx in range(max_rows):
            for col_idx in range(max_cols):
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip().rstrip("：: ")

                clean_str = cell_str.rstrip("：: ")
                matched_field = None

                # 精确匹配标签
                if clean_str in card_labels:
                    matched_field = card_labels[clean_str]
                else:
                    # 内嵌格式 "标签：值" 或 "标签号XXXXX"
                    for lbl, fld in card_labels.items():
                        m = re.match(rf'^{re.escape(lbl)}\s*[:：]?\s*([A-Za-z0-9]{{6,}})', cell_str)
                        if m:
                            matched_field = fld
                            val = m.group(1).strip()
                            if val and fld not in info:
                                if fld == "multiplier":
                                    try:
                                        info[fld] = float(val)
                                    except ValueError:
                                        pass
                                else:
                                    info[fld] = clean_id(val)
                            break

                if not matched_field:
                    continue

                field = matched_field
                if field in info:
                    continue

                # 查找值：右侧 → 下方 → 同列各行
                value = None
                for nc in range(col_idx + 1, min(max_cols, col_idx + 4)):
                    v = df.iloc[row_idx, nc]
                    if v is not None and not pd.isna(v):
                        vs = str(v).strip()
                        if vs and vs.rstrip("：: ") not in card_labels and len(vs) >= 2:
                            value = vs
                            break
                if not value:
                    # 下方同列 → 下方偏右（对角线，常见于合并单元格布局）
                    for nr in range(row_idx + 1, min(max_rows, row_idx + 4)):
                        for nc in range(col_idx, min(max_cols, col_idx + 3)):
                            v = df.iloc[nr, nc]
                            if v is not None and not pd.isna(v):
                                vs = str(v).strip()
                                if vs and vs.rstrip("：: ") not in card_labels and len(vs) >= 2:
                                    value = vs
                                    break
                        if value:
                            break

                if value:
                    if field in ("multiplier",):
                        try:
                            info[field] = float(value.replace(",", ""))
                        except ValueError:
                            pass
                    elif field == "discount":
                        dm = re.search(r'(\d+\.?\d*)', value)
                        if dm:
                            d = float(dm.group(1))
                            info[field] = d / 10.0 if d > 1 else d
                    else:
                        info[field] = clean_id(value)

        return info

    def _identify_transposed_column_groups(self, df: pd.DataFrame, category_col: int) -> dict:
        """识别转置表的列分组（正向数据/反向数据/倍率等）。

        通过扫描表头区域的关键词来确定各列的含义。
        """
        groups = {}
        max_cols = min(len(df.columns), 20)

        # 扫描前几行，建立列含义
        for row_idx in range(min(8, len(df))):
            for col_idx in range(max_cols):
                if col_idx == category_col:
                    continue
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()

                # 检测列分组标记
                if "正向" in cell_str or "发电" in cell_str:
                    # 这一行是正向数据的分组头
                    # 下一行的具体列名会更精确
                    pass
                if "反向" in cell_str or "上网" in cell_str:
                    pass

                # 具体列标识
                if cell_str in ("发电量", "用电量", "正向用电量"):
                    groups["forward_usage"] = col_idx
                elif cell_str in ("上网电量", "上网用电量", "反向用电量"):
                    groups["reverse_usage"] = col_idx
                elif cell_str in ("电表用理", "电表用量") and "forward_meter_usage" not in groups:
                    groups["forward_meter_usage"] = col_idx
                elif cell_str in ("电表用理", "电表用量") and "forward_meter_usage" in groups:
                    groups["reverse_meter_usage"] = col_idx
                elif cell_str == "倍率" and "multiplier" not in groups:
                    groups["multiplier"] = col_idx

        # 如果没找到明确的列标识，尝试通过位置推断
        # 通常正向数据在左，反向数据在右
        if not groups.get("forward_usage") and not groups.get("forward_meter_usage"):
            # 找第一个包含数值的列（跳过类别列）
            for col_idx in range(category_col + 1, max_cols):
                for row_idx in range(min(len(df), 15)):
                    cell = df.iloc[row_idx, col_idx]
                    if cell is not None and not pd.isna(cell):
                        cell_str = str(cell).strip()
                        if cell_str in ("电表用理", "电表用量", "用量"):
                            groups.setdefault("forward_meter_usage", col_idx)
                            break

        log.debug("  转置表列分组: %s", groups)
        return groups

    def _get_transposed_value(self, row, col_idx) -> Optional[float]:
        """从转置表行中提取数值。"""
        if col_idx is None:
            return None
        try:
            val = row.iloc[col_idx]
            if val is None or pd.isna(val):
                return None
            return float(str(val).replace(",", "").strip())
        except (ValueError, TypeError, IndexError):
            return None

    def _try_parse_card_layout(self, df: pd.DataFrame, sheet_name: str,
                                filepath: Path, source_info: dict,
                                meta: dict) -> list[dict]:
        """尝试解析纵向卡片格式（标签-值配对）。

        这种格式常见于"用户表码"等Sheet，支持多组卡片。
        每遇到"用户号"标签开始一组新的卡片。

        支持两种布局：
        - 横向: 标签在A列，值在B列（同行）
        - 纵向: 标签在第N行，值在第N+1行（同列）
        - 内嵌: 标签：值 在同一单元格内
        """
        # 纵向标签关键词映射
        label_map = {
            "用户号": "user_id", "用户编号": "user_id", "户号": "user_id",
            "客户编号": "user_id", "用电户号": "user_id",
            "用户名称": "user_name", "客户名称": "user_name",
            "电表号": "meter_number", "表号": "meter_number",
            "电表编号": "meter_number", "电能表号": "meter_number",
            "发电表号": "gen_meter", "发电表": "gen_meter",
            "发电电表号": "gen_meter", "逆变表号": "gen_meter",
            "上网表号": "grid_meter", "上网表": "grid_meter",
            "上网电表号": "grid_meter", "并网表号": "grid_meter",
            "发电表资产编号": "gen_asset", "发电表资产号": "gen_asset",
            "发电资产号": "gen_asset",
            "上网表资产编号": "grid_asset", "上网表资产号": "grid_asset",
            "上网资产号": "grid_asset",
            "电表资产号": "asset_number", "资产编号": "asset_number",
            "资产号": "asset_number", "核销资产号": "asset_number",
            "倍率": "multiplier", "CT倍率": "multiplier", "变比": "multiplier",
            "统计日期": "reading_date", "抄表日期": "reading_date",
            "用电地址": "address", "安装地址": "address",
            "项目名称": "project_name", "项目": "project_name",
            "电站名称": "project_name",
            "折扣": "discount",
        }

        # 分组分隔标签（遇到这些标签时开始新的一组）
        group_start_fields = {"user_id"}

        # 第一步：扫描所有单元格，提取有序的(标签, 值)流
        ordered_pairs = []  # [(field_name, value_str, row_idx), ...]

        max_rows = min(len(df), 500)
        max_cols = min(len(df.columns), 20)

        # 收集所有标签位置
        label_positions = []  # [(row, col, label_text, field_name)]

        for row_idx in range(max_rows):
            for col_idx in range(max_cols):
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()
                if not cell_str:
                    continue

                # 精确匹配标签
                clean = cell_str.rstrip("：: ")
                if clean in label_map:
                    label_positions.append((row_idx, col_idx, clean, label_map[clean]))
                    continue

                # 内嵌格式 "标签：值"
                for lbl, field in label_map.items():
                    m = re.match(rf'^{re.escape(lbl)}\s*[:：]\s*(.+)$', cell_str)
                    if m:
                        val = m.group(1).strip()
                        if val and len(val) >= 2:
                            ordered_pairs.append((field, val, row_idx))
                        break

        # 检测是否为卡片格式
        label_fields = set(f for _, _, _, f in label_positions)
        meter_fields = {"gen_meter", "grid_meter", "meter_number"}
        has_meter = bool(label_fields & meter_fields)
        if len(label_fields) < 2 or not has_meter:
            return []

        log.info("  检测到纵向卡片格式 [%s]: 字段=%s", sheet_name,
                 set(l for _, _, l, _ in label_positions))

        # 第二步：为每个标签找到对应的值
        for row_idx, col_idx, lbl_text, field in label_positions:
            value = None

            # 策略1 - 横向：同行右侧单元格
            for nc in range(col_idx + 1, min(max_cols, col_idx + 4)):
                val = df.iloc[row_idx, nc]
                if val is not None and not pd.isna(val):
                    vs = str(val).strip()
                    if vs and vs.rstrip("：: ") not in label_map and len(vs) >= 2:
                        value = vs
                        break

            # 策略2 - 纵向：下方单元格
            if not value:
                for nr in range(row_idx + 1, min(max_rows, row_idx + 3)):
                    val = df.iloc[nr, col_idx]
                    if val is not None and not pd.isna(val):
                        vs = str(val).strip()
                        if vs and vs.rstrip("：: ") not in label_map and len(vs) >= 2:
                            value = vs
                            break

            if value:
                ordered_pairs.append((field, value, row_idx))

        # 按行号排序，确保处理顺序正确
        ordered_pairs.sort(key=lambda x: x[2])

        if not ordered_pairs:
            return []

        # 第三步：按"用户号"分组（每个用户号开始一组新卡片）
        groups = []
        current_group = {}

        for field, value, row_idx in ordered_pairs:
            if field in group_start_fields and current_group:
                # 遇到新的用户号，保存当前组，开始新组
                groups.append(current_group)
                current_group = {}
            current_group.setdefault(field, []).append(value)

        if current_group:
            groups.append(current_group)

        # 如果没有 user_id 分组标记，整体作为一组
        if not groups:
            return []

        log.debug("  卡片分组: %d 组", len(groups))

        # 第四步：每组生成电表记录
        results = []
        from src.parsers.validators import validate_record, clean_id

        project_name = meta.get("project_name")
        reading_month = "unknown"
        if meta.get("reading_dates"):
            reading_month = meta["reading_dates"][0]
        if reading_month == "unknown":
            reading_month = self._infer_month(source_info, filepath.name, sheet_name)

        for group in groups:
            g_results = self._build_records_from_card_group(
                group, project_name, reading_month, filepath, sheet_name
            )
            results.extend(g_results)

        if results:
            gen_count = len([r for r in results if r.get("meter_type") == "发电表"])
            grid_count = len([r for r in results if r.get("meter_type") == "上网表"])
            other_count = len(results) - gen_count - grid_count
            log.info("  卡片格式提取 [%s]: %d 条记录 (发电%d/上网%d/其他%d), %d 组用户",
                     sheet_name, len(results), gen_count, grid_count, other_count, len(groups))

        return results

    def _build_records_from_card_group(self, group: dict, project_name: str,
                                        default_month: str, filepath: Path,
                                        sheet_name: str) -> list[dict]:
        """从一组卡片键值对构建电表记录。"""
        from src.parsers.validators import validate_record, clean_id

        results = []

        # 提取公共字段
        user_id = None
        for uid in group.get("user_id", []):
            cleaned = clean_id(uid)
            if cleaned and len(cleaned) >= 6:
                user_id = cleaned
                break

        multiplier = None
        for mv in group.get("multiplier", []):
            try:
                multiplier = float(str(mv).replace(",", ""))
            except (ValueError, TypeError):
                pass

        discount = None
        for dv in group.get("discount", []):
            dm = re.search(r'(\d+\.?\d*)', str(dv))
            if dm:
                d = float(dm.group(1))
                if 0 < d <= 10:
                    discount = d / 10.0 if d > 1 else d

        proj = project_name
        if not proj:
            for pv in group.get("project_name", []):
                proj = pv
                break

        reading_month = default_month
        for dv in group.get("reading_date", []):
            dm = re.search(r'(\d{4})[-/年.]?(\d{1,2})', str(dv))
            if dm:
                y, m = int(dm.group(1)), int(dm.group(2))
                if 2015 <= y <= 2035 and 1 <= m <= 12:
                    reading_month = f"{y}-{str(m).zfill(2)}"
                    break

        base = {
            "user_id": user_id,
            "multiplier": multiplier,
            "discount": discount,
            "project_name": proj,
            "reading_month": reading_month,
            "sharp_peak": None, "peak": None, "flat": None, "valley": None,
            "total_kwh": None,
            "source_file": filepath.name,
            "source_sheet": sheet_name,
        }

        gen_meters = [clean_id(v) for v in group.get("gen_meter", []) if clean_id(v)]
        grid_meters = [clean_id(v) for v in group.get("grid_meter", []) if clean_id(v)]
        gen_assets = [clean_id(v) for v in group.get("gen_asset", []) if clean_id(v)]
        grid_assets = [clean_id(v) for v in group.get("grid_asset", []) if clean_id(v)]
        plain_meters = [clean_id(v) for v in group.get("meter_number", []) if clean_id(v)]
        plain_assets = [clean_id(v) for v in group.get("asset_number", []) if clean_id(v)]
        used_meters = set()

        # 发电表
        for i, gm in enumerate(gen_meters):
            if len(gm) < 6:
                continue
            rec = {**base, "meter_number": gm, "meter_type": "发电表",
                   "asset_number": gen_assets[i] if i < len(gen_assets) else None}
            validated = validate_record(rec)
            if validated:
                results.append(validated)
                used_meters.add(gm)

        # 上网表
        for i, grd in enumerate(grid_meters):
            if len(grd) < 6:
                continue
            rec = {**base, "meter_number": grd, "meter_type": "上网表",
                   "asset_number": grid_assets[i] if i < len(grid_assets) else None}
            validated = validate_record(rec)
            if validated:
                results.append(validated)
                used_meters.add(grd)

        # 普通电表（排除已在发电/上网中出现的）
        for i, pm in enumerate(plain_meters):
            if len(pm) < 6 or pm in used_meters:
                continue
            rec = {**base, "meter_number": pm, "meter_type": "未知",
                   "asset_number": plain_assets[i] if i < len(plain_assets) else None}
            validated = validate_record(rec)
            if validated:
                results.append(validated)

        return results

    def _disambiguate_headers(self, headers: list[str]) -> list[str]:
        """消歧重复列名：根据位置上下文（正向有功/反向有功）前缀化。

        例如：
            [..., "正向有功总(kWh)", "尖(kWh)", "峰(kWh)", "平(kWh)", "谷(kWh)",
             "反向有功总(kWh)", "尖(kWh)", "峰(kWh)", "平(kWh)", "谷(kWh)"]
        变为：
            [..., "正向有功总(kWh)", "正_尖(kWh)", "正_峰(kWh)", "正_平(kWh)", "正_谷(kWh)",
             "反向有功总(kWh)", "反_尖(kWh)", "反_峰(kWh)", "反_平(kWh)", "反_谷(kWh)"]
        """
        result = list(headers)
        context = None  # "正" or "反"

        # 短列名模式：尖, 峰, 平, 谷 (可能带单位)
        short_reading_re = re.compile(r'^[尖峰平谷](?:\s*[\(（].*[\)）])?$')

        for i, h in enumerate(result):
            if not h:
                continue

            # 检测方向标记列
            if any(kw in h for kw in ["正向有功", "正向总", "正有功"]):
                context = "正"
                continue
            elif any(kw in h for kw in ["反向有功", "反向总", "反有功"]):
                context = "反"
                continue

            # 如果遇到不相关的列（不是尖峰平谷），重置上下文
            if context and not short_reading_re.match(h):
                # 检查是不是还在当前区段内（如 "总" 列之后紧跟 尖峰平谷）
                # 不重置，因为可能有总列后面跟着子列
                # 只有遇到完全不相关的列才重置
                if not any(kw in h for kw in ["总", "合计", "有功", "无功"]):
                    context = None
                continue

            # 对短列名添加方向前缀
            if context and short_reading_re.match(h):
                result[i] = f"{context}_{h}"

        # 确保没有重复列名（万一消歧不够，加数字后缀）
        seen = {}
        for i, h in enumerate(result):
            if h in seen:
                seen[h] += 1
                result[i] = f"{h}_{seen[h]}"
            else:
                seen[h] = 0

        return result

    def _scan_meta_info(self, df: pd.DataFrame, sheet_name: str, filepath: Path) -> dict:
        """全表扫描提取元信息：用户编号、折扣、配对电表号等。"""
        meta = {
            "user_ids": [],
            "discount": None,
            "grid_meters": {},
            "gen_meters": {},
            "asset_numbers": {},
            "prices": {},
            "paired_meters": [],
            "project_name": None,
            "reading_dates": [],
        }

        discount_patterns = self.reconciliation_cfg.get("discount_patterns", [])

        for row_idx in range(min(len(df), 50)):
            for col_idx in range(min(len(df.columns), 30)):
                cell = df.iloc[row_idx, col_idx]
                if cell is None or pd.isna(cell):
                    continue
                cell_str = str(cell).strip()
                if not cell_str:
                    continue

                # 提取用户编号
                for kw in ["用户号", "用户编号", "用电户号", "户号", "Account"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{8,20})', cell_str)
                        for n in nums:
                            if n not in meta["user_ids"]:
                                meta["user_ids"].append(n)
                                log.debug("    扫描到用户编号: %s (行%d)", n, row_idx)

                # 独立数字单元格可能是用户编号
                if re.match(r'^\d{10,20}$', cell_str):
                    if not re.match(r'^20\d{2}(0[1-9]|1[0-2])', cell_str):
                        if cell_str not in meta["user_ids"]:
                            row_text = " ".join(str(df.iloc[row_idx, c]) for c in range(min(len(df.columns), 30))
                                                if df.iloc[row_idx, c] is not None and not pd.isna(df.iloc[row_idx, c]))
                            if any(kw in row_text for kw in ["用户", "户号", "编号", "上网", "发电", "资产"]):
                                meta["user_ids"].append(cell_str)

                # 提取日期（统计日期等）
                date_match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', cell_str)
                if date_match:
                    year, month = int(date_match.group(1)), int(date_match.group(2))
                    if 2015 <= year <= 2035 and 1 <= month <= 12:
                        date_str = f"{year}-{str(month).zfill(2)}"
                        if date_str not in meta["reading_dates"]:
                            meta["reading_dates"].append(date_str)

                # 提取折扣
                for pattern in discount_patterns:
                    m = re.search(pattern, cell_str)
                    if m:
                        try:
                            d = float(m.group(1))
                            if 0 < d <= 10:
                                meta["discount"] = d / 10.0 if d > 1 else d
                                log.debug("    扫描到折扣: %s -> %.2f (行%d)", cell_str, meta["discount"], row_idx)
                        except ValueError:
                            pass

                # 提取配对电表号（注释标签中）
                for kw in ["上网表", "上网电表", "并网表"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{6,20})', cell_str)
                        for n in nums:
                            meta["grid_meters"][n] = row_idx

                for kw in ["发电表", "发电电表", "逆变表"]:
                    if kw in cell_str:
                        nums = re.findall(r'(\d{6,20})', cell_str)
                        for n in nums:
                            meta["gen_meters"][n] = row_idx

                for kw in ["资产编号", "资产号", "资产表"]:
                    if kw in cell_str:
                        nums = re.findall(r'[A-Za-z0-9]{10,30}', cell_str)
                        for n in nums:
                            meta["asset_numbers"][n] = row_idx

                # 从标题行提取项目名（前5行）
                if row_idx < 5:
                    proj = self._extract_project_from_text(cell_str)
                    if proj:
                        meta["project_name"] = proj

        # 配对关系
        gen_by_row = {}
        for meter, row in meta["gen_meters"].items():
            gen_by_row.setdefault(row, []).append(meter)
        grid_by_row = {}
        for meter, row in meta["grid_meters"].items():
            grid_by_row.setdefault(row, []).append(meter)

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

        # 额外关键词：表头中常见的方向标签和通用字段
        all_keywords.update([
            "正向尖", "正向峰", "正向平", "正向谷",
            "反向尖", "反向峰", "反向平", "反向谷",
            "上月表数", "本月表数", "电表用量",
            "上网表号", "发电表号", "用电量", "发电量",
            # 用户表码格式的关键词
            "用户编号", "用户名称", "电表资产号", "统计日期",
            "正向有功", "反向有功", "正向有功总", "反向有功总",
            "尖", "峰", "平", "谷",  # 独立的尖峰平谷列
            "用户类型", "用电地址", "抄表编号", "遥测",
            "核销资产", "kWh",
        ])

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

        # 判断数据模式：单电表 or 配对电表（发电表+上网表在同一行）
        has_primary = bool(col_map.get("meter_number"))
        has_gen = bool(col_map.get("gen_meter_number"))
        has_grid = bool(col_map.get("grid_meter_number"))
        has_asset = bool(col_map.get("asset_number"))

        # 如果没有 meter_number 但有 asset_number → 用 asset_number 作为 meter_number
        if not has_primary and has_asset and not has_gen and not has_grid:
            col_map["meter_number"] = col_map["asset_number"]
            has_primary = True
            log.info("  使用 asset_number 列 '%s' 作为 meter_number", col_map["asset_number"])

        has_paired = has_gen or has_grid

        # 如果没有任何电表号列 → 尝试自动检测
        if not has_primary and not has_paired:
            auto_col = self._auto_detect_meter_column(df)
            if auto_col is not None:
                col_map["meter_number"] = auto_col
                has_primary = True
                log.info("  自动检测到电表号列: '%s'", auto_col)
            else:
                # 如果 meta 里有电表号，作为全表共用
                if meta and (meta.get("gen_meters") or meta.get("grid_meters")):
                    log.info("  Sheet '%s' 无电表号列，但有元信息电表号，尝试用元信息补充", sheet_name)
                else:
                    log.info("  Sheet '%s' 缺少电表号列，跳过 (映射: %s)", sheet_name, col_map)
                    return []

        log.info("  列映射: %s", col_map)
        log.info("  模式: primary=%s, gen=%s, grid=%s, asset_as_meter=%s",
                   has_primary, has_gen, has_grid,
                   has_asset and col_map.get("meter_number") == col_map.get("asset_number"))

        for idx, row in df.iterrows():
            try:
                records = self._extract_records(row, col_map, sheet_name, filepath,
                                                 source_info, meta,
                                                 has_primary, has_gen, has_grid)
                results.extend(records)
            except Exception as e:
                log.debug("  行 %d 提取失败: %s", idx, e)

        # 用元信息补全
        self._enrich_with_meta(results, meta)

        return results

    def _auto_detect_meter_column(self, df: pd.DataFrame) -> Optional[str]:
        """自动检测哪一列可能是电表号（当配置的别名匹配不到时）。

        扫描数据行，找包含≥6位纯数字且长度合理的列。
        """
        import re
        candidates = {}  # col_name -> count_of_valid_values

        for col in df.columns:
            count = 0
            for idx in range(min(len(df), 15)):
                val = df.iloc[idx][col]
                if val is None or pd.isna(val):
                    continue
                val_str = str(val).strip()
                # 去除 .0 后缀
                val_str = re.sub(r'\.0+$', '', val_str)
                # 电表号特征：6-20位纯数字或字母数字
                if re.match(r'^[0-9A-Za-z\-\.]{6,20}$', val_str) and not re.search(r'[\u4e00-\u9fff]', val_str):
                    # 排除像日期的（2026-01）
                    if re.match(r'^\d{4}-\d{2}$', val_str):
                        continue
                    count += 1
            if count >= 2:
                candidates[col] = count

        if candidates:
            # 选匹配数最多的列
            best = max(candidates, key=candidates.get)
            log.info("  自动检测电表号列候选: %s", {str(k): v for k, v in candidates.items()})
            return best
        return None

    def _map_columns(self, columns: list[str]) -> dict:
        """将 DataFrame 列名映射到标准字段名。

        两轮匹配策略：
        1. 全部字段先做精确匹配（防止子串抢占，如 "电表资产号" 被 "电表号" 抢走）
        2. 未匹配的字段再做子串匹配

        映射顺序：特殊字段(gen/grid) → asset_number → meter_number → 其他
        所有字段都检查 used_cols 防止一列被多个字段占用。
        """
        col_map = {}
        used_cols = set()  # 已占用的列名

        # 字段映射顺序：asset_number 在 meter_number 之前，
        # 确保 "电表资产号" 优先精确匹配到 asset_number
        all_fields = [
            "gen_meter_number", "grid_meter_number",
            "asset_number", "meter_number",
            "user_id", "multiplier",
            "project_name", "discount", "unit_price", "amount",
            "forward_total", "reverse_total",
            "usage", "reading_date", "user_name", "address",
        ]

        # 第一轮：所有字段做精确匹配
        for field in all_fields:
            if field in col_map:
                continue
            aliases = self.field_mapping.get(field, [])
            if not isinstance(aliases, list):
                continue
            for col in columns:
                if col in used_cols:
                    continue
                col_clean = str(col).strip()
                if col_clean in aliases:
                    col_map[field] = col
                    used_cols.add(col)
                    break

        # 第二轮：未匹配的字段做子串匹配
        for field in all_fields:
            if field in col_map:
                continue
            aliases = self.field_mapping.get(field, [])
            if not isinstance(aliases, list):
                continue
            for col in columns:
                if col in used_cols:
                    continue
                col_clean = str(col).strip()
                if any(a in col_clean for a in aliases):
                    col_map[field] = col
                    used_cols.add(col)
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

        # 额外检测
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

    def _extract_records(self, row, col_map: dict, sheet_name: str,
                         filepath: Path, source_info: dict,
                         meta: dict, has_primary: bool,
                         has_gen: bool, has_grid: bool) -> list[dict]:
        """从单行提取电表记录。配对模式下返回2条（发电表+上网表）。"""

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

        # 检查汇总行
        first_cells = [str(row.iloc[i]).strip() if i < len(row) and not pd.isna(row.iloc[i]) else ""
                       for i in range(min(3, len(row)))]
        summary_keywords = {"合计", "总计", "小计", "总合计", "汇总", "合 计", "总 计"}
        if any(kw in cell for cell in first_cells for kw in summary_keywords):
            return []

        # 公共字段：优先从数据行内部获取月份（不依赖文件名）
        reading_month = self._extract_month_from_row(row, col_map, meta)
        if not reading_month:
            reading_month = self._infer_month(source_info, filepath.name, sheet_name)

        project_name = str(get_val("project_name") or "").strip() or None
        if not project_name and meta:
            project_name = meta.get("project_name")
        if not project_name:
            project_name = self._infer_project(filepath.name, sheet_name)

        discount = get_float("discount")
        if discount is None and meta:
            discount = meta.get("discount")

        unit_price = get_float("unit_price")
        multiplier = get_float("multiplier")
        asset_number = get_val("asset_number")
        user_id = get_val("user_id")
        if not user_id and meta and meta.get("user_ids"):
            user_id = meta["user_ids"][0]

        total_kwh = get_float("usage")
        fwd_total = get_float("forward_total")
        rev_total = get_float("reverse_total")

        # 从上月/本月表数计算用电量（如果 usage/forward_total 缺失）
        prev_reading = get_float("prev_reading")
        curr_reading = get_float("curr_reading")
        if prev_reading is not None and curr_reading is not None:
            calc_usage = curr_reading - prev_reading
            if calc_usage >= 0:
                if total_kwh is None and fwd_total is None:
                    total_kwh = calc_usage
                    log.debug("  从表数差值计算用电量: %.2f - %.2f = %.2f",
                              curr_reading, prev_reading, calc_usage)

        # 读取正向和反向表码数据
        fwd = {
            "sharp_peak": get_float("forward_readings_sharp") or get_float("forward_readings_sharp_peak"),
            "peak": get_float("forward_readings_peak"),
            "flat": get_float("forward_readings_flat"),
            "valley": get_float("forward_readings_valley"),
        }
        rev = {
            "sharp_peak": get_float("reverse_readings_sharp_peak"),
            "peak": get_float("reverse_readings_peak"),
            "flat": get_float("reverse_readings_flat"),
            "valley": get_float("reverse_readings_valley"),
        }
        fwd_has_data = any(v is not None for v in fwd.values())
        rev_has_data = any(v is not None for v in rev.values())

        # 计算总电量（从分项累加）
        if fwd_has_data and fwd_total is None:
            fwd_total = sum(v for v in fwd.values() if v is not None)
        if rev_has_data and rev_total is None:
            rev_total = sum(v for v in rev.values() if v is not None)

        results = []

        # ========== 配对模式：同一行有发电表号+上网表号 ==========
        gen_meter = clean_id(get_val("gen_meter_number"))
        grid_meter = clean_id(get_val("grid_meter_number"))

        if has_primary:
            # 有统一电表号列的模式
            meter_number = get_val("meter_number")
            # 从 "用户类型" / "类别" 列检测（优先级最高，因为是行级信息）
            category = str(get_val("category") or "")
            meter_type = self._detect_type_from_category(category, "")
            if meter_type == "未知":
                meter_type = self._detect_meter_type(sheet_name, filepath.name, col_map)
            # 从行内容检测类型
            if meter_type == "未知":
                row_text = " ".join(str(v) for v in first_cells)
                if "上网" in row_text:
                    meter_type = "上网表"
                elif "发电" in row_text:
                    meter_type = "发电表"

            # 如果这行同时有正向和反向数据（单表双向场景），
            # 优先用正向数据（发电），反向数据会在后面单独处理
            if fwd_has_data and rev_has_data and not has_gen and not has_grid:
                # 单个电表有双向数据：创建一条记录包含所有数据
                # total_kwh 取正向总
                record = {
                    "meter_number": meter_number,
                    "asset_number": asset_number,
                    "user_id": user_id,
                    "meter_type": meter_type,
                    "multiplier": multiplier,
                    "discount": discount,
                    "project_name": project_name,
                    "reading_month": reading_month,
                    "sharp_peak": fwd["sharp_peak"],
                    "peak": fwd["peak"],
                    "flat": fwd["flat"],
                    "valley": fwd["valley"],
                    "total_kwh": fwd_total or total_kwh,
                    "unit_price": unit_price,
                    "grid_meter_number": grid_meter,
                    "gen_meter_number": gen_meter,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                    # 额外存储反向数据
                    "reverse_sharp_peak": rev["sharp_peak"],
                    "reverse_peak": rev["peak"],
                    "reverse_flat": rev["flat"],
                    "reverse_valley": rev["valley"],
                    "reverse_total_kwh": rev_total,
                }
                validated = validate_record(record)
                if validated:
                    results.append(validated)
            else:
                # 标准单向模式
                if meter_type == "上网表" and rev_has_data:
                    readings = rev
                    readings_total = rev_total
                elif meter_type == "发电表" and fwd_has_data:
                    readings = fwd
                    readings_total = fwd_total
                elif fwd_has_data:
                    readings = fwd
                    readings_total = fwd_total
                elif rev_has_data:
                    readings = rev
                    readings_total = rev_total
                else:
                    readings = {"sharp_peak": None, "peak": None, "flat": None, "valley": None}
                    readings_total = None

                record = {
                    "meter_number": meter_number,
                    "asset_number": asset_number,
                    "user_id": user_id,
                    "meter_type": meter_type,
                    "multiplier": multiplier,
                    "discount": discount,
                    "project_name": project_name,
                    "reading_month": reading_month,
                    "sharp_peak": readings["sharp_peak"],
                    "peak": readings["peak"],
                    "flat": readings["flat"],
                    "valley": readings["valley"],
                    "total_kwh": readings_total or total_kwh,
                    "unit_price": unit_price,
                    "grid_meter_number": grid_meter,
                    "gen_meter_number": gen_meter,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                }
                validated = validate_record(record)
                if validated:
                    results.append(validated)

        # ========== 发电表记录（从 gen_meter_number 列）==========
        if has_gen and gen_meter and len(gen_meter) >= 6:
            # 如果 primary 已经用了同一个号，跳过
            primary_num = clean_id(get_val("meter_number")) if has_primary else ""
            if gen_meter != primary_num:
                gen_record = {
                    "meter_number": gen_meter,
                    "asset_number": asset_number,
                    "user_id": user_id,
                    "meter_type": "发电表",
                    "multiplier": multiplier,
                    "discount": discount,
                    "project_name": project_name,
                    "reading_month": reading_month,
                    "sharp_peak": fwd["sharp_peak"] if fwd_has_data else None,
                    "peak": fwd["peak"] if fwd_has_data else None,
                    "flat": fwd["flat"] if fwd_has_data else None,
                    "valley": fwd["valley"] if fwd_has_data else None,
                    "total_kwh": fwd_total,
                    "unit_price": unit_price,
                    "grid_meter_number": grid_meter,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                }
                validated = validate_record(gen_record)
                if validated:
                    results.append(validated)

        # ========== 上网表记录（从 grid_meter_number 列）==========
        if has_grid and grid_meter and len(grid_meter) >= 6:
            primary_num = clean_id(get_val("meter_number")) if has_primary else ""
            if grid_meter != primary_num:
                grid_record = {
                    "meter_number": grid_meter,
                    "asset_number": None,
                    "user_id": user_id,
                    "meter_type": "上网表",
                    "multiplier": multiplier,
                    "discount": discount,
                    "project_name": project_name,
                    "reading_month": reading_month,
                    "sharp_peak": rev["sharp_peak"] if rev_has_data else None,
                    "peak": rev["peak"] if rev_has_data else None,
                    "flat": rev["flat"] if rev_has_data else None,
                    "valley": rev["valley"] if rev_has_data else None,
                    "total_kwh": rev_total,
                    "unit_price": unit_price,
                    "gen_meter_number": gen_meter,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                }
                validated = validate_record(grid_record)
                if validated:
                    results.append(validated)

        # ========== 兜底：没有 primary 但有 gen/grid ==========
        if not has_primary and not results:
            # 尝试用第一个可用的电表号
            meter_num = gen_meter or grid_meter
            mtype = "发电表" if meter_num == gen_meter else "上网表"
            readings = fwd if mtype == "发电表" else rev
            if not any(v is not None for v in readings.values()):
                readings = rev if mtype == "发电表" else fwd

            if meter_num:
                fallback = {
                    "meter_number": meter_num,
                    "asset_number": asset_number,
                    "user_id": user_id,
                    "meter_type": mtype,
                    "multiplier": multiplier,
                    "discount": discount,
                    "project_name": project_name,
                    "reading_month": reading_month,
                    "sharp_peak": readings.get("sharp_peak"),
                    "peak": readings.get("peak"),
                    "flat": readings.get("flat"),
                    "valley": readings.get("valley"),
                    "total_kwh": total_kwh,
                    "unit_price": unit_price,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                }
                validated = validate_record(fallback)
                if validated:
                    results.append(validated)

        return results

    def _extract_month_from_row(self, row, col_map: dict, meta: dict) -> Optional[str]:
        """从行内部数据提取月份，优先使用文件内的日期字段。"""
        # 1. 尝试从 reading_date 列获取
        col = col_map.get("reading_date")
        if col is not None:
            val = row.get(col)
            if val is not None and not pd.isna(val):
                val_str = str(val).strip()
                # 尝试解析日期格式: 2026-02-01, 2026/02/01, 20260201
                for pattern in [
                    r'(\d{4})[-/](\d{1,2})[-/]\d{1,2}',
                    r'(\d{4})(\d{2})\d{2}',
                    r'(\d{4})[-/](\d{1,2})',
                ]:
                    m = re.match(pattern, val_str)
                    if m:
                        year, month = int(m.group(1)), int(m.group(2))
                        if 2015 <= year <= 2035 and 1 <= month <= 12:
                            return f"{year}-{str(month).zfill(2)}"

                # pandas Timestamp
                if hasattr(val, 'year') and hasattr(val, 'month'):
                    try:
                        return f"{val.year}-{str(val.month).zfill(2)}"
                    except Exception:
                        pass

        # 2. 尝试从元信息中获取扫描到的日期
        if meta and meta.get("reading_dates"):
            return meta["reading_dates"][0]

        return None

    def _enrich_with_meta(self, records: list[dict], meta: dict):
        """用元信息补全记录中的缺失字段。"""
        if not meta:
            return

        for rec in records:
            if not rec.get("user_id") and meta.get("user_ids"):
                rec["user_id"] = meta["user_ids"][0]

            if not rec.get("discount") and meta.get("discount"):
                rec["discount"] = meta["discount"]

            if not rec.get("project_name") and meta.get("project_name"):
                rec["project_name"] = meta["project_name"]

            meter_num = rec.get("meter_number")
            if meter_num:
                for gen, grid in meta.get("paired_meters", []):
                    if meter_num == gen:
                        rec.setdefault("grid_meter_number", grid)
                    elif meter_num == grid:
                        rec.setdefault("gen_meter_number", gen)

    def _extract_project_from_text(self, text: str) -> Optional[str]:
        """从文本中提取项目名称，去除月份前缀等噪声。"""
        if not text or len(text) < 4:
            return None

        # 先去除常见的月份前缀：如 "1月深圳市xxx" → "深圳市xxx"
        # 或 "2026年1月xxx" → "xxx"
        text_clean = re.sub(r'^\d{4}[-年]\d{1,2}[-月]?\s*', '', text)
        text_clean = re.sub(r'^\d{1,2}[-月]\s*', '', text_clean)

        # 项目名模式（按优先级）
        patterns = [
            # 带光伏/电站/工业园等关键词的名称
            r'([\u4e00-\u9fff]{2,}(?:光伏|工业园|产业园|电站)[\u4e00-\u9fff]*?)(?:项目|统计|电费|汇总|分配|明细)',
            r'([\u4e00-\u9fff]{2,}(?:光伏|工业园|产业园|电站)[\u4e00-\u9fff]*)',
            # "xxx项目" 格式
            r'([\u4e00-\u9fff]{2,})(?:项目)',
            # 公司名称
            r'([\u4e00-\u9fff]{2,}(?:有限公司|有限责任公司|集团|股份))',
            # 深圳市xxx公司
            r'((?:深圳|广州|东莞|佛山|惠州)[\u4e00-\u9fff]{2,}(?:公司|工厂|厂))',
        ]

        for pattern in patterns:
            m = re.search(pattern, text_clean)
            if m:
                proj = m.group(1).strip()
                # 去除尾缀
                proj = re.sub(r'(?:统计表|分配表|汇总表|电费表|明细表|电费单|项目发电|发电)$', '', proj)
                # 验证：至少2个字，不能以"月"开头
                if len(proj) >= 2 and not proj.startswith("月"):
                    return proj

        return None

    def _infer_project(self, filename: str, sheet_name: str) -> Optional[str]:
        """从文件名或 Sheet 名推断项目名称。"""
        for text in [filename, sheet_name]:
            proj = self._extract_project_from_text(text)
            if proj:
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

    def _detect_type_from_category(self, category: str, row_text: str) -> str:
        """从用户类型/类别/方向字段智能判断电表类型。"""
        text = f"{category} {row_text}"
        # 光伏发电客户 / 发电 / 正向 → 发电表
        if any(kw in text for kw in ["光伏发电", "发电客户", "发电户", "逆变"]):
            return "发电表"
        # 地方电厂户 / 上网 / 反向 / 关口 → 上网表
        if any(kw in text for kw in ["地方电厂", "电厂户", "上网", "并网", "关口"]):
            return "上网表"
        # 方向标记
        if "正向" in text and "反向" not in text:
            return "发电表"
        if "反向" in text and "正向" not in text:
            return "上网表"
        # 公变/专变客户
        if any(kw in text for kw in ["公变客户", "专变客户", "公变", "专变"]):
            return "未知"  # 无法确定
        return "未知"

    def _infer_month(self, source_info: dict, filename: str, sheet_name: str) -> str:
        """从文件名、Sheet 名或邮件日期推断数据所属月份（兜底方案）。"""
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
