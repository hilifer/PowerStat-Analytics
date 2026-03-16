"""多轮扫描提取器：笨方法版。

每轮单独扫描所有文件的所有单元格，只关注一种数据：
  第1轮：提取电表号 + 资产编号（同行直接关联）
  第2轮：提取用户编号，关联到最近的电表
  第3轮：判定电表类型
  第4轮：提取倍率、折扣、项目名
  合并短电表号
  第5轮：提取月度读数（尖峰平谷）

固定数据全部找完，再找动态数据。文件全部在内存中，不会反复打开文件。
"""

import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config_loader import config
from src.logger import log
from src.parsers.validators import clean_id, is_valid_meter_number


# ========== 工具函数 ==========
_CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')
_DATE_RE = re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{4}[-/]\d{1,2}$|^\d{8}$')
_MONTH_RE = re.compile(r'(\d{4})\s*[-年/]\s*(\d{1,2})\s*月?')


def _cell_str(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if s.lower() in ("none", "nan", "null", ""):
        return ""
    return re.sub(r'\.0+$', '', s)


def _to_float(val) -> Optional[float]:
    s = _cell_str(val)
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _is_meter_like(s: str) -> bool:
    if not s or len(s) < 8 or len(s) > 16:
        return False
    if _CHINESE_RE.search(s) or _DATE_RE.match(s):
        return False
    return bool(re.match(r'^\d+$', s))


class MultiPassExtractor:
    """多轮扫描提取器（笨方法版）。"""

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}

        # 从 config 收集所有别名
        self._meter_aliases = set(self.field_mapping.get("meter_number", []))
        self._asset_aliases = set(self.field_mapping.get("asset_number", []))
        self._user_aliases = set(self.field_mapping.get("user_id", []))
        self._gen_aliases = set(self.field_mapping.get("gen_meter_number", []))
        self._grid_aliases = set(self.field_mapping.get("grid_meter_number", []))
        self._multiplier_aliases = set(self.field_mapping.get("multiplier", []))
        self._discount_aliases = set(self.field_mapping.get("discount", []))
        self._project_aliases = set(self.field_mapping.get("project_name", []))
        self._usage_aliases = set(self.field_mapping.get("usage", []))
        self._fwd_total_aliases = set(self.field_mapping.get("forward_total", []))
        self._rev_total_aliases = set(self.field_mapping.get("reverse_total", []))
        self._date_aliases = set(self.field_mapping.get("reading_date", []))
        self._fwd_readings_cfg = self.field_mapping.get("forward_readings", {})
        self._rev_readings_cfg = self.field_mapping.get("reverse_readings", {})

        self._gen_keywords = set(self.meter_type_rules.get("generation_meter", {}).get("keywords", []))
        self._grid_keywords = set(self.meter_type_rules.get("grid_meter", {}).get("keywords", []))

        self._sheets = []
        self.meters = {}     # meter_number -> {asset, user_id, type, ...}
        self.readings = {}   # (meter_number, month) -> {sharp_peak, peak, flat, valley, total}

    def load_dataframes(self, sheets: list):
        self._sheets = sheets
        log.info("多轮扫描: 加载 %d 个 sheet", len(sheets))

    def extract_all(self) -> list[dict]:
        log.info("=" * 50)
        log.info("开始多轮扫描提取（笨方法）")
        log.info("=" * 50)

        # === 固定数据 ===
        self._pass1_meters_and_assets()
        self._pass2_user_ids()
        self._pass3_meter_types()
        self._pass4_fixed_attrs()

        # 合并短电表号
        self._merge_short_meters()

        # 打印电表档案
        log.info("电表档案建立完成: %d 个电表", len(self.meters))
        for mn, info in self.meters.items():
            log.info("  %s | 类型=%s | 资产=%s | 用户=%s | 倍率=%s | 项目=%s",
                     mn, info.get("meter_type"), info.get("asset_number"),
                     info.get("user_id"), info.get("multiplier"), info.get("project_name"))

        # === 动态数据 ===
        self._pass5_readings()
        log.info("读数提取完成: %d 条记录", len(self.readings))

        return self._build_records()

    # ================================================================
    # 第1轮：提取电表号 + 资产编号
    # ================================================================

    def _pass1_meters_and_assets(self):
        """扫描所有文件，找电表号和资产编号（同行直接关联）。"""
        log.info("[第1轮] 扫描电表号 + 资产编号...")
        asset_count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # A. 表头列中找电表号列和资产编号列
            meter_cols = []  # [(col_idx, meter_type)]
            asset_cols = []
            has_meter_col = False

            if header_idx is not None:
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if not hdr:
                        continue

                    # 先判断资产列（"发电表资产编号"含"发电表"会误匹配电表列）
                    if self._matches_any(hdr, self._asset_aliases):
                        asset_cols.append(c)
                    elif self._matches_any(hdr, self._gen_aliases):
                        meter_cols.append((c, "发电表"))
                        has_meter_col = True
                    elif self._matches_any(hdr, self._grid_aliases):
                        meter_cols.append((c, "上网表"))
                        has_meter_col = True
                    elif self._matches_any(hdr, self._meter_aliases):
                        meter_cols.append((c, None))
                        has_meter_col = True

                # 从电表号列提取电表，同时同行关联最近的资产编号列
                for mc, mtype in meter_cols:
                    # 找距离此电表列最近的资产列（优先右侧相邻列，表头通常是 电表号|资产编号）
                    nearest_ac = min(asset_cols, key=lambda ac: (abs(ac - mc), -ac)) if asset_cols else None
                    for r in range(header_idx + 1, len(df)):
                        val = clean_id(_cell_str(df.iloc[r, mc]))
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, mtype)
                            # 同行最近资产编号
                            if nearest_ac is not None:
                                av = clean_id(_cell_str(df.iloc[r, nearest_ac]))
                                if av and len(av) >= 4 and not _CHINESE_RE.search(av):
                                    if not self.meters[val]["asset_number"]:
                                        self.meters[val]["asset_number"] = av
                                        asset_count += 1

                # 无电表号列时，资产编号列当电表号
                if not has_meter_col:
                    for ac in asset_cols:
                        for r in range(header_idx + 1, len(df)):
                            val = clean_id(_cell_str(df.iloc[r, ac]))
                            if is_valid_meter_number(val):
                                self._register_meter(val, filepath.name, sheet_name)

            # B. 全表扫描：标签 + 值 配对 和 内嵌格式
            for r in range(len(df)):
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell:
                        continue

                    # 资产编号标签配对（必须先于电表判断，否则"发电表资产编号"会匹配"发电表"）
                    if self._matches_any(cell, self._asset_aliases):
                        val = clean_id(self._find_value_near(df, r, c))
                        if val and len(val) >= 4 and not _CHINESE_RE.search(val):
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["asset_number"]:
                                self.meters[nearest]["asset_number"] = val
                                asset_count += 1

                    # 电表号标签配对
                    elif self._matches_any(cell, self._meter_aliases):
                        val = clean_id(self._find_value_near(df, r, c))
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name)
                    elif self._matches_any(cell, self._gen_aliases):
                        val = clean_id(self._find_value_near(df, r, c))
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, "发电表")
                    elif self._matches_any(cell, self._grid_aliases):
                        val = clean_id(self._find_value_near(df, r, c))
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, "上网表")

                    # 内嵌格式："电表号：12345678"
                    for pattern, mtype in [
                        (r'(?:电表号|表号|电能表号|表计编号)\s*[:：]\s*(\d{8,16})', None),
                        (r'(?:发电表号?|发电电表号?)\s*[:：]\s*(\d{8,16})', "发电表"),
                        (r'(?:上网表号?|上网电表号?)\s*[:：]\s*(\d{8,16})', "上网表"),
                    ]:
                        m = re.search(pattern, cell)
                        if m:
                            val = clean_id(m.group(1))
                            if is_valid_meter_number(val):
                                self._register_meter(val, filepath.name, sheet_name, mtype)

        log.info("  找到 %d 个电表, 关联 %d 个资产编号", len(self.meters), asset_count)

    # ================================================================
    # 第2轮：提取用户编号，关联到最近电表
    # ================================================================

    def _pass2_user_ids(self):
        """扫描所有文件，找用户编号并关联到电表。"""
        log.info("[第2轮] 扫描用户编号...")
        count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)
            sheet_users = set()

            # A. 表头列同行关联
            if header_idx is not None:
                meter_cols, user_cols = [], []
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if not hdr:
                        continue
                    if self._matches_any(hdr, self._meter_aliases | self._gen_aliases | self._grid_aliases):
                        meter_cols.append(c)
                    if self._matches_any(hdr, self._user_aliases):
                        user_cols.append(c)

                if meter_cols and user_cols:
                    for r in range(header_idx + 1, len(df)):
                        mn = self._find_meter_in_row(df, r, meter_cols)
                        if not mn:
                            continue
                        for uc in user_cols:
                            uv = clean_id(_cell_str(df.iloc[r, uc]))
                            if uv and len(uv) >= 6 and re.match(r'^\d+$', uv):
                                if not self.meters[mn]["user_id"]:
                                    self.meters[mn]["user_id"] = uv
                                    count += 1
                                sheet_users.add(uv)
                                break

            # B. 标签配对
            for r in range(len(df)):
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell or not self._matches_any(cell, self._user_aliases):
                        continue
                    val = clean_id(self._find_value_near(df, r, c))
                    if val and len(val) >= 6 and re.match(r'^\d+$', val):
                        sheet_users.add(val)
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest and not self.meters[nearest]["user_id"]:
                            self.meters[nearest]["user_id"] = val
                            count += 1

                    # 内嵌格式
                    m = re.search(r'(?:用户编号|用户号|户号|用电户号)\s*[:：]\s*(\d{6,20})', cell)
                    if m:
                        uid = m.group(1)
                        sheet_users.add(uid)
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest and not self.meters[nearest]["user_id"]:
                            self.meters[nearest]["user_id"] = uid
                            count += 1

            # C. 单用户 sheet → 关联给所有本文件电表
            if len(sheet_users) == 1:
                uid = sheet_users.pop()
                for mn, info in self.meters.items():
                    if info["source_file"] == filepath.name and not info["user_id"]:
                        info["user_id"] = uid
                        count += 1

        # D. 同用户电表互补
        by_user = {}
        for mn, info in self.meters.items():
            uid = info.get("user_id")
            if uid:
                by_user.setdefault(uid, []).append(mn)

        log.info("  关联 %d 个用户编号", count)

    # ================================================================
    # 第3轮：判定电表类型
    # ================================================================

    def _pass3_meter_types(self):
        """扫描所有文件，判定电表类型。"""
        log.info("[第3轮] 判定电表类型...")
        count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # A. 表头列中的类型列同行关联
            if header_idx is not None:
                meter_cols, type_cols = [], []
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if not hdr:
                        continue
                    if self._matches_any(hdr, self._meter_aliases | self._gen_aliases | self._grid_aliases):
                        meter_cols.append(c)
                    if "用户类型" in hdr or "类别" in hdr:
                        type_cols.append(c)

                if meter_cols and type_cols:
                    for r in range(header_idx + 1, len(df)):
                        mn = self._find_meter_in_row(df, r, meter_cols)
                        if not mn or self.meters[mn]["meter_type"] != "未知":
                            continue
                        for tc in type_cols:
                            tv = _cell_str(df.iloc[r, tc])
                            mtype = self._detect_type_from_text(tv)
                            if mtype != "未知":
                                self.meters[mn]["meter_type"] = mtype
                                count += 1
                                break

            # B. 列结构推断（正向/反向列）
            headers = {}
            if header_idx is not None:
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if hdr:
                        headers[c] = hdr

            has_fwd = any(
                self._matches_any(h, self._fwd_total_aliases) or
                any(self._matches_any(h, als) for als in self._fwd_readings_cfg.values()
                    if isinstance(als, list))
                for h in headers.values()
            )
            has_rev = any(
                self._matches_any(h, self._rev_total_aliases) or
                any(self._matches_any(h, als) for als in self._rev_readings_cfg.values()
                    if isinstance(als, list))
                for h in headers.values()
            )

            # C. 文件名/sheet名推断
            context = f"{filepath.name} {sheet_name}"
            ctx_type = self._detect_type_from_text(context)

            for mn, info in self.meters.items():
                if info["source_file"] != filepath.name:
                    continue
                if info["meter_type"] != "未知":
                    continue

                if ctx_type != "未知":
                    info["meter_type"] = ctx_type
                    count += 1
                elif has_fwd and not has_rev:
                    info["meter_type"] = "发电表"
                    count += 1
                elif has_rev and not has_fwd:
                    info["meter_type"] = "上网表"
                    count += 1

        log.info("  判定 %d 个电表类型", count)

    # ================================================================
    # 第4轮：提取倍率、折扣、项目名
    # ================================================================

    def _pass4_fixed_attrs(self):
        """扫描所有文件，找倍率、折扣、项目名并关联到电表。"""
        log.info("[第4轮] 扫描倍率/折扣/项目名...")
        count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # A. 表头列同行关联
            if header_idx is not None:
                meter_cols = []
                mult_cols, disc_cols, proj_cols = [], [], []
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if not hdr:
                        continue
                    if self._matches_any(hdr, self._meter_aliases | self._gen_aliases | self._grid_aliases):
                        meter_cols.append(c)
                    if self._matches_any(hdr, self._multiplier_aliases):
                        mult_cols.append(c)
                    if self._matches_any(hdr, self._discount_aliases):
                        disc_cols.append(c)
                    if self._matches_any(hdr, self._project_aliases):
                        proj_cols.append(c)

                if meter_cols:
                    for r in range(header_idx + 1, len(df)):
                        mn = self._find_meter_in_row(df, r, meter_cols)
                        if not mn:
                            continue
                        info = self.meters[mn]

                        if not info["multiplier"]:
                            for mc in mult_cols:
                                v = _to_float(df.iloc[r, mc])
                                if v and v >= 1:
                                    info["multiplier"] = v
                                    count += 1
                                    break

                        if not info["discount"]:
                            for dc in disc_cols:
                                v = _to_float(df.iloc[r, dc])
                                if v:
                                    info["discount"] = v
                                    count += 1
                                    break

                        if not info["project_name"]:
                            for pc in proj_cols:
                                v = _cell_str(df.iloc[r, pc])
                                if v and not re.match(r'^\d+$', v) and len(v) >= 2:
                                    info["project_name"] = v
                                    count += 1
                                    break

            # B. 标签配对
            for r in range(len(df)):
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell:
                        continue

                    # 倍率
                    if self._matches_any(cell, self._multiplier_aliases):
                        val = self._find_value_near(df, r, c)
                        fv = _to_float(val)
                        if fv and fv >= 1:
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["multiplier"]:
                                self.meters[nearest]["multiplier"] = fv
                                count += 1

                    # 折扣
                    elif self._matches_any(cell, self._discount_aliases):
                        val = self._find_value_near(df, r, c)
                        m = re.search(r'(\d+\.?\d*)', val) if val else None
                        if m:
                            dv = float(m.group(1))
                            if dv > 1:
                                dv = dv / 10
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["discount"]:
                                self.meters[nearest]["discount"] = dv
                                count += 1

                    # 项目名
                    elif self._matches_any(cell, self._project_aliases):
                        val = self._find_value_near(df, r, c)
                        if val and not re.match(r'^\d+$', val) and len(val) >= 2:
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["project_name"]:
                                self.meters[nearest]["project_name"] = val
                                count += 1

            # C. 项目名兜底：从文件名提取
            proj = self._extract_project_from_filename(filepath.name)
            if proj:
                for mn, info in self.meters.items():
                    if info["source_file"] == filepath.name and not info["project_name"]:
                        info["project_name"] = proj
                        count += 1

        # D. 同用户电表互补（倍率、折扣、项目名）
        by_user = {}
        for mn, info in self.meters.items():
            uid = info.get("user_id")
            if uid:
                by_user.setdefault(uid, []).append(mn)

        for uid, meter_list in by_user.items():
            proj = next((self.meters[mn]["project_name"] for mn in meter_list
                         if self.meters[mn].get("project_name")), None)
            mult = next((self.meters[mn]["multiplier"] for mn in meter_list
                         if self.meters[mn].get("multiplier")), None)
            disc = next((self.meters[mn]["discount"] for mn in meter_list
                         if self.meters[mn].get("discount")), None)
            for mn in meter_list:
                info = self.meters[mn]
                if not info["project_name"] and proj:
                    info["project_name"] = proj
                    count += 1
                if not info["multiplier"] and mult:
                    info["multiplier"] = mult
                    count += 1
                if not info["discount"] and disc:
                    info["discount"] = disc
                    count += 1

        log.info("  关联 %d 个固定属性", count)

    # ================================================================
    # 第5轮：提取月度读数
    # ================================================================

    def _pass5_readings(self):
        """扫描所有文件，提取每个电表每月的尖峰平谷读数。"""
        log.info("[第5轮] 提取月度读数...")

        for df, filepath, sheet_name, source_info in self._sheets:
            # 标准表格
            self._readings_from_table(df, filepath, sheet_name, source_info)
            # 转置表
            self._readings_from_transposed(df, filepath, sheet_name, source_info)

    def _readings_from_table(self, df, filepath, sheet_name, source_info):
        """从标准表格提取读数。"""
        header_idx = self._find_header_row(df)
        if header_idx is None:
            return

        headers = {}
        for c in range(len(df.columns)):
            hdr = _cell_str(df.iloc[header_idx, c])
            if hdr:
                headers[c] = hdr

        # 电表号列
        meter_cols = []
        for c, hdr in headers.items():
            if self._matches_any(hdr, self._meter_aliases | self._gen_aliases | self._grid_aliases):
                meter_cols.append(c)
        if not meter_cols:
            for c, hdr in headers.items():
                if self._matches_any(hdr, self._asset_aliases):
                    meter_cols.append(c)
        if not meter_cols:
            return

        # 读数列
        reading_cols = self._map_reading_columns(headers)
        if not reading_cols:
            return

        # 特殊列
        date_col = next((c for c, h in headers.items()
                         if self._matches_any(h, self._date_aliases)), None)
        usage_col = next((c for c, h in headers.items()
                          if self._matches_any(h, self._usage_aliases)), None)
        fwd_total_col = next((c for c, h in headers.items()
                              if self._matches_any(h, self._fwd_total_aliases)), None)
        rev_total_col = next((c for c, h in headers.items()
                              if self._matches_any(h, self._rev_total_aliases)), None)

        current_month = self._infer_month(filepath.name, sheet_name, source_info)
        data_start = header_idx + 1

        for row_idx in range(data_start, len(df)):
            row = df.iloc[row_idx]

            # 月份标题行
            row_text = " ".join(_cell_str(row.iloc[c]) for c in range(min(5, len(row))))
            mm = _MONTH_RE.search(row_text)
            non_empty = sum(1 for c in range(len(row)) if _cell_str(row.iloc[c]))
            if mm and non_empty <= 3:
                y, mo = int(mm.group(1)), int(mm.group(2))
                if 2015 <= y <= 2035 and 1 <= mo <= 12:
                    current_month = f"{y}-{str(mo).zfill(2)}"
                continue

            # 汇总行跳过
            first = _cell_str(row.iloc[0]) if len(row) > 0 else ""
            if any(kw in first for kw in ("合计", "总计", "小计", "汇总")):
                continue

            # 找电表号
            meter_number = self._find_meter_in_row(df, row_idx, meter_cols)
            if not meter_number:
                continue

            # 月份
            row_month = current_month
            if date_col is not None:
                dv = _cell_str(row.iloc[date_col])
                dm = _MONTH_RE.search(dv)
                if dm:
                    y, mo = int(dm.group(1)), int(dm.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        row_month = f"{y}-{str(mo).zfill(2)}"
                elif hasattr(row.iloc[date_col], 'year'):
                    try:
                        d = row.iloc[date_col]
                        row_month = f"{d.year}-{str(d.month).zfill(2)}"
                    except Exception:
                        pass
            if not row_month or row_month == "unknown":
                continue

            # 读数
            readings = {k: _to_float(row.iloc[c]) for k, c in reading_cols.items()}
            total = None
            for tc in [fwd_total_col, rev_total_col, usage_col]:
                if tc is not None:
                    total = _to_float(row.iloc[tc])
                    if total is not None:
                        break
            parts = [v for v in readings.values() if v is not None]
            if total is None and parts:
                total = sum(parts)

            self._upsert_reading(meter_number, row_month, readings, total,
                                 filepath.name, sheet_name)

    def _readings_from_transposed(self, df, filepath, sheet_name, source_info):
        """从转置表（行=尖峰平谷）提取读数。"""
        period_keywords = {
            "尖峰": "sharp_peak", "尖": "sharp_peak",
            "正有功尖峰": "sharp_peak", "正有功尖": "sharp_peak",
            "峰": "peak", "正有功峰": "peak",
            "平": "flat", "正有功平": "flat",
            "谷": "valley", "正有功谷": "valley",
            "总": "total", "正有功总": "total", "合计": "total",
        }

        category_col = None
        period_rows = {}
        for col_idx in range(min(3, len(df.columns))):
            temp = {}
            for row_idx in range(min(len(df), 30)):
                cell = _cell_str(df.iloc[row_idx, col_idx])
                for kw, pkey in period_keywords.items():
                    if cell == kw or cell.startswith(kw):
                        temp[row_idx] = pkey
                        break
            if len(temp) >= 3:
                period_rows = temp
                category_col = col_idx
                break
        if len(period_rows) < 3:
            return

        # 数据列
        data_cols = {}
        for row_idx in range(min(8, len(df))):
            for col_idx in range(len(df.columns)):
                if col_idx == category_col:
                    continue
                cell = _cell_str(df.iloc[row_idx, col_idx])
                if cell in ("发电量", "用电量", "正向用电量", "电表用量", "电表用理"):
                    data_cols.setdefault("forward", col_idx)
                elif cell in ("上网电量", "上网用电量", "反向用电量"):
                    data_cols.setdefault("reverse", col_idx)

        if not data_cols.get("forward"):
            for col_idx in range(category_col + 1, min(len(df.columns), 15)):
                for row_idx in period_rows:
                    if _to_float(df.iloc[row_idx, col_idx]) is not None:
                        data_cols["forward"] = col_idx
                        break
                if "forward" in data_cols:
                    break

        # 月份
        month = self._infer_month(filepath.name, sheet_name, source_info)
        for r in range(min(5, len(df))):
            for c in range(min(5, len(df.columns))):
                m = _MONTH_RE.search(_cell_str(df.iloc[r, c]))
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        month = f"{y}-{str(mo).zfill(2)}"
                        break
        if not month or month == "unknown":
            return

        # 对应的电表
        sheet_meters = [mn for mn, info in self.meters.items()
                        if info["source_file"] == filepath.name]
        gen_meter = grid_meter = None
        for mn in sheet_meters:
            mt = self.meters[mn]["meter_type"]
            if mt == "发电表" and not gen_meter:
                gen_meter = mn
            elif mt == "上网表" and not grid_meter:
                grid_meter = mn
            elif not gen_meter:
                gen_meter = mn

        fwd_r, rev_r = {}, {}
        fwd_total = rev_total = None
        for row_idx, period in period_rows.items():
            fv = _to_float(df.iloc[row_idx, data_cols["forward"]]) if "forward" in data_cols else None
            rv = _to_float(df.iloc[row_idx, data_cols["reverse"]]) if "reverse" in data_cols else None
            if period == "total":
                fwd_total, rev_total = fv, rv
            elif period in ("sharp_peak", "peak", "flat", "valley"):
                fwd_r[period] = fv
                rev_r[period] = rv

        if gen_meter and any(v is not None for v in fwd_r.values()):
            parts = [v for v in fwd_r.values() if v is not None]
            self._upsert_reading(gen_meter, month, fwd_r,
                                 fwd_total or (sum(parts) if parts else None),
                                 filepath.name, sheet_name)
        if grid_meter and any(v is not None for v in rev_r.values()):
            parts = [v for v in rev_r.values() if v is not None]
            self._upsert_reading(grid_meter, month, rev_r,
                                 rev_total or (sum(parts) if parts else None),
                                 filepath.name, sheet_name)

    # ================================================================
    # 组装 + 合并
    # ================================================================

    def _upsert_reading(self, meter_number, month, readings, total, source_file, source_sheet):
        """写入或补全读数。"""
        key = (meter_number, month)
        if key not in self.readings:
            self.readings[key] = {
                "sharp_peak": readings.get("sharp_peak"),
                "peak": readings.get("peak"),
                "flat": readings.get("flat"),
                "valley": readings.get("valley"),
                "total_kwh": total,
                "source_file": source_file,
                "source_sheet": source_sheet,
            }
        else:
            existing = self.readings[key]
            for f in ("sharp_peak", "peak", "flat", "valley"):
                if existing.get(f) is None and readings.get(f) is not None:
                    existing[f] = readings[f]
            if existing.get("total_kwh") is None and total is not None:
                existing["total_kwh"] = total

    def _build_records(self) -> list[dict]:
        records = []
        for (mn, month), reading in self.readings.items():
            if mn not in self.meters:
                continue
            info = self.meters[mn]
            records.append({
                "meter_number": mn,
                "asset_number": info.get("asset_number"),
                "user_id": info.get("user_id"),
                "meter_type": info.get("meter_type", "未知"),
                "multiplier": info.get("multiplier"),
                "discount": info.get("discount"),
                "project_name": info.get("project_name"),
                "reading_month": month,
                "sharp_peak": reading.get("sharp_peak"),
                "peak": reading.get("peak"),
                "flat": reading.get("flat"),
                "valley": reading.get("valley"),
                "total_kwh": reading.get("total_kwh"),
                "source_file": reading.get("source_file"),
                "source_sheet": reading.get("source_sheet"),
            })

        # 没有读数但有电表信息的也入库
        meters_with_readings = {mn for mn, _ in self.readings}
        for mn, info in self.meters.items():
            if mn not in meters_with_readings:
                records.append({
                    "meter_number": mn,
                    "asset_number": info.get("asset_number"),
                    "user_id": info.get("user_id"),
                    "meter_type": info.get("meter_type", "未知"),
                    "multiplier": info.get("multiplier"),
                    "discount": info.get("discount"),
                    "project_name": info.get("project_name"),
                    "reading_month": "unknown",
                    "source_file": info.get("source_file"),
                    "source_sheet": info.get("source_sheet"),
                })

        log.info("最终结果: %d 个电表, %d 条记录", len(self.meters), len(records))
        return records

    def _merge_short_meters(self):
        """合并短电表号到长电表号。"""
        all_nums = list(self.meters.keys())
        to_merge = []
        for i, a in enumerate(all_nums):
            if not re.match(r'^\d+$', a):
                continue
            for b in all_nums[i+1:]:
                if not re.match(r'^\d+$', b):
                    continue
                if len(a) < len(b) and a in b:
                    to_merge.append((a, b))
                elif len(b) < len(a) and b in a:
                    to_merge.append((b, a))

        for short, long in to_merge:
            if short not in self.meters:
                continue
            si, li = self.meters[short], self.meters[long]
            for f in ("asset_number", "user_id", "multiplier", "discount", "project_name"):
                if not li.get(f) and si.get(f):
                    li[f] = si[f]
            if li["meter_type"] == "未知" and si["meter_type"] != "未知":
                li["meter_type"] = si["meter_type"]
            for key in list(self.readings.keys()):
                if key[0] == short:
                    new_key = (long, key[1])
                    if new_key not in self.readings:
                        self.readings[new_key] = self.readings[key]
                    del self.readings[key]
            del self.meters[short]
            log.info("  合并: %s -> %s", short, long)

    # ================================================================
    # 工具方法
    # ================================================================

    def _register_meter(self, meter_number: str, source_file="", source_sheet="", meter_type=None):
        if meter_number not in self.meters:
            self.meters[meter_number] = {
                "meter_number": meter_number,
                "asset_number": None, "user_id": None,
                "meter_type": meter_type or "未知",
                "multiplier": None, "discount": None, "project_name": None,
                "source_file": source_file, "source_sheet": source_sheet,
            }
        elif meter_type and self.meters[meter_number]["meter_type"] == "未知":
            self.meters[meter_number]["meter_type"] = meter_type

    def _find_header_row(self, df, max_scan=20) -> Optional[int]:
        all_kw = set()
        for field, aliases in self.field_mapping.items():
            if isinstance(aliases, list):
                all_kw.update(aliases)
            elif isinstance(aliases, dict):
                for sub in aliases.values():
                    if isinstance(sub, list):
                        all_kw.update(sub)
        all_kw.update(["上月表数", "本月表数", "电表用量", "用电量", "发电量",
                       "用户编号", "用户名称", "电表资产号", "统计日期",
                       "尖", "峰", "平", "谷", "用户类型", "kWh"])
        best_idx, best_score = None, 0
        for i in range(min(len(df), max_scan)):
            cells = [_cell_str(df.iloc[i, c]) for c in range(len(df.columns))]
            score = sum(1 for cell in cells if cell and any(kw in cell for kw in all_kw))
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx if best_score >= 2 else None

    def _matches_any(self, text: str, aliases: set) -> bool:
        if not text or not aliases:
            return False
        text = text.strip()
        if text in aliases:
            return True
        for a in aliases:
            if a in text or text in a:
                return True
        return False

    def _find_value_near(self, df, row_idx, col_idx) -> str:
        max_r, max_c = len(df), len(df.columns)
        for dr, dc in [(0, 1), (1, 0), (1, 1), (0, 2)]:
            r, c = row_idx + dr, col_idx + dc
            if r < max_r and c < max_c:
                val = _cell_str(df.iloc[r, c])
                if val and not _CHINESE_RE.search(val):
                    return val
        return ""

    def _find_meter_in_row(self, df, row_idx, meter_cols) -> Optional[str]:
        """在指定行的电表列中查找已知电表号。"""
        for mc in meter_cols:
            val = clean_id(_cell_str(df.iloc[row_idx, mc]))
            if val in self.meters:
                return val
            if _is_meter_like(val):
                for mn in self.meters:
                    if val in mn or mn in val:
                        return mn if len(mn) > len(val) else val
        return None

    def _find_nearest_meter(self, df, row_idx, col_idx, source_file) -> Optional[str]:
        """找距离当前位置最近的电表号（同文件）。"""
        # 同行的电表
        for mn, info in self.meters.items():
            if info["source_file"] != source_file:
                continue
        # 通过文件中所有单元格暴力搜索
        best_mn, best_dist = None, 999
        for r in range(max(0, row_idx - 5), min(len(df), row_idx + 6)):
            for c in range(len(df.columns)):
                val = clean_id(_cell_str(df.iloc[r, c]))
                if val in self.meters:
                    dist = abs(r - row_idx) + abs(c - col_idx)
                    if dist < best_dist:
                        best_dist = dist
                        best_mn = val
        return best_mn

    def _map_reading_columns(self, headers: dict) -> dict:
        """从表头映射尖峰平谷列。"""
        result = {}
        for sub_field, aliases in self._fwd_readings_cfg.items():
            if not isinstance(aliases, list):
                continue
            key = "sharp_peak" if "sharp" in sub_field else sub_field
            for col_idx, hdr in headers.items():
                if any(a in hdr for a in aliases):
                    result.setdefault(key, col_idx)
                    break
        if not result:
            for sub_field, aliases in self._rev_readings_cfg.items():
                if not isinstance(aliases, list):
                    continue
                key = "sharp_peak" if "sharp" in sub_field else sub_field
                for col_idx, hdr in headers.items():
                    if any(a in hdr for a in aliases):
                        result.setdefault(key, col_idx)
                        break
        return result

    def _detect_type_from_text(self, text: str) -> str:
        if any(kw in text for kw in ["光伏发电", "发电客户", "发电户", "逆变"]):
            return "发电表"
        if any(kw in text for kw in ["地方电厂", "电厂户", "上网", "并网", "关口"]):
            return "上网表"
        if any(kw in text for kw in self._gen_keywords):
            return "发电表"
        if any(kw in text for kw in self._grid_keywords):
            return "上网表"
        return "未知"

    def _infer_month(self, filename: str, sheet_name: str, source_info: dict) -> str:
        for text in [filename, sheet_name]:
            for m in _MONTH_RE.finditer(text):
                y, mo = int(m.group(1)), int(m.group(2))
                if 2015 <= y <= 2035 and 1 <= mo <= 12:
                    return f"{y}-{str(mo).zfill(2)}"
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")
        return "unknown"

    def _extract_project_from_filename(self, filename: str) -> Optional[str]:
        m = re.search(r'([\u4e00-\u9fff]{2,10}(?:项目|电站|光伏))', filename)
        return m.group(1) if m else None
