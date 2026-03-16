"""多轮扫描提取器：按用户思路，每轮只做一件事。

第1轮：扫描所有文件，提取所有电表号
第2轮：扫描所有文件，提取资产编号，关联到电表
第3轮：扫描所有文件，提取用户编号，关联到电表
第4轮：扫描所有文件，提取电表类型
第5轮：扫描所有文件，提取每个电表每月的读数（尖峰平谷）
"""

import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config_loader import config
from src.logger import log
from src.parsers.validators import clean_id, is_valid_meter_number


# ========== 正则模式 ==========
# 电表号：8-16位纯数字（排除日期格式）
_METER_RE = re.compile(r'(?<!\d)(\d{8,16})(?!\d)')
# 资产编号：含字母和数字混合，或纯数字6-20位
_ASSET_RE = re.compile(r'(?<!\w)([A-Za-z0-9\-\.]{6,20})(?!\w)')
# 用户编号：6-20位纯数字
_USER_RE = re.compile(r'(?<!\d)(\d{6,20})(?!\d)')
# 日期模式（排除用）
_DATE_RE = re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{4}[-/]\d{1,2}$|^\d{8}$')
# 月份提取
_MONTH_RE = re.compile(r'(\d{4})\s*[-年/]\s*(\d{1,2})\s*月?')
# 中文字符
_CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')


def _cell_str(val) -> str:
    """安全地把单元格值变成字符串。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if s.lower() in ("none", "nan", "null", ""):
        return ""
    # 去 .0 后缀
    s = re.sub(r'\.0+$', '', s)
    return s


def _to_float(val) -> Optional[float]:
    """安全地转浮点。"""
    s = _cell_str(val)
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _is_meter_like(s: str) -> bool:
    """判断字符串是否像电表号。"""
    if not s or len(s) < 8 or len(s) > 16:
        return False
    if _CHINESE_RE.search(s):
        return False
    if _DATE_RE.match(s):
        return False
    if not re.match(r'^\d+$', s):
        return False
    return True


class MultiPassExtractor:
    """多轮扫描提取器。

    使用方式：
        extractor = MultiPassExtractor()
        extractor.load_dataframes(all_dataframes)  # 加载所有文件的 DataFrame
        records = extractor.extract_all()           # 多轮扫描，返回最终记录
    """

    def __init__(self):
        self.field_mapping = config.get("field_mapping") or {}
        self.meter_type_rules = config.get("meter_type_rules") or {}

        # 从 config 收集别名
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

        # 尖峰平谷别名
        self._fwd_readings_cfg = self.field_mapping.get("forward_readings", {})
        self._rev_readings_cfg = self.field_mapping.get("reverse_readings", {})

        # 类型关键词
        self._gen_keywords = set(self.meter_type_rules.get("generation_meter", {}).get("keywords", []))
        self._grid_keywords = set(self.meter_type_rules.get("grid_meter", {}).get("keywords", []))

        # 所有 sheet 数据: [(df, filepath, sheet_name, source_info), ...]
        self._sheets = []

        # 提取结果
        self.meters = {}          # meter_number -> {asset, user_id, type, ...}
        self.readings = {}        # (meter_number, month) -> {sharp_peak, peak, flat, valley, total}

    def load_dataframes(self, sheets: list):
        """加载所有文件的 DataFrame。

        Args:
            sheets: [(df, filepath, sheet_name, source_info), ...]
        """
        self._sheets = sheets
        log.info("多轮扫描: 加载 %d 个 sheet", len(sheets))

    def extract_all(self) -> list[dict]:
        """执行多轮扫描，返回最终的电表记录列表。"""
        log.info("=" * 50)
        log.info("开始多轮扫描提取")
        log.info("=" * 50)

        # ===== 固定数据（电表档案）=====
        # 第1轮：提取所有电表号
        self._pass1_meter_numbers()
        log.info("第1轮完成: 找到 %d 个电表", len(self.meters))

        # 第2轮：提取资产编号 → 关联电表
        self._pass2_asset_numbers()
        linked_assets = sum(1 for m in self.meters.values() if m.get("asset_number"))
        log.info("第2轮完成: %d 个电表已关联资产编号", linked_assets)

        # 第3轮：提取用户编号 → 关联电表
        self._pass3_user_ids()
        linked_users = sum(1 for m in self.meters.values() if m.get("user_id"))
        log.info("第3轮完成: %d 个电表已关联用户编号", linked_users)

        # 第4轮：提取电表类型
        self._pass4_meter_types()
        typed = sum(1 for m in self.meters.values() if m.get("meter_type") != "未知")
        log.info("第4轮完成: %d 个电表已确定类型", typed)

        # 第5轮：提取倍率、折扣、项目名（都是固定属性）
        self._pass5_fixed_attrs()
        has_mult = sum(1 for m in self.meters.values() if m.get("multiplier"))
        has_proj = sum(1 for m in self.meters.values() if m.get("project_name"))
        log.info("第5轮完成: %d 有倍率, %d 有项目名", has_mult, has_proj)

        # 合并短电表号
        self._merge_short_meters()
        log.info("电表档案建立完成: %d 个电表", len(self.meters))
        for mn, info in self.meters.items():
            log.info("  %s | 类型=%s | 资产=%s | 用户=%s | 倍率=%s | 项目=%s",
                     mn, info.get("meter_type"), info.get("asset_number"),
                     info.get("user_id"), info.get("multiplier"), info.get("project_name"))

        # ===== 动态数据（月度读数）=====
        # 第6轮：提取每月读数（尖峰平谷）
        self._pass6_readings()
        log.info("第6轮完成: %d 条读数记录", len(self.readings))

        # 组装最终记录
        return self._build_records()

    # ========== 第1轮：提取电表号 ==========

    def _pass1_meter_numbers(self):
        """扫描所有文件所有单元格，找出电表号。

        识别逻辑：
        1. 列名含 "电表号/表号/表计编号" 等 → 该列所有值都是电表号
        2. 单元格旁边有 "电表号/发电表号/上网表号" 标签 → 值是电表号
        3. 列名含 "发电表号/上网表号" → 该列值也是电表号（分类不同）
        """
        for df, filepath, sheet_name, source_info in self._sheets:
            # 先找表头行
            header_idx = self._find_header_row(df)
            if header_idx is not None:
                headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
                data_start = header_idx + 1
            else:
                headers = []
                data_start = 0

            # 方式A：从表头列提取
            for col_idx, hdr in enumerate(headers):
                if not hdr:
                    continue
                is_meter = self._matches_any(hdr, self._meter_aliases)
                is_gen = self._matches_any(hdr, self._gen_aliases)
                is_grid = self._matches_any(hdr, self._grid_aliases)
                is_asset = self._matches_any(hdr, self._asset_aliases)

                if is_meter or is_gen or is_grid:
                    tag = "发电表" if is_gen else ("上网表" if is_grid else None)
                    for row_idx in range(data_start, len(df)):
                        val = _cell_str(df.iloc[row_idx, col_idx])
                        val = clean_id(val)
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, tag)
                elif is_asset:
                    # 资产编号列：如果没有找到电表号列，把资产编号当电表号
                    has_meter_col = any(
                        self._matches_any(_cell_str(df.iloc[header_idx, c]), self._meter_aliases)
                        or self._matches_any(_cell_str(df.iloc[header_idx, c]), self._gen_aliases)
                        or self._matches_any(_cell_str(df.iloc[header_idx, c]), self._grid_aliases)
                        for c in range(len(df.columns)) if c != col_idx
                    )
                    if not has_meter_col:
                        for row_idx in range(data_start, len(df)):
                            val = _cell_str(df.iloc[row_idx, col_idx])
                            val = clean_id(val)
                            if is_valid_meter_number(val):
                                self._register_meter(val, filepath.name, sheet_name)

            # 方式B：扫描所有单元格找 "标签: 值" 模式
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if not cell:
                        continue

                    # 检查是否是电表号标签
                    tag = None
                    is_label = False
                    if self._matches_any(cell, self._meter_aliases):
                        is_label = True
                    elif self._matches_any(cell, self._gen_aliases):
                        is_label = True
                        tag = "发电表"
                    elif self._matches_any(cell, self._grid_aliases):
                        is_label = True
                        tag = "上网表"

                    if is_label:
                        # 在右边、下边、对角线找值
                        val = self._find_value_near(df, row_idx, col_idx)
                        val = clean_id(val)
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, tag)

                    # 检查单元格自身是否包含 "标签：值" 格式
                    for pattern in [
                        r'(?:电表号|表号|电能表号|表计编号)\s*[:：]\s*(\d{8,16})',
                        r'(?:发电表号?|发电电表号?)\s*[:：]\s*(\d{8,16})',
                        r'(?:上网表号?|上网电表号?)\s*[:：]\s*(\d{8,16})',
                    ]:
                        m = re.search(pattern, cell)
                        if m:
                            val = clean_id(m.group(1))
                            if is_valid_meter_number(val):
                                t = None
                                if "发电" in pattern:
                                    t = "发电表"
                                elif "上网" in pattern:
                                    t = "上网表"
                                self._register_meter(val, filepath.name, sheet_name, t)

    def _register_meter(self, meter_number: str, source_file: str = "",
                        source_sheet: str = "", meter_type: str = None):
        """注册一个电表号。"""
        if meter_number not in self.meters:
            self.meters[meter_number] = {
                "meter_number": meter_number,
                "asset_number": None,
                "user_id": None,
                "meter_type": meter_type or "未知",
                "multiplier": None,
                "discount": None,
                "project_name": None,
                "source_file": source_file,
                "source_sheet": source_sheet,
            }
        else:
            # 更新类型（如果之前未知）
            if meter_type and self.meters[meter_number]["meter_type"] == "未知":
                self.meters[meter_number]["meter_type"] = meter_type

    # ========== 第2轮：提取资产编号 ==========

    def _pass2_asset_numbers(self):
        """扫描所有文件，找出资产编号，关联到同行/附近的电表号。"""
        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)
            if header_idx is not None:
                headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
                data_start = header_idx + 1
            else:
                headers = []
                data_start = 0

            # 找资产编号列和电表号列
            asset_cols = []
            meter_cols = []
            for col_idx, hdr in enumerate(headers):
                if self._matches_any(hdr, self._asset_aliases):
                    asset_cols.append(col_idx)
                if (self._matches_any(hdr, self._meter_aliases) or
                    self._matches_any(hdr, self._gen_aliases) or
                    self._matches_any(hdr, self._grid_aliases)):
                    meter_cols.append(col_idx)

            # 同行关联
            if asset_cols and meter_cols:
                for row_idx in range(data_start, len(df)):
                    for mc in meter_cols:
                        mn = clean_id(_cell_str(df.iloc[row_idx, mc]))
                        if mn in self.meters:
                            for ac in asset_cols:
                                av = _cell_str(df.iloc[row_idx, ac])
                                av = clean_id(av)
                                if av and len(av) >= 4 and not _CHINESE_RE.search(av):
                                    if not self.meters[mn]["asset_number"]:
                                        self.meters[mn]["asset_number"] = av
                                        log.debug("  资产编号: %s -> %s", mn, av)

            # 标签-值模式
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if self._matches_any(cell, self._asset_aliases):
                        val = clean_id(self._find_value_near(df, row_idx, col_idx))
                        if val and len(val) >= 4 and not _CHINESE_RE.search(val):
                            # 找最近的电表号
                            nearest = self._find_nearest_meter(df, row_idx, col_idx)
                            if nearest and not self.meters[nearest]["asset_number"]:
                                self.meters[nearest]["asset_number"] = val

    # ========== 第3轮：提取用户编号 ==========

    def _pass3_user_ids(self):
        """扫描所有文件，找出用户编号，关联到电表。"""
        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)
            if header_idx is not None:
                headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
                data_start = header_idx + 1
            else:
                headers = []
                data_start = 0

            user_cols = []
            meter_cols = []
            for col_idx, hdr in enumerate(headers):
                if self._matches_any(hdr, self._user_aliases):
                    user_cols.append(col_idx)
                if (self._matches_any(hdr, self._meter_aliases) or
                    self._matches_any(hdr, self._gen_aliases) or
                    self._matches_any(hdr, self._grid_aliases)):
                    meter_cols.append(col_idx)

            # 同行关联
            if user_cols and meter_cols:
                for row_idx in range(data_start, len(df)):
                    for mc in meter_cols:
                        mn = clean_id(_cell_str(df.iloc[row_idx, mc]))
                        if mn in self.meters:
                            for uc in user_cols:
                                uv = clean_id(_cell_str(df.iloc[row_idx, uc]))
                                if uv and len(uv) >= 6 and re.match(r'^\d+$', uv):
                                    if not self.meters[mn]["user_id"]:
                                        self.meters[mn]["user_id"] = uv

            # 标签-值模式（卡片布局、转置表侧栏等）
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if self._matches_any(cell, self._user_aliases):
                        val = clean_id(self._find_value_near(df, row_idx, col_idx))
                        if val and len(val) >= 6 and re.match(r'^\d+$', val):
                            nearest = self._find_nearest_meter(df, row_idx, col_idx)
                            if nearest and not self.meters[nearest]["user_id"]:
                                self.meters[nearest]["user_id"] = val

            # 如果整个 sheet 只有一个用户编号，关联给所有没有用户编号的电表
            sheet_users = set()
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if self._matches_any(cell, self._user_aliases):
                        val = clean_id(self._find_value_near(df, row_idx, col_idx))
                        if val and len(val) >= 6 and re.match(r'^\d+$', val):
                            sheet_users.add(val)
            if len(sheet_users) == 1:
                uid = sheet_users.pop()
                for mn, info in self.meters.items():
                    if info["source_file"] == filepath.name and not info["user_id"]:
                        info["user_id"] = uid

    # ========== 第4轮：提取电表类型 ==========

    def _pass4_meter_types(self):
        """扫描所有文件，从上下文确定电表类型。"""
        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)
            if header_idx is not None:
                headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
                data_start = header_idx + 1
            else:
                headers = []
                data_start = 0

            # 方式A：用户类型列
            type_col = None
            meter_cols = []
            for col_idx, hdr in enumerate(headers):
                if "用户类型" in hdr or "类别" in hdr or "类型" in hdr:
                    type_col = col_idx
                if (self._matches_any(hdr, self._meter_aliases) or
                    self._matches_any(hdr, self._gen_aliases) or
                    self._matches_any(hdr, self._grid_aliases)):
                    meter_cols.append(col_idx)

            if type_col is not None and meter_cols:
                for row_idx in range(data_start, len(df)):
                    type_val = _cell_str(df.iloc[row_idx, type_col])
                    mtype = self._detect_type_from_text(type_val)
                    if mtype != "未知":
                        for mc in meter_cols:
                            mn = clean_id(_cell_str(df.iloc[row_idx, mc]))
                            if mn in self.meters and self.meters[mn]["meter_type"] == "未知":
                                self.meters[mn]["meter_type"] = mtype

            # 方式B：Sheet名/文件名推断
            context = f"{filepath.name} {sheet_name}"
            ctx_type = self._detect_type_from_text(context)

            # 方式C：列结构推断（有反向列 → 上网表，有正向列 → 发电表）
            has_fwd = any(self._matches_any(h, self._fwd_total_aliases) or
                         any(self._matches_any(h, als) for als in self._fwd_readings_cfg.values()
                             if isinstance(als, list))
                         for h in headers)
            has_rev = any(self._matches_any(h, self._rev_total_aliases) or
                         any(self._matches_any(h, als) for als in self._rev_readings_cfg.values()
                             if isinstance(als, list))
                         for h in headers)

            # 方式D：标签附近的电表号
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if self._matches_any(cell, self._gen_aliases):
                        val = clean_id(self._find_value_near(df, row_idx, col_idx))
                        if val in self.meters and self.meters[val]["meter_type"] == "未知":
                            self.meters[val]["meter_type"] = "发电表"
                    elif self._matches_any(cell, self._grid_aliases):
                        val = clean_id(self._find_value_near(df, row_idx, col_idx))
                        if val in self.meters and self.meters[val]["meter_type"] == "未知":
                            self.meters[val]["meter_type"] = "上网表"

            # 用 sheet 级上下文给剩余未知电表赋类型
            for mn, info in self.meters.items():
                if info["source_file"] != filepath.name:
                    continue
                if info["meter_type"] != "未知":
                    continue
                # 从文件名/sheet名
                if ctx_type != "未知":
                    info["meter_type"] = ctx_type
                # 从列结构
                elif has_fwd and not has_rev:
                    info["meter_type"] = "发电表"
                elif has_rev and not has_fwd:
                    info["meter_type"] = "上网表"

    # ========== 第5轮：提取固定属性（倍率、折扣、项目名）==========

    def _pass5_fixed_attrs(self):
        """扫描所有文件，提取倍率、折扣、项目名等固定属性。"""
        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)
            if header_idx is not None:
                headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
                data_start = header_idx + 1
            else:
                headers = []
                data_start = 0

            # 找表头列
            meter_cols = []
            mult_col = None
            disc_col = None
            proj_col = None
            for col_idx, hdr in enumerate(headers):
                if (self._matches_any(hdr, self._meter_aliases) or
                    self._matches_any(hdr, self._gen_aliases) or
                    self._matches_any(hdr, self._grid_aliases)):
                    meter_cols.append(col_idx)
                if self._matches_any(hdr, self._multiplier_aliases):
                    mult_col = col_idx
                if self._matches_any(hdr, self._discount_aliases):
                    disc_col = col_idx
                if self._matches_any(hdr, self._project_aliases):
                    proj_col = col_idx

            # 从表格行提取
            for row_idx in range(data_start, len(df)):
                for mc in meter_cols:
                    mn = clean_id(_cell_str(df.iloc[row_idx, mc]))
                    if mn not in self.meters:
                        continue
                    info = self.meters[mn]
                    if mult_col is not None and not info["multiplier"]:
                        v = _to_float(df.iloc[row_idx, mult_col])
                        if v and v >= 1:
                            info["multiplier"] = v
                    if disc_col is not None and not info["discount"]:
                        v = _to_float(df.iloc[row_idx, disc_col])
                        if v:
                            info["discount"] = v
                    if proj_col is not None and not info["project_name"]:
                        v = _cell_str(df.iloc[row_idx, proj_col])
                        if v and not re.match(r'^\d+$', v):
                            info["project_name"] = v

            # 标签-值模式
            for row_idx in range(len(df)):
                for col_idx in range(len(df.columns)):
                    cell = _cell_str(df.iloc[row_idx, col_idx])
                    if self._matches_any(cell, self._multiplier_aliases):
                        val = _to_float(self._find_value_near(df, row_idx, col_idx))
                        if val and val >= 1:
                            nearest = self._find_nearest_meter(df, row_idx, col_idx)
                            if nearest and not self.meters[nearest]["multiplier"]:
                                self.meters[nearest]["multiplier"] = val
                    elif self._matches_any(cell, self._discount_aliases):
                        val_str = self._find_value_near(df, row_idx, col_idx)
                        # 折扣可能是 "9折" 或 "0.9"
                        m = re.search(r'(\d+\.?\d*)', val_str)
                        if m:
                            dv = float(m.group(1))
                            if dv > 1:
                                dv = dv / 10  # "9折" -> 0.9
                            nearest = self._find_nearest_meter(df, row_idx, col_idx)
                            if nearest and not self.meters[nearest]["discount"]:
                                self.meters[nearest]["discount"] = dv
                    elif self._matches_any(cell, self._project_aliases):
                        val = self._find_value_near(df, row_idx, col_idx)
                        if val and not re.match(r'^\d+$', val) and len(val) >= 2:
                            nearest = self._find_nearest_meter(df, row_idx, col_idx)
                            if nearest and not self.meters[nearest]["project_name"]:
                                self.meters[nearest]["project_name"] = val

            # 从文件名推断项目名
            fname = filepath.name
            for mn, info in self.meters.items():
                if info["source_file"] == fname and not info["project_name"]:
                    proj = self._extract_project_from_filename(fname)
                    if proj:
                        info["project_name"] = proj

        # 同用户编号的电表共享项目名、倍率、折扣
        by_user = {}
        for mn, info in self.meters.items():
            uid = info.get("user_id")
            if uid:
                by_user.setdefault(uid, []).append(mn)

        for uid, meter_list in by_user.items():
            # 找到该用户下有值的属性
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
                if not info["multiplier"] and mult:
                    info["multiplier"] = mult
                if not info["discount"] and disc:
                    info["discount"] = disc

    # ========== 第6轮：提取动态读数 ==========

    def _pass6_readings(self):
        """扫描所有文件，为每个电表提取每月的尖峰平谷读数。"""
        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # 策略A：标准表格（有表头行，电表号在某列，读数在其他列）
            if header_idx is not None:
                self._extract_readings_table(df, header_idx, filepath, sheet_name, source_info)

            # 策略B：转置表（行=尖峰平谷，列=数据）
            self._extract_readings_transposed(df, filepath, sheet_name, source_info)

    def _extract_readings_table(self, df, header_idx, filepath, sheet_name, source_info):
        """从标准表格提取读数。"""
        headers = [_cell_str(df.iloc[header_idx, c]) for c in range(len(df.columns))]
        data_start = header_idx + 1

        # 找电表号列
        meter_cols = []
        for col_idx, hdr in enumerate(headers):
            if (self._matches_any(hdr, self._meter_aliases) or
                self._matches_any(hdr, self._gen_aliases) or
                self._matches_any(hdr, self._grid_aliases)):
                meter_cols.append(col_idx)

        if not meter_cols:
            # 没有电表号列，可能是资产编号列
            for col_idx, hdr in enumerate(headers):
                if self._matches_any(hdr, self._asset_aliases):
                    meter_cols.append(col_idx)

        if not meter_cols:
            return

        # 找读数列
        reading_cols = self._map_reading_columns(headers)
        if not reading_cols:
            return

        # 找月份列
        date_col = None
        for col_idx, hdr in enumerate(headers):
            if self._matches_any(hdr, self._date_aliases):
                date_col = col_idx
                break

        # 查找其他有用的列
        usage_col = None
        fwd_total_col = None
        rev_total_col = None
        multiplier_col = None
        for col_idx, hdr in enumerate(headers):
            if self._matches_any(hdr, self._usage_aliases):
                usage_col = col_idx
            if self._matches_any(hdr, self._fwd_total_aliases):
                fwd_total_col = col_idx
            if self._matches_any(hdr, self._rev_total_aliases):
                rev_total_col = col_idx
            if self._matches_any(hdr, self._multiplier_aliases):
                multiplier_col = col_idx

        # 检测多月分段
        current_month = self._infer_month(filepath.name, sheet_name, source_info)

        for row_idx in range(data_start, len(df)):
            row = df.iloc[row_idx]

            # 检查是否是月份标题行
            row_text = " ".join(_cell_str(row.iloc[c]) for c in range(min(5, len(row))))
            month_match = _MONTH_RE.search(row_text)
            non_empty = sum(1 for c in range(len(row)) if _cell_str(row.iloc[c]))
            if month_match and non_empty <= 3:
                y, mo = int(month_match.group(1)), int(month_match.group(2))
                if 2015 <= y <= 2035 and 1 <= mo <= 12:
                    current_month = f"{y}-{str(mo).zfill(2)}"
                continue

            # 跳过汇总行
            first_cell = _cell_str(row.iloc[0]) if len(row) > 0 else ""
            if any(kw in first_cell for kw in ("合计", "总计", "小计", "汇总")):
                continue

            # 找电表号
            meter_number = None
            for mc in meter_cols:
                val = clean_id(_cell_str(row.iloc[mc]))
                if val in self.meters:
                    meter_number = val
                    break
                # 模糊匹配：短号匹配长号
                if _is_meter_like(val):
                    for mn in self.meters:
                        if val in mn or mn in val:
                            meter_number = mn if len(mn) > len(val) else val
                            break
                if meter_number:
                    break

            if not meter_number:
                continue

            # 从日期列获取月份
            row_month = current_month
            if date_col is not None:
                dv = _cell_str(row.iloc[date_col])
                m = _MONTH_RE.search(dv)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
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

            # 提取读数
            readings = {}
            for key, col_idx in reading_cols.items():
                readings[key] = _to_float(row.iloc[col_idx])

            # 提取总量
            total = None
            if fwd_total_col is not None:
                total = _to_float(row.iloc[fwd_total_col])
            if total is None and rev_total_col is not None:
                total = _to_float(row.iloc[rev_total_col])
            if total is None and usage_col is not None:
                total = _to_float(row.iloc[usage_col])

            # 累加计算
            parts = [v for v in readings.values() if v is not None]
            if total is None and parts:
                total = sum(parts)

            # 倍率
            if multiplier_col is not None:
                mult = _to_float(row.iloc[multiplier_col])
                if mult and mult > 1 and meter_number in self.meters:
                    if not self.meters[meter_number]["multiplier"]:
                        self.meters[meter_number]["multiplier"] = mult

            key = (meter_number, row_month)
            if key not in self.readings:
                self.readings[key] = {
                    "sharp_peak": readings.get("sharp_peak"),
                    "peak": readings.get("peak"),
                    "flat": readings.get("flat"),
                    "valley": readings.get("valley"),
                    "total_kwh": total,
                    "source_file": filepath.name,
                    "source_sheet": sheet_name,
                }
            else:
                # 补全缺失值
                existing = self.readings[key]
                for f in ("sharp_peak", "peak", "flat", "valley", "total_kwh"):
                    if existing.get(f) is None and readings.get(f) is not None:
                        existing[f] = readings.get(f)
                if existing.get("total_kwh") is None and total is not None:
                    existing["total_kwh"] = total

    def _extract_readings_transposed(self, df, filepath, sheet_name, source_info):
        """从转置表（行=尖峰平谷）提取读数。"""
        period_keywords = {
            "尖峰": "sharp_peak", "尖": "sharp_peak",
            "正有功尖峰": "sharp_peak", "正有功尖": "sharp_peak",
            "峰": "peak", "正有功峰": "peak",
            "平": "flat", "正有功平": "flat",
            "谷": "valley", "正有功谷": "valley",
            "总": "total", "正有功总": "total", "合计": "total",
        }

        # 检测是否为转置格式
        category_col = None
        period_rows = {}  # row_idx -> period_key

        for col_idx in range(min(3, len(df.columns))):
            count = 0
            temp = {}
            for row_idx in range(min(len(df), 30)):
                cell = _cell_str(df.iloc[row_idx, col_idx])
                for kw, pkey in period_keywords.items():
                    if cell == kw or cell.startswith(kw):
                        temp[row_idx] = pkey
                        count += 1
                        break
            if count >= 3:
                period_rows = temp
                category_col = col_idx
                break

        if len(period_rows) < 3:
            return

        # 找数据列（发电量/上网电量/电表用量 等）
        data_cols = {}  # "forward" or "reverse" -> col_idx
        for row_idx in range(min(8, len(df))):
            for col_idx in range(len(df.columns)):
                if col_idx == category_col:
                    continue
                cell = _cell_str(df.iloc[row_idx, col_idx])
                if cell in ("发电量", "用电量", "正向用电量", "电表用理", "电表用量"):
                    data_cols.setdefault("forward", col_idx)
                elif cell in ("上网电量", "上网用电量", "反向用电量"):
                    data_cols.setdefault("reverse", col_idx)
                elif cell == "倍率":
                    data_cols.setdefault("multiplier", col_idx)

        # 如果没找到明确的数据列，用第一个有数值的列
        if not data_cols.get("forward"):
            for col_idx in range(category_col + 1, min(len(df.columns), 15)):
                for row_idx in period_rows:
                    val = _to_float(df.iloc[row_idx, col_idx])
                    if val is not None and val > 0:
                        data_cols["forward"] = col_idx
                        break
                if "forward" in data_cols:
                    break

        # 提取月份
        month = self._infer_month(filepath.name, sheet_name, source_info)
        for row_idx in range(min(5, len(df))):
            for col_idx in range(min(5, len(df.columns))):
                cell = _cell_str(df.iloc[row_idx, col_idx])
                m = _MONTH_RE.search(cell)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        month = f"{y}-{str(mo).zfill(2)}"
                        break

        if not month or month == "unknown":
            return

        # 找最近的电表号（从这个 sheet 的右侧列或标签中）
        sheet_meters = [mn for mn, info in self.meters.items()
                        if info["source_file"] == filepath.name]
        gen_meter = None
        grid_meter = None
        for mn in sheet_meters:
            mtype = self.meters[mn]["meter_type"]
            if mtype == "发电表":
                gen_meter = mn
            elif mtype == "上网表":
                grid_meter = mn
            else:
                if not gen_meter:
                    gen_meter = mn

        # 提取每个时段的值
        fwd_readings = {}
        rev_readings = {}
        fwd_total = None
        rev_total = None

        for row_idx, period in period_rows.items():
            fwd_val = _to_float(df.iloc[row_idx, data_cols["forward"]]) if "forward" in data_cols else None
            rev_val = _to_float(df.iloc[row_idx, data_cols["reverse"]]) if "reverse" in data_cols else None

            if period == "total":
                fwd_total = fwd_val
                rev_total = rev_val
            elif period in ("sharp_peak", "peak", "flat", "valley"):
                fwd_readings[period] = fwd_val
                rev_readings[period] = rev_val

        # 存入 readings
        if gen_meter and any(v is not None for v in fwd_readings.values()):
            key = (gen_meter, month)
            self.readings[key] = {
                "sharp_peak": fwd_readings.get("sharp_peak"),
                "peak": fwd_readings.get("peak"),
                "flat": fwd_readings.get("flat"),
                "valley": fwd_readings.get("valley"),
                "total_kwh": fwd_total or (sum(v for v in fwd_readings.values() if v) if fwd_readings else None),
                "source_file": filepath.name,
                "source_sheet": sheet_name,
            }

        if grid_meter and any(v is not None for v in rev_readings.values()):
            key = (grid_meter, month)
            self.readings[key] = {
                "sharp_peak": rev_readings.get("sharp_peak"),
                "peak": rev_readings.get("peak"),
                "flat": rev_readings.get("flat"),
                "valley": rev_readings.get("valley"),
                "total_kwh": rev_total or (sum(v for v in rev_readings.values() if v) if rev_readings else None),
                "source_file": filepath.name,
                "source_sheet": sheet_name,
            }

    # ========== 第6轮：补充信息 ==========

    # ========== 组装最终记录 ==========

    def _build_records(self) -> list[dict]:
        """把 meters + readings 组装成最终记录列表。"""
        records = []

        # 有读数的电表
        for (meter_number, month), reading in self.readings.items():
            if meter_number not in self.meters:
                continue
            info = self.meters[meter_number]
            rec = {
                "meter_number": meter_number,
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
            }
            records.append(rec)

        # 没有读数但有电表信息的（也入库，至少保存电表元数据）
        meters_with_readings = {mn for mn, _ in self.readings}
        for mn, info in self.meters.items():
            if mn not in meters_with_readings:
                rec = {
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
                }
                records.append(rec)

        log.info("多轮扫描最终结果: %d 个电表, %d 条记录", len(self.meters), len(records))
        return records

    def _merge_short_meters(self):
        """合并短电表号到长电表号。"""
        all_numbers = list(self.meters.keys())
        to_merge = []  # (short, long)

        for i, a in enumerate(all_numbers):
            if not re.match(r'^\d+$', a):
                continue
            for j, b in enumerate(all_numbers):
                if i == j or not re.match(r'^\d+$', b):
                    continue
                if len(a) < len(b) and a in b:
                    to_merge.append((a, b))

        for short, long in to_merge:
            if short not in self.meters:
                continue
            short_info = self.meters[short]
            long_info = self.meters[long]

            # 把短号的信息补到长号
            for field in ("asset_number", "user_id", "multiplier", "discount", "project_name"):
                if not long_info.get(field) and short_info.get(field):
                    long_info[field] = short_info[field]
            if long_info["meter_type"] == "未知" and short_info["meter_type"] != "未知":
                long_info["meter_type"] = short_info["meter_type"]

            # 读数也迁移
            for key in list(self.readings.keys()):
                if key[0] == short:
                    new_key = (long, key[1])
                    if new_key not in self.readings:
                        self.readings[new_key] = self.readings[key]
                    else:
                        # 补全
                        for f in ("sharp_peak", "peak", "flat", "valley", "total_kwh"):
                            if self.readings[new_key].get(f) is None:
                                self.readings[new_key][f] = self.readings[key].get(f)
                    del self.readings[key]

            del self.meters[short]
            log.info("  合并短电表号: %s -> %s", short, long)

    # ========== 工具方法 ==========

    def _find_header_row(self, df, max_scan=20) -> Optional[int]:
        """找表头行：关键词匹配最多的行。"""
        all_keywords = set()
        for field, aliases in self.field_mapping.items():
            if isinstance(aliases, list):
                all_keywords.update(aliases)
            elif isinstance(aliases, dict):
                for sub in aliases.values():
                    if isinstance(sub, list):
                        all_keywords.update(sub)
        all_keywords.update([
            "上月表数", "本月表数", "电表用量", "用电量", "发电量",
            "用户编号", "用户名称", "电表资产号", "统计日期",
            "尖", "峰", "平", "谷", "用户类型", "kWh",
        ])

        best_idx = None
        best_score = 0
        for i in range(min(len(df), max_scan)):
            row_strs = [_cell_str(df.iloc[i, c]) for c in range(len(df.columns))]
            score = sum(1 for cell in row_strs if cell and any(kw in cell for kw in all_keywords))
            if score > best_score:
                best_score = score
                best_idx = i

        return best_idx if best_score >= 2 else None

    def _matches_any(self, text: str, aliases: set) -> bool:
        """检查文本是否匹配任一别名（精确或包含）。"""
        if not text or not aliases:
            return False
        text = text.strip()
        # 精确匹配
        if text in aliases:
            return True
        # 包含匹配
        for a in aliases:
            if a in text or text in a:
                return True
        return False

    def _find_value_near(self, df, row_idx, col_idx) -> str:
        """在标签单元格的右边、下边、对角线找值。"""
        max_r, max_c = len(df), len(df.columns)

        # 右边
        if col_idx + 1 < max_c:
            val = _cell_str(df.iloc[row_idx, col_idx + 1])
            if val and not _CHINESE_RE.search(val):
                return val

        # 下边
        if row_idx + 1 < max_r:
            val = _cell_str(df.iloc[row_idx + 1, col_idx])
            if val and not _CHINESE_RE.search(val):
                return val

        # 对角线（右下）
        if row_idx + 1 < max_r and col_idx + 1 < max_c:
            val = _cell_str(df.iloc[row_idx + 1, col_idx + 1])
            if val and not _CHINESE_RE.search(val):
                return val

        # 再看右边两格
        if col_idx + 2 < max_c:
            val = _cell_str(df.iloc[row_idx, col_idx + 2])
            if val and not _CHINESE_RE.search(val):
                return val

        return ""

    def _find_nearest_meter(self, df, row_idx, col_idx) -> Optional[str]:
        """在附近找最近的已知电表号。"""
        # 优先同行
        for c in range(len(df.columns)):
            val = clean_id(_cell_str(df.iloc[row_idx, c]))
            if val in self.meters:
                return val

        # 上下几行
        for delta in range(1, 5):
            for r in [row_idx - delta, row_idx + delta]:
                if 0 <= r < len(df):
                    for c in range(len(df.columns)):
                        val = clean_id(_cell_str(df.iloc[r, c]))
                        if val in self.meters:
                            return val

        # 同 sheet 的第一个电表
        return None

    def _map_reading_columns(self, headers: list[str]) -> dict:
        """从表头映射尖峰平谷列。返回 {period_key: col_idx}。"""
        result = {}

        # 正向读数
        for sub_field, aliases in self._fwd_readings_cfg.items():
            if not isinstance(aliases, list):
                continue
            key = "sharp_peak" if "sharp" in sub_field else sub_field
            for col_idx, hdr in enumerate(headers):
                if any(a in hdr for a in aliases):
                    result.setdefault(key, col_idx)
                    break

        # 如果没找到正向，试反向
        if not result:
            for sub_field, aliases in self._rev_readings_cfg.items():
                if not isinstance(aliases, list):
                    continue
                key = "sharp_peak" if "sharp" in sub_field else sub_field
                for col_idx, hdr in enumerate(headers):
                    if any(a in hdr for a in aliases):
                        result.setdefault(key, col_idx)
                        break

        return result

    def _detect_type_from_text(self, text: str) -> str:
        """从文本推断电表类型。"""
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
        """从文件名/Sheet名/邮件日期推断月份。"""
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
        """从文件名提取项目名。"""
        # 常见模式：XXX项目、XXX电站
        m = re.search(r'([\u4e00-\u9fff]{2,10}(?:项目|电站|光伏))', filename)
        if m:
            return m.group(1)
        return None
