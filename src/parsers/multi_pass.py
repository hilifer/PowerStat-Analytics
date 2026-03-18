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
        self.pairs = []      # [(gen_meter_number, grid_meter_number)]

    def load_dataframes(self, sheets: list):
        self._sheets = sheets
        log.info("多轮扫描: 加载 %d 个 sheet", len(sheets))

    def extract_meters_only(self) -> list[dict]:
        """仅提取电表档案（不提取读数），用于首页刷新。"""
        log.info("=" * 50)
        log.info("开始多轮扫描提取（仅电表档案）")
        log.info("=" * 50)

        self._extract_meter_info()
        return self._build_records()

    def extract_all(self) -> list[dict]:
        log.info("=" * 50)
        log.info("开始多轮扫描提取（完整模式）")
        log.info("=" * 50)

        self._extract_meter_info()

        # === 动态数据 ===
        self._pass5_readings()
        log.info("读数提取完成: %d 条记录", len(self.readings))

        return self._build_records()

    def _extract_meter_info(self):
        """提取电表档案信息（pass0-pass4 + 交叉验证）。"""
        # === 预扫描：配对电表块 ===
        self._pass0_paired_blocks()

        # === 固定数据 ===
        self._pass1_meters_and_assets()
        self._pass2_user_ids()
        self._pass3_meter_types()
        self._pass4_fixed_attrs()

        # 合并短电表号
        self._merge_short_meters()

        # === 交叉验证：检测并清除冲突数据 ===
        self._pass6_cross_validate()

        # 打印电表档案
        log.info("电表档案建立完成: %d 个电表", len(self.meters))
        for mn, info in self.meters.items():
            log.info("  %s | 类型=%s | 资产=%s | 用户=%s | 倍率=%s | 项目=%s",
                     mn, info.get("meter_type"), info.get("asset_number"),
                     info.get("user_id"), info.get("multiplier"), info.get("project_name"))

    # ================================================================
    # 预扫描：配对电表块识别（统计表中的用户号+发电表+上网表块）
    # ================================================================

    # 块内标签正则：兼容两种格式
    # 格式1（表格型）: 单元格内容 = "用户号"，相邻单元格 = 值
    # 格式2（文本型）: 单元格内容 = "用电户号0950000088133431" 或 "发电表号：XXX"
    _BLOCK_LABEL_PATTERNS = [
        # (字段名, 正则, 组号)
        ("user_id", re.compile(
            r'(?:用户编号|用户号|用电户号|户号|客户编号)\s*[:：]?\s*(\d{6,20})'), 1),
        ("gen_meter", re.compile(
            r'(?:发电表号?|发电电表号?|发电表表号)\s*[:：]?\s*(\d{6,16})'), 1),
        ("grid_meter", re.compile(
            r'(?:上网表号?|上网电表号?|上网电表)\s*[:：]?\s*(\d{6,16})'), 1),
        ("gen_asset", re.compile(
            r'(?:发电表?资产编号?|发电资产[号产]?|发电表资产)\s*[:：]?\s*([0-9A-Za-z]{8,30})'), 1),
        ("grid_asset", re.compile(
            r'(?:上网表?资产[号产表]?|上网电表资产表?|上网表资产)\s*[:：]?\s*([0-9A-Za-z]{8,30})'), 1),
        ("multiplier", re.compile(
            r'(?:倍率|CT倍率|变比)\s*[:：]?\s*(\d+\.?\d*)'), 1),
    ]

    # 哪些标签关键字标识块内各字段（用于格式1的 label-then-value 模式）
    _BLOCK_LABEL_KW = {
        "user_id":     ["用户编号", "用户号", "用电户号", "户号", "客户编号"],
        "gen_meter":   ["发电表号", "发电电表号", "发电表", "发电表表号"],
        "grid_meter":  ["上网表号", "上网电表号", "上网电表", "上网表"],
        "gen_asset":   ["发电表资产编号", "发电表资产", "发电资产号", "发电资产产号"],
        "grid_asset":  ["上网表资产号", "上网表资产产", "上网表资产", "上网电表资产表", "上网电表资产"],
        "multiplier":  ["倍率", "CT倍率", "变比"],
    }

    def _pass0_paired_blocks(self):
        """预扫描：识别统计表中用户号+发电表+上网表配对块。

        支持两种格式：
        格式1（表格型）：标签在一个单元格，值在相邻单元格
        格式2（文本型）：标签和值在同一单元格内（如"用电户号0950000088133431"）

        配对块的特征：在一个较小区域（约10行内）同时出现用户号、发电表号、上网表号。
        """
        log.info("[预扫描] 识别配对电表块...")
        block_count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            # 收集所有标签命中：(row, field_name, value)
            hits = []

            for r in range(len(df)):
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell:
                        continue

                    # 策略A：内嵌格式（标签+值在同一单元格）
                    for field_name, pattern, grp in self._BLOCK_LABEL_PATTERNS:
                        m = pattern.search(cell)
                        if m:
                            hits.append((r, field_name, m.group(grp)))
                            break  # 一个单元格只取第一个匹配

                    # 策略B：标签单元格（纯标签，值在右侧或下方）
                    # 用最长关键字匹配，避免 "上网表资产产" 被 "上网表" 先匹配到 grid_meter
                    best_field, best_kw_len = None, 0
                    for field_name, kws in self._BLOCK_LABEL_KW.items():
                        for kw in kws:
                            if cell.strip() == kw:
                                kw_len = len(kw) + 1000  # 精确匹配最优
                            elif kw in cell and len(cell) <= len(kw) + 3:
                                kw_len = len(kw)
                            else:
                                continue
                            if kw_len > best_kw_len:
                                best_kw_len = kw_len
                                best_field = field_name
                    if best_field:
                        val = self._find_value_near(df, r, c)
                        if val:
                            if best_field in ("user_id",) and re.match(r'^\d{6,20}$', val):
                                hits.append((r, best_field, val))
                            elif best_field in ("gen_meter", "grid_meter") and _is_meter_like(val):
                                hits.append((r, best_field, val))
                            elif best_field in ("gen_asset", "grid_asset") and len(val) >= 8 and not _CHINESE_RE.search(val):
                                hits.append((r, best_field, val))
                            elif best_field == "multiplier":
                                fv = _to_float(val)
                                if fv and fv >= 1:
                                    hits.append((r, best_field, val))

                    # 策略C：处理 "上网表7月新装09001SG..." 这类非标准标签
                    m = re.search(r'(?:上网表|发电表)\d{1,2}月新装\s*[:：]?\s*([0-9A-Za-z]{8,30})', cell)
                    if m:
                        # 判断是上网还是发电
                        if "上网" in cell:
                            hits.append((r, "grid_asset", m.group(1)))
                        elif "发电" in cell:
                            hits.append((r, "gen_asset", m.group(1)))

            if not hits:
                continue

            # 按行排序
            hits.sort(key=lambda x: x[0])

            # 用滑动窗口聚合块：在10行范围内的命中归为一个块
            blocks = self._cluster_hits_into_blocks(hits, max_gap=10)

            # 提取项目名：目录路径 → sheet标题 → 文件名
            project_name = (self._extract_project_from_path(filepath)
                            or self._extract_project_from_sheet_title(df)
                            or self._extract_project_from_filename(filepath.name))

            for block in blocks:
                user_id = block.get("user_id")
                gen_meter = block.get("gen_meter")
                grid_meter = block.get("grid_meter")
                # 防止电表号被误当用户编号
                if user_id and (user_id == gen_meter or user_id == grid_meter):
                    user_id = None
                gen_asset = block.get("gen_asset")
                grid_asset = block.get("grid_asset")
                multiplier_str = block.get("multiplier")
                multiplier = _to_float(multiplier_str) if multiplier_str else None

                # 必须至少有一个电表号才有意义
                if not gen_meter and not grid_meter:
                    continue

                block_count += 1

                # 注册发电表
                if gen_meter and is_valid_meter_number(gen_meter):
                    self._register_meter(gen_meter, filepath.name, sheet_name, "发电表")
                    info = self.meters[gen_meter]
                    if gen_asset and not info["asset_number"]:
                        info["asset_number"] = gen_asset
                    if user_id and not info["user_id"]:
                        info["user_id"] = user_id
                    if multiplier and not info["multiplier"]:
                        info["multiplier"] = multiplier
                    if project_name and not info["project_name"]:
                        info["project_name"] = project_name

                # 注册上网表
                if grid_meter and is_valid_meter_number(grid_meter):
                    self._register_meter(grid_meter, filepath.name, sheet_name, "上网表")
                    info = self.meters[grid_meter]
                    if grid_asset and not info["asset_number"]:
                        info["asset_number"] = grid_asset
                    if user_id and not info["user_id"]:
                        info["user_id"] = user_id
                    if multiplier and not info["multiplier"]:
                        info["multiplier"] = multiplier
                    if project_name and not info["project_name"]:
                        info["project_name"] = project_name

                # 配对
                if gen_meter and grid_meter and is_valid_meter_number(gen_meter) and is_valid_meter_number(grid_meter):
                    self.pairs.append((gen_meter, grid_meter))
                    log.info("  配对块: 用户=%s 发电表=%s 上网表=%s 项目=%s",
                             user_id, gen_meter, grid_meter, project_name)

        log.info("  预扫描发现 %d 个配对块", block_count)

    def _cluster_hits_into_blocks(self, hits: list, max_gap: int = 10) -> list[dict]:
        """将按行排序的命中项聚合为块。同一块内行间距不超过 max_gap。"""
        blocks = []
        current_block = {}
        current_max_row = -999

        for row, field, value in hits:
            if row - current_max_row > max_gap and current_block:
                # 开始新块
                blocks.append(current_block)
                current_block = {}

            # 同一字段取第一个值（不覆盖）
            if field not in current_block:
                current_block[field] = value
            current_max_row = max(current_max_row, row)

        if current_block:
            blocks.append(current_block)

        return blocks

    def _extract_project_from_path(self, filepath) -> Optional[str]:
        """从文件路径的目录名提取项目名称。

        遍历路径的各级目录，查找包含中文的目录名作为项目名。
        跳过常见非项目目录（temp、archive、output 等）。
        """
        try:
            p = Path(filepath) if not isinstance(filepath, Path) else filepath
            skip_dirs = {"temp", "tmp", "archive", "output", "data", "temp_attachments",
                         "attachments", "uploads", "download", "downloads", "charts",
                         "logs", "config", "src", "test", "tests"}

            # 从内到外遍历目录
            for parent in p.parents:
                dirname = parent.name
                if not dirname or dirname in skip_dirs:
                    continue
                # 目录名包含中文 → 可能是项目名
                if _CHINESE_RE.search(dirname):
                    # 进一步清理：去掉日期后缀等
                    proj = re.sub(r'\d{4}[-_]\d{1,2}[-_]?\d{0,2}', '', dirname).strip(" -_")
                    if proj and len(proj) >= 2:
                        return proj
        except Exception:
            pass
        return None

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

                    # 竞争匹配：最长别名匹配决定列类型
                    cat = self._classify_header(hdr)
                    if cat == "asset":
                        asset_cols.append(c)
                    elif cat == "gen_meter":
                        meter_cols.append((c, "发电表"))
                        has_meter_col = True
                    elif cat == "grid_meter":
                        meter_cols.append((c, "上网表"))
                        has_meter_col = True
                    elif cat == "meter":
                        meter_cols.append((c, None))
                        has_meter_col = True

                # 为每个电表列找最近的资产列
                meter_asset_map = {}  # mc -> nearest_ac
                for mc, mtype in meter_cols:
                    if asset_cols:
                        meter_asset_map[mc] = min(asset_cols, key=lambda ac: (abs(ac - mc), -ac))

                # 按行提取：同一行的发电表和上网表自动配对
                for r in range(header_idx + 1, len(df)):
                    row_gen = None
                    row_grid = None
                    for mc, mtype in meter_cols:
                        val = clean_id(_cell_str(df.iloc[r, mc]))
                        if not is_valid_meter_number(val):
                            continue
                        self._register_meter(val, filepath.name, sheet_name, mtype)
                        # 同行最近资产编号
                        nearest_ac = meter_asset_map.get(mc)
                        if nearest_ac is not None:
                            av = clean_id(_cell_str(df.iloc[r, nearest_ac]))
                            if av and len(av) >= 4 and not _CHINESE_RE.search(av):
                                if av not in self.meters[val]["_asset_candidates"]:
                                    self.meters[val]["_asset_candidates"].append(av)
                                if not self.meters[val]["asset_number"]:
                                    self.meters[val]["asset_number"] = av
                                    asset_count += 1
                        # 记录配对
                        if mtype == "发电表":
                            row_gen = val
                        elif mtype == "上网表":
                            row_grid = val
                    # 同行配对
                    if row_gen and row_grid:
                        self.pairs.append((row_gen, row_grid))

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

                    # 竞争匹配：标签配对
                    cat = self._classify_header(cell)
                    if cat == "asset":
                        val = clean_id(self._find_value_near(df, r, c))
                        if val and len(val) >= 4 and not _CHINESE_RE.search(val):
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest:
                                if val not in self.meters[nearest]["_asset_candidates"]:
                                    self.meters[nearest]["_asset_candidates"].append(val)
                                if not self.meters[nearest]["asset_number"]:
                                    self.meters[nearest]["asset_number"] = val
                                    asset_count += 1
                    elif cat in ("meter", "gen_meter", "grid_meter"):
                        mtype = {"gen_meter": "发电表", "grid_meter": "上网表"}.get(cat)
                        val = clean_id(self._find_value_near(df, r, c))
                        if is_valid_meter_number(val):
                            self._register_meter(val, filepath.name, sheet_name, mtype)

                    # 内嵌格式："电表号：12345678" 或 "发电表号0950050038124235"
                    for pattern, mtype in [
                        (r'(?:电表号|表号|电能表号|表计编号)\s*[:：]?\s*(\d{8,16})', None),
                        (r'(?:发电表号?|发电电表号?|发电表表号)\s*[:：]?\s*(\d{8,16})', "发电表"),
                        (r'(?:上网表号?|上网电表号?|上网电表)\s*[:：]?\s*(\d{8,16})', "上网表"),
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

    def _is_known_meter_number(self, val: str) -> bool:
        """检查值是否是已知的电表号，防止电表号被误当用户编号。"""
        return val in self.meters

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
                                # 收集所有候选值
                                if uv not in self.meters[mn]["_user_id_candidates"]:
                                    self.meters[mn]["_user_id_candidates"].append(uv)
                                if not self._is_known_meter_number(uv):
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
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest:
                            if val not in self.meters[nearest]["_user_id_candidates"]:
                                self.meters[nearest]["_user_id_candidates"].append(val)
                        if not self._is_known_meter_number(val):
                            sheet_users.add(val)
                            if nearest and not self.meters[nearest]["user_id"]:
                                self.meters[nearest]["user_id"] = val
                                count += 1

                    # 内嵌格式（兼容冒号可选）
                    m = re.search(r'(?:用户编号|用户号|户号|用电户号)\s*[:：]?\s*(\d{6,20})', cell)
                    if m:
                        uid = m.group(1)
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest:
                            if uid not in self.meters[nearest]["_user_id_candidates"]:
                                self.meters[nearest]["_user_id_candidates"].append(uid)
                        if not self._is_known_meter_number(uid):
                            sheet_users.add(uid)
                            if nearest and not self.meters[nearest]["user_id"]:
                                self.meters[nearest]["user_id"] = uid
                                count += 1

            # C. 单用户 sheet → 关联给所有本文件电表
            # 再次过滤，排除可能混入的电表号
            sheet_users = {u for u in sheet_users if not self._is_known_meter_number(u)}
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

            # C. 项目名兜底：目录路径 → sheet标题 → 文件名
            proj = (self._extract_project_from_path(filepath)
                    or self._extract_project_from_sheet_title(df)
                    or self._extract_project_from_filename(filepath.name))
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
        # 构建配对索引：meter_number -> paired_meter_number
        pair_map = {}
        for gen, grid in self.pairs:
            pair_map.setdefault(gen, grid)
            pair_map.setdefault(grid, gen)

        # 配对电表共享用户编号和项目名
        for gen, grid in self.pairs:
            gen_info = self.meters.get(gen, {})
            grid_info = self.meters.get(grid, {})
            # 传递用户编号
            if gen_info.get("user_id") and not grid_info.get("user_id"):
                grid_info["user_id"] = gen_info["user_id"]
            elif grid_info.get("user_id") and not gen_info.get("user_id"):
                gen_info["user_id"] = grid_info["user_id"]
            # 传递项目名
            if gen_info.get("project_name") and not grid_info.get("project_name"):
                grid_info["project_name"] = gen_info["project_name"]
            elif grid_info.get("project_name") and not gen_info.get("project_name"):
                gen_info["project_name"] = grid_info["project_name"]

        if self.pairs:
            log.info("  配对关系: %d 对", len(self.pairs))
            for gen, grid in self.pairs[:5]:
                log.info("    发电表 %s ↔ 上网表 %s", gen, grid)

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
                "paired_meter": pair_map.get(mn),
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
                    "paired_meter": pair_map.get(mn),
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
    # 交叉验证
    # ================================================================

    def _pass6_cross_validate(self):
        """交叉验证：检测字段冲突，从候选列表中选替代值修复。

        规则：
        1. user_id 不能等于任何已知 meter_number
        2. asset_number 不能等于任何已知 meter_number
        3. user_id 和 asset_number 不能相同
        4. user_id 不应满足电表号格式特征（8-16位纯数字）

        冲突时从 _user_id_candidates / _asset_candidates 中选下一个合格值。
        """
        log.info("[交叉验证] 检查字段冲突…")
        all_meter_numbers = set(self.meters.keys())
        fixed = 0
        cleared = 0

        def _pick_valid_user_id(info):
            """从候选列表中选第一个合格的 user_id。"""
            for candidate in info.get("_user_id_candidates", []):
                if candidate in all_meter_numbers:
                    continue
                if _is_meter_like(candidate):
                    continue
                # 不能和 asset_number 相同
                if candidate == (info.get("asset_number") or ""):
                    continue
                return candidate
            return ""

        def _pick_valid_asset(info):
            """从候选列表中选第一个合格的 asset_number。"""
            for candidate in info.get("_asset_candidates", []):
                if candidate in all_meter_numbers:
                    continue
                if candidate == (info.get("user_id") or ""):
                    continue
                return candidate
            return ""

        for mn, info in self.meters.items():
            uid = info.get("user_id") or ""
            asset = info.get("asset_number") or ""

            # 规则1：user_id 不能是任何已知电表号
            if uid and uid in all_meter_numbers:
                new_uid = _pick_valid_user_id(info)
                if new_uid:
                    log.info("  电表 %s: user_id '%s' 与电表号冲突，替换为候选值 '%s'",
                             mn, uid, new_uid)
                    fixed += 1
                else:
                    log.warning("  电表 %s: user_id '%s' 与电表号冲突，无合格候选值，已清除",
                                mn, uid)
                    cleared += 1
                info["user_id"] = new_uid

            # 规则2：asset_number 不能是任何已知电表号
            if asset and asset in all_meter_numbers:
                new_asset = _pick_valid_asset(info)
                if new_asset:
                    log.info("  电表 %s: asset_number '%s' 与电表号冲突，替换为候选值 '%s'",
                             mn, asset, new_asset)
                    fixed += 1
                else:
                    log.warning("  电表 %s: asset_number '%s' 与电表号冲突，无合格候选值，已清除",
                                mn, asset)
                    cleared += 1
                info["asset_number"] = new_asset

            # 重新读取（可能已被上面修改）
            uid = info.get("user_id") or ""
            asset = info.get("asset_number") or ""

            # 规则3：user_id 和 asset_number 不能相同
            if uid and asset and uid == asset:
                new_uid = _pick_valid_user_id(info)
                if new_uid and new_uid != asset:
                    log.info("  电表 %s: user_id 与 asset_number 相同 ('%s')，user_id 替换为 '%s'",
                             mn, uid, new_uid)
                    info["user_id"] = new_uid
                    fixed += 1
                else:
                    log.warning("  电表 %s: user_id 与 asset_number 相同 ('%s')，user_id 已清除",
                                mn, uid)
                    info["user_id"] = ""
                    cleared += 1

            # 规则4：user_id 格式像电表号（8-16位纯数字）
            uid = info.get("user_id") or ""
            if uid and _is_meter_like(uid):
                new_uid = _pick_valid_user_id(info)
                if new_uid:
                    log.info("  电表 %s: user_id '%s' 格式像电表号，替换为候选值 '%s'",
                             mn, uid, new_uid)
                    info["user_id"] = new_uid
                    fixed += 1
                else:
                    log.warning("  电表 %s: user_id '%s' 格式像电表号，无合格候选值，已清除",
                                mn, uid)
                    info["user_id"] = ""
                    cleared += 1

        if fixed or cleared:
            log.info("  交叉验证: 修复 %d 个, 清除 %d 个（留待人工审核）", fixed, cleared)
        else:
            log.info("  交叉验证通过，无冲突")

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
                "_user_id_candidates": [],
                "_asset_candidates": [],
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
        """判断 text 是否匹配 aliases 中的某个别名。

        匹配规则：
        1. 精确匹配
        2. 子串匹配（alias in text 或 text in alias）
        3. 当 text 同时匹配多个类别的别名时，需用 _classify_header 做竞争
        """
        if not text or not aliases:
            return False
        text = text.strip()
        if text in aliases:
            return True
        for a in aliases:
            if a in text or text in a:
                return True
        return False

    def _best_match_length(self, text: str, aliases: set) -> int:
        """返回 text 与 aliases 中最长匹配别名的长度，无匹配返回 0。"""
        if not text or not aliases:
            return 0
        text = text.strip()
        best = 0
        for a in aliases:
            if text == a:
                return len(text) + 1000  # 精确匹配最优
            if a in text or text in a:
                best = max(best, len(a))
        return best

    # 所有参与竞争的类别及对应的别名集合（按需查表）
    def _classify_header(self, text: str) -> Optional[str]:
        """对表头文本做竞争匹配，返回最佳匹配类别。

        解决 "发电表资产编号" 同时包含 "发电表" 和 "资产编号" 的歧义：
        "资产编号"(4字) > "发电表"(3字)，归为 asset_number。
        """
        if not text:
            return None
        text = text.strip()

        categories = {
            "gen_meter":    self._gen_aliases,
            "grid_meter":   self._grid_aliases,
            "meter":        self._meter_aliases,
            "asset":        self._asset_aliases,
            "user":         self._user_aliases,
            "multiplier":   self._multiplier_aliases,
            "discount":     self._discount_aliases,
            "project":      self._project_aliases,
            "usage":        self._usage_aliases,
            "fwd_total":    self._fwd_total_aliases,
            "rev_total":    self._rev_total_aliases,
            "date":         self._date_aliases,
        }

        best_cat = None
        best_len = 0
        for cat, aliases in categories.items():
            ml = self._best_match_length(text, aliases)
            if ml > best_len:
                best_len = ml
                best_cat = cat
        return best_cat

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

    def _extract_project_name(self, text: str) -> Optional[str]:
        """从文本中提取项目名称（如 "特旺光伏项目"）。

        处理: "2025年4月特旺光伏项目发电统计表" -> "特旺光伏项目"
        排除: "月" 等日期字符被捕获到项目名开头
        """
        # 先用带排除的正则
        m = re.search(r'(?:[\d月日])([\u4e00-\u9fff]{2,10}(?:项目|电站|光伏))', text)
        if m:
            proj = m.group(1)
            # 去除开头的日期相关字符
            proj = proj.lstrip('年月日号')
            if len(proj) >= 2:
                return proj
        # 兜底：通用匹配
        m = re.search(r'([\u4e00-\u9fff]{2,10}(?:项目|电站|光伏))', text)
        if m:
            proj = m.group(1).lstrip('年月日号')
            if len(proj) >= 2:
                return proj
        return None

    def _extract_project_from_filename(self, filename: str) -> Optional[str]:
        return self._extract_project_name(filename)

    def _extract_project_from_sheet_title(self, df) -> Optional[str]:
        """从 sheet 前几行的标题中提取项目名。"""
        for r in range(min(5, len(df))):
            for c in range(min(5, len(df.columns))):
                cell = _cell_str(df.iloc[r, c])
                if not cell or len(cell) < 4:
                    continue
                proj = self._extract_project_name(cell)
                if proj:
                    return proj
        return None
