"""多轮扫描提取器：笨方法版。

按两层结构提取电表档案，再提取月度读数：

第一层 · 核心身份（电表固有属性）：
  pass1：电表号 + 资产编号（基础身份，最先建立）
  配对块：补充电表 + 建立配对关系
  pass2：电表类型（发电表/上网表）
  pass3：倍率 + 折扣（计量/商务属性）

第二层 · 补充属性（关联/上下文）：
  pass4：用户编号（关联到电表）
  pass5：项目名 + 同用户互补共享

整理：
  合并短电表号
  pass6：交叉验证（清理冲突数据）

月度读数：
  标准表格 + 转置表（尖峰平谷）

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
        self.prices = {}     # (user_id, month) -> {sharp_peak_price, peak_price, ...}
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
        self._pass6_readings()
        log.info("读数提取完成: %d 条记录", len(self.readings))

        return self._build_records()

    def _extract_meter_info(self):
        """提取电表档案信息。

        按两层结构组织：

        第一层 · 核心身份（电表固有属性）：
          pass1：电表号 + 资产编号（基础身份，最先建立）
          配对块：补充电表 + 建立配对关系
          pass2：电表类型（发电表/上网表）
          pass3：倍率 + 折扣（计量/商务属性）

        第二层 · 补充属性（关联/上下文）：
          pass4：用户编号（关联到电表）
          pass5：项目名 + 同用户互补共享

        整理：
          合并短电表号
          交叉验证（清理冲突数据）
        """
        # ── 第一层：核心身份（电表固有属性） ──────────────

        # pass1：电表号 + 资产编号
        self._pass1_meters_and_assets()

        # 配对块：补充电表 + 建立配对关系
        self._paired_blocks()

        # pass2：电表类型
        self._pass2_meter_types()

        # pass3：倍率 + 折扣
        self._pass3_multiplier_and_discount()

        # ── 第二层：补充属性（关联/上下文） ──────────────

        # pass4：用户编号
        self._pass4_user_ids()

        # pass5：项目名 + 同用户互补共享
        self._pass5_supplementary_attrs()

        # ── 整理 ─────────────────────────────────────

        # 合并短电表号
        self._merge_short_meters()

        # 配对去重
        seen_pairs = set()
        unique_pairs = []
        for pair in self.pairs:
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                unique_pairs.append(pair)
        self.pairs = unique_pairs

        # 交叉验证：检测并清除冲突数据
        self._pass6_cross_validate()

        # 打印电表档案
        log.info("电表档案建立完成: %d 个电表", len(self.meters))
        for mn, info in self.meters.items():
            log.info("  %s | 类型=%s | 资产=%s | 用户=%s | 倍率=%s | 项目=%s",
                     mn, info.get("meter_type"), info.get("asset_number"),
                     info.get("user_id"), info.get("multiplier"), info.get("project_name"))

    # ================================================================
    # 配对块：识别统计表中的用户号+发电表+上网表块
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
    ]

    # 哪些标签关键字标识块内各字段（用于格式1的 label-then-value 模式）
    _BLOCK_LABEL_KW = {
        "user_id":     ["用户编号", "用户号", "用电户号", "户号", "客户编号"],
        "gen_meter":   ["发电表号", "发电电表号", "发电表", "发电表表号"],
        "grid_meter":  ["上网表号", "上网电表号", "上网电表", "上网表"],
        "gen_asset":   ["发电表资产编号", "发电表资产", "发电资产号", "发电资产产号"],
        "grid_asset":  ["上网表资产号", "上网表资产产", "上网表资产", "上网电表资产表", "上网电表资产"],
    }

    def _paired_blocks(self):
        """扫描统计表中的配对块，注册电表并建立配对关系。

        在 pass1 之后执行。此时基础电表已注册，
        本步骤补充统计表中发现的电表并建立发电表↔上网表配对。

        支持两种格式：
        格式1（表格型）：标签在一个单元格，值在相邻单元格
        格式2（文本型）：标签和值在同一单元格内（如"用电户号0950000088133431"）
        """
        log.info("[配对块] 扫描统计表中的配对电表块...")
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
                    best_field, best_kw_len = None, 0
                    for field_name, kws in self._BLOCK_LABEL_KW.items():
                        for kw in kws:
                            if cell.strip() == kw:
                                kw_len = len(kw) + 1000
                            elif kw in cell and len(cell) <= len(kw) + 3:
                                kw_len = len(kw)
                            else:
                                continue
                            if kw_len > best_kw_len:
                                best_kw_len = kw_len
                                best_field = field_name
                    if best_field:
                        nearby_vals = {v for (hr, hf, v) in hits if abs(hr - r) <= 10}
                        val = self._find_value_near(df, r, c,
                                                    field_name=best_field,
                                                    exclude_values=nearby_vals)
                        if val:
                            if best_field in ("user_id",) and re.match(r'^\d{6,20}$', val):
                                hits.append((r, best_field, val))
                            elif best_field in ("gen_meter", "grid_meter") and _is_meter_like(val):
                                hits.append((r, best_field, val))
                            elif best_field in ("gen_asset", "grid_asset") and len(val) >= 8 and not _CHINESE_RE.search(val):
                                hits.append((r, best_field, val))

                    # 策略C：处理 "上网表7月新装09001SG..." 这类非标准标签
                    m = re.search(r'(?:上网表|发电表)\d{1,2}月新装\s*[:：]?\s*([0-9A-Za-z]{8,30})', cell)
                    if m:
                        if "上网" in cell:
                            hits.append((r, "grid_asset", m.group(1)))
                        elif "发电" in cell:
                            hits.append((r, "gen_asset", m.group(1)))

            if not hits:
                continue

            hits.sort(key=lambda x: x[0])
            blocks = self._cluster_hits_into_blocks(hits, max_gap=10)

            project_name = (self._extract_project_from_path(filepath)
                            or self._extract_project_from_filename(filepath.name))

            for block in blocks:
                user_id = block.get("user_id")
                gen_meter = block.get("gen_meter")
                grid_meter = block.get("grid_meter")
                # 值冲突去重：同一个值不可能既是用户号又是电表号
                if user_id and user_id == gen_meter:
                    gen_meter = None
                if user_id and user_id == grid_meter:
                    grid_meter = None
                if gen_meter and gen_meter == grid_meter:
                    grid_meter = None

                # 必须至少有一个电表号才有意义
                if not gen_meter and not grid_meter:
                    continue

                block_count += 1
                gen_asset = block.get("gen_asset")
                grid_asset = block.get("grid_asset")

                # 只做三件事：注册电表 + 关联资产号 + 建立配对
                # user_id、multiplier、project_name 由后续专门的 pass 提取，不在此重复

                # 注册发电表
                if gen_meter and is_valid_meter_number(gen_meter):
                    self._register_meter(gen_meter, filepath.name, sheet_name, "发电表")
                    if gen_asset and not self.meters[gen_meter]["asset_number"]:
                        self.meters[gen_meter]["asset_number"] = gen_asset

                # 注册上网表
                if grid_meter and is_valid_meter_number(grid_meter):
                    self._register_meter(grid_meter, filepath.name, sheet_name, "上网表")
                    if grid_asset and not self.meters[grid_meter]["asset_number"]:
                        self.meters[grid_meter]["asset_number"] = grid_asset

                # 配对
                if gen_meter and grid_meter and is_valid_meter_number(gen_meter) and is_valid_meter_number(grid_meter):
                    self.pairs.append((gen_meter, grid_meter))
                    log.info("  配对块: 发电表=%s 上网表=%s", gen_meter, grid_meter)

        log.info("  发现 %d 个配对块", block_count)

    def _cluster_hits_into_blocks(self, hits: list, max_gap: int = 10) -> list[dict]:
        """将按行排序的命中项聚合为块。同一块内行间距不超过 max_gap。

        去重规则：同一个值不能被分配到多个字段（如 user_id 和 grid_meter 不能相同）。
        字段优先级：user_id > gen_meter > grid_meter > gen_asset > grid_asset
        """
        # 字段优先级（先出现的优先保留值）
        _FIELD_PRIORITY = {
            "user_id": 0, "gen_meter": 1, "grid_meter": 2,
            "gen_asset": 3, "grid_asset": 4,
        }

        blocks = []
        current_block = {}
        current_max_row = -999

        for row, field, value in hits:
            if row - current_max_row > max_gap and current_block:
                # 开始新块
                blocks.append(current_block)
                current_block = {}

            # 如果同一字段已有不同值，说明进入了新的记录块，需要拆分
            if field in current_block and current_block[field] != value:
                blocks.append(current_block)
                current_block = {}

            # 同一字段取第一个值（不覆盖）
            if field not in current_block:
                # 值去重：检查是否已被更高优先级字段占用
                conflict_field = None
                for existing_field, existing_val in current_block.items():
                    if existing_val == value:
                        conflict_field = existing_field
                        break
                if conflict_field is None:
                    current_block[field] = value
                else:
                    # 优先级高的（数值小）保留，低的丢弃
                    if _FIELD_PRIORITY.get(field, 99) < _FIELD_PRIORITY.get(conflict_field, 99):
                        del current_block[conflict_field]
                        current_block[field] = value
                    # 否则跳过当前 hit

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
                    # 进一步清理：去掉日期前缀/后缀
                    # 匹配 20260309_、2026-01、2025_04 等日期格式
                    proj = re.sub(r'\d{6,8}', '', dirname)  # 连续6-8位数字
                    proj = re.sub(r'\d{4}[-_]\d{1,2}[-_]?\d{0,2}', '', proj)  # 2025-04 格式
                    proj = proj.strip(" -_")
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
                        raw_cell = _cell_str(df.iloc[r, mc])
                        # 跳过包含用户号/资产号前缀的单元格（这不是电表号）
                        if self._cell_has_non_meter_prefix(raw_cell):
                            continue
                        val = clean_id(raw_cell)
                        if not is_valid_meter_number(val):
                            continue
                        self._register_meter(val, filepath.name, sheet_name, mtype)
                        if val not in self.meters:
                            continue
                        # 同行最近资产编号
                        nearest_ac = meter_asset_map.get(mc)
                        if nearest_ac is not None:
                            av = clean_id(_cell_str(df.iloc[r, nearest_ac]))
                            if av and len(av) >= 8 and not _CHINESE_RE.search(av) and not self._looks_like_reading(av):
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
                        val = clean_id(self._find_value_near(df, r, c, field_name="asset"))
                        if val and len(val) >= 8 and not _CHINESE_RE.search(val) and not self._looks_like_reading(val):
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest:
                                if val not in self.meters[nearest]["_asset_candidates"]:
                                    self.meters[nearest]["_asset_candidates"].append(val)
                                if not self.meters[nearest]["asset_number"]:
                                    self.meters[nearest]["asset_number"] = val
                                    asset_count += 1
                    elif cat in ("meter", "gen_meter", "grid_meter"):
                        mtype = {"gen_meter": "发电表", "grid_meter": "上网表"}.get(cat)
                        val = clean_id(self._find_value_near(df, r, c, field_name=cat))
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
    # pass4：用户编号（补充属性）
    # ================================================================

    def _is_known_meter_number(self, val: str, for_meter: str = None) -> bool:
        """检查值是否是已知的电表号，防止电表号被误当用户编号。

        特例：如果 val 是 for_meter 的配对方电表号，返回 False（允许作为用户号）。
        华尔特9个表中 用户号=上网表号 是合法的双重身份。
        """
        if val not in self.meters:
            return False
        # 如果是配对方，允许作为用户号
        if for_meter:
            for gen, grid in self.pairs:
                if (gen == for_meter and grid == val) or (grid == for_meter and gen == val):
                    return False
        return True

    def _pass4_user_ids(self):
        """扫描所有文件，找用户编号并关联到电表。"""
        log.info("[pass4] 扫描用户编号...")
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
                                if not self._is_known_meter_number(uv, for_meter=mn):
                                    if not self.meters[mn]["user_id"]:
                                        self.meters[mn]["user_id"] = uv
                                        count += 1
                                    sheet_users.add(uv)
                                    break

            # B. 标签配对（跳过表头行，避免误将表头中的编号当作数据）
            for r in range(len(df)):
                if header_idx is not None and r == header_idx:
                    continue
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell or not self._matches_any(cell, self._user_aliases):
                        continue
                    val = clean_id(self._find_value_near(df, r, c, field_name="user_id"))
                    if val and len(val) >= 6 and re.match(r'^\d+$', val):
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest:
                            if val not in self.meters[nearest]["_user_id_candidates"]:
                                self.meters[nearest]["_user_id_candidates"].append(val)
                        if not self._is_known_meter_number(val, for_meter=nearest):
                            sheet_users.add(val)
                            if nearest and not self.meters[nearest]["user_id"]:
                                self.meters[nearest]["user_id"] = val
                                count += 1

                    # 内嵌格式（兼容冒号可选、引号可选）
                    m = re.search(r'(?:用户编号|用户号|户号|用电户号)\s*[:：]?\s*[\'\"''""]*\s*(\d{6,20})', cell)
                    if m:
                        uid = m.group(1)
                        nearest = self._find_nearest_meter(df, r, c, filepath.name)
                        if nearest:
                            if uid not in self.meters[nearest]["_user_id_candidates"]:
                                self.meters[nearest]["_user_id_candidates"].append(uid)
                        if not self._is_known_meter_number(uid, for_meter=nearest):
                            sheet_users.add(uid)
                            if nearest and not self.meters[nearest]["user_id"]:
                                self.meters[nearest]["user_id"] = uid
                                count += 1

            # C. 单用户 sheet → 关联给所有本文件电表
            # 再次过滤，排除可能混入的电表号（但允许配对方电表号作为用户号）
            file_meters = {mn for mn, info in self.meters.items()
                          if info["source_file"] == filepath.name}
            def _is_pure_meter(u):
                """是否为纯粹的电表号（非配对方用户号）"""
                if u not in self.meters:
                    return False
                # 如果 u 是本文件某个电表的配对方，允许它作为用户号
                for fm in file_meters:
                    for gen, grid in self.pairs:
                        if (gen == fm and grid == u) or (grid == fm and gen == u):
                            return False
                return True
            sheet_users = {u for u in sheet_users if not _is_pure_meter(u)}
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

        # E. 配对方用户互补：如果配对中一方有user_id，另一方没有则继承
        for gen, grid in self.pairs:
            gen_uid = self.meters.get(gen, {}).get("user_id")
            grid_uid = self.meters.get(grid, {}).get("user_id")
            if gen_uid and not grid_uid:
                self.meters[grid]["user_id"] = gen_uid
                count += 1
            elif grid_uid and not gen_uid:
                self.meters[gen]["user_id"] = grid_uid
                count += 1

        log.info("  关联 %d 个用户编号", count)

    # ================================================================
    # pass2：电表类型（核心身份）
    # ================================================================

    def _pass2_meter_types(self):
        """扫描所有文件，判定电表类型。"""
        log.info("[pass2] 判定电表类型...")
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
    # pass3：倍率 + 折扣（核心身份 — 计量属性）
    # ================================================================

    def _pass3_multiplier_and_discount(self):
        """扫描所有文件，提取倍率和折扣并关联到电表。"""
        log.info("[pass3] 提取倍率/折扣...")
        count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # A. 表头列同行关联
            if header_idx is not None:
                meter_cols, mult_cols, disc_cols = [], [], []
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

                if meter_cols and (mult_cols or disc_cols):
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

            # B. 标签配对
            for r in range(len(df)):
                for c in range(len(df.columns)):
                    cell = _cell_str(df.iloc[r, c])
                    if not cell:
                        continue
                    if self._matches_any(cell, self._multiplier_aliases):
                        val = self._find_value_near(df, r, c, field_name="multiplier")
                        fv = _to_float(val)
                        if fv and fv >= 1:
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["multiplier"]:
                                self.meters[nearest]["multiplier"] = fv
                                count += 1
                    elif self._matches_any(cell, self._discount_aliases):
                        val = self._find_value_near(df, r, c, field_name="discount")
                        m = re.search(r'(\d+\.?\d*)', val) if val else None
                        if m:
                            dv = float(m.group(1))
                            if dv > 1:
                                dv = dv / 10
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["discount"]:
                                self.meters[nearest]["discount"] = dv
                                count += 1

        log.info("  提取 %d 个倍率/折扣", count)

    # ================================================================
    # pass5：项目名 + 同用户互补共享（补充属性）
    # ================================================================

    def _pass5_supplementary_attrs(self):
        """扫描所有文件，提取项目名，并做同用户互补共享。"""
        log.info("[pass5] 提取项目名 + 同用户互补共享...")
        count = 0

        for df, filepath, sheet_name, source_info in self._sheets:
            header_idx = self._find_header_row(df)

            # A. 表头列同行关联
            if header_idx is not None:
                meter_cols, proj_cols = [], []
                for c in range(len(df.columns)):
                    hdr = _cell_str(df.iloc[header_idx, c])
                    if not hdr:
                        continue
                    if self._matches_any(hdr, self._meter_aliases | self._gen_aliases | self._grid_aliases):
                        meter_cols.append(c)
                    if self._matches_any(hdr, self._project_aliases):
                        proj_cols.append(c)

                if meter_cols and proj_cols:
                    for r in range(header_idx + 1, len(df)):
                        mn = self._find_meter_in_row(df, r, meter_cols)
                        if not mn:
                            continue
                        info = self.meters[mn]
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
                    if self._matches_any(cell, self._project_aliases):
                        val = self._find_value_near(df, r, c, field_name="project")
                        if val and not re.match(r'^\d+$', val) and len(val) >= 2:
                            nearest = self._find_nearest_meter(df, r, c, filepath.name)
                            if nearest and not self.meters[nearest]["project_name"]:
                                self.meters[nearest]["project_name"] = val
                                count += 1

            # C. 项目名兜底：目录路径 → sheet标题 → 文件名
            proj = (self._extract_project_from_path(filepath)
                    or self._extract_project_from_filename(filepath.name))
            if proj:
                for mn, info in self.meters.items():
                    if info["source_file"] == filepath.name and not info["project_name"]:
                        info["project_name"] = proj
                        count += 1

        # D. 同用户电表互补共享（倍率、折扣、项目名）
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

        log.info("  关联 %d 个补充属性", count)

    # ================================================================
    # 月度读数
    # ================================================================

    def _pass6_readings(self):
        """扫描所有文件，提取每个电表每月的尖峰平谷读数。"""
        log.info("[第5轮] 提取月度读数...")

        for df, filepath, sheet_name, source_info in self._sheets:
            # 标准表格
            self._readings_from_table(df, filepath, sheet_name, source_info)
            # 转置表
            self._readings_from_transposed(df, filepath, sheet_name, source_info)

    def _readings_from_table(self, df, filepath, sheet_name, source_info):
        """从标准表格提取读数。

        支持：
        - 电表号列 / 用户编号列（兼作电表号）/ 资产号列（反查电表）
        - 同时提取正向读数和反向读数（同一行两组尖峰平谷）
        - 通过正向总/反向总列的位置区分重复的"尖/峰/平/谷"列名
        """
        header_idx = self._find_header_row(df)
        if header_idx is None:
            return

        headers = {}
        for c in range(len(df.columns)):
            hdr = _cell_str(df.iloc[header_idx, c])
            if hdr:
                headers[c] = hdr

        # === 识别标识列：电表号 > 用户编号 > 资产号 ===
        meter_cols = []
        user_id_cols = []
        asset_cols = []
        type_col = None  # "用户类型"/"用户类别" 列
        for c, hdr in headers.items():
            cat = self._classify_header(hdr)
            if cat in ("meter", "gen_meter", "grid_meter"):
                meter_cols.append(c)
            elif cat == "user":
                user_id_cols.append(c)
            elif cat == "asset":
                asset_cols.append(c)
            if "用户类型" in hdr or "用户类别" in hdr or "类别" in hdr:
                type_col = c

        # 优先级：电表号列 > 用户编号列 > 资产号列
        id_cols = meter_cols or user_id_cols or asset_cols
        id_source = "meter" if meter_cols else ("user_id" if user_id_cols else "asset")
        if not id_cols:
            return

        # 构建资产号→电表号的反查表
        asset_to_meter = {}
        for mn, info in self.meters.items():
            a = info.get("asset_number")
            if a:
                asset_to_meter[a] = mn

        # === 读数列映射：同时提取正向和反向 ===
        fwd_cols, rev_cols = self._map_reading_columns_dual(headers)
        if not fwd_cols and not rev_cols:
            return

        # 特殊列
        date_col = next((c for c, h in headers.items()
                         if self._matches_any(h, self._date_aliases)), None)
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

            # === 找电表号：按标识列类型匹配 ===
            meter_number = None
            if id_source == "meter":
                meter_number = self._find_meter_in_row(df, row_idx, id_cols)
            elif id_source == "user_id":
                # 用户编号列的值可能就是电表号（用户确认这是合法的）
                for ic in id_cols:
                    val = clean_id(_cell_str(df.iloc[row_idx, ic]))
                    if val in self.meters:
                        meter_number = val
                        break
            elif id_source == "asset":
                # 资产号反查电表号
                for ic in id_cols:
                    val = clean_id(_cell_str(df.iloc[row_idx, ic]))
                    if val in asset_to_meter:
                        meter_number = asset_to_meter[val]
                        break
                    # 资产号可能被截断，尝试前缀匹配
                    if val:
                        for full_asset, mn in asset_to_meter.items():
                            if full_asset.startswith(val) or val.startswith(full_asset):
                                meter_number = mn
                                break
                    if meter_number:
                        break

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

            # === 判断电表类型，决定正向/反向归属 ===
            row_type = _cell_str(row.iloc[type_col]) if type_col is not None else ""
            meter_type = self.meters.get(meter_number, {}).get("meter_type", "未知")
            detected_type = self._detect_type_from_text(row_type) if row_type else "未知"

            # 正向读数
            if fwd_cols:
                fwd_readings = {k: _to_float(row.iloc[c]) for k, c in fwd_cols.items()}
                fwd_total = _to_float(row.iloc[fwd_total_col]) if fwd_total_col is not None else None
                parts = [v for v in fwd_readings.values() if v is not None]
                if fwd_total is None and parts:
                    fwd_total = sum(parts)
                if any(v is not None for v in fwd_readings.values()) or fwd_total is not None:
                    self._upsert_reading(meter_number, row_month, fwd_readings,
                                         fwd_total, filepath.name, sheet_name)

            # 反向读数：写入配对的上网表（如果有）
            if rev_cols:
                rev_readings = {k: _to_float(row.iloc[c]) for k, c in rev_cols.items()}
                rev_total = _to_float(row.iloc[rev_total_col]) if rev_total_col is not None else None
                parts = [v for v in rev_readings.values() if v is not None]
                if rev_total is None and parts:
                    rev_total = sum(parts)
                if any(v is not None for v in rev_readings.values()) or rev_total is not None:
                    # 反向数据归到配对的上网表
                    paired = self._find_paired_meter(meter_number, "上网表")
                    target = paired or meter_number
                    self._upsert_reading(target, row_month, rev_readings,
                                         rev_total, filepath.name, sheet_name)

    def _readings_from_transposed(self, df, filepath, sheet_name, source_info):
        """从转置表（行=尖峰平谷）提取读数。

        支持多块（同一 sheet 内多个统计表块，如华尔特/新丰）。
        每个块以标题行（含"统计表"或"电费单"）开头，后跟表头和数据行。
        """
        period_keywords = {
            "尖峰": "sharp_peak", "尖": "sharp_peak",
            "正有功尖峰": "sharp_peak", "正有功尖": "sharp_peak",
            "正有功尖峰1": "sharp_peak",
            "峰": "peak", "正有功峰": "peak", "正有功峰2": "peak",
            "平": "flat", "正有功平": "flat",
            "谷": "valley", "正有功谷": "valley",
            "总": "total", "正有功总": "total", "合计": "total",
        }

        # 扫描整个 sheet，找出所有块的边界
        blocks = self._find_transposed_blocks(df, period_keywords)
        if not blocks:
            return

        for block in blocks:
            self._extract_one_transposed_block(
                df, filepath, sheet_name, source_info,
                block, period_keywords)

    def _find_transposed_blocks(self, df, period_keywords):
        """在 sheet 中找出所有转置表块。

        每个块 = {
            'title_row': int,       # 标题行索引
            'category_col': int,    # 类别列索引
            'period_rows': dict,    # {row_idx: period_key}
        }

        块边界识别：每当出现新的标题行（含"统计表"/"电费单"），或
        连续出现 ≥3 个 period 关键字行，就开始一个新块。
        """
        blocks = []
        max_rows = len(df)

        # 策略：找所有 period 行簇，按簇分块
        all_period_hits = []  # [(row_idx, col_idx, period_key)]
        for col_idx in range(min(3, len(df.columns))):
            for row_idx in range(max_rows):
                cell = _cell_str(df.iloc[row_idx, col_idx])
                for kw, pkey in period_keywords.items():
                    if cell == kw or cell.startswith(kw):
                        all_period_hits.append((row_idx, col_idx, pkey))
                        break

        if not all_period_hits:
            return []

        # 按行排序，聚合为块（连续行间隔 ≤ 3）
        all_period_hits.sort(key=lambda x: x[0])
        current_cluster = [all_period_hits[0]]
        clusters = []

        for hit in all_period_hits[1:]:
            if hit[0] - current_cluster[-1][0] <= 3 and hit[1] == current_cluster[0][1]:
                current_cluster.append(hit)
            else:
                if len(current_cluster) >= 3:
                    clusters.append(current_cluster)
                current_cluster = [hit]
        if len(current_cluster) >= 3:
            clusters.append(current_cluster)

        # 转化为块定义
        for cluster in clusters:
            category_col = cluster[0][1]
            period_rows = {}
            for row_idx, col_idx, pkey in cluster:
                if pkey not in period_rows.values():  # 同一周期只取第一个
                    period_rows[row_idx] = pkey
            # 找标题行（在 period 行上方 1-5 行内，包含中文项目名或"统计表"）
            first_period_row = min(period_rows.keys())
            title_row = max(0, first_period_row - 5)
            for r in range(first_period_row - 1, max(-1, first_period_row - 6), -1):
                if r < 0:
                    break
                cell = _cell_str(df.iloc[r, 0]) if len(df.columns) > 0 else ""
                if any(kw in cell for kw in ("统计表", "电费单", "项目", "光伏")):
                    title_row = r
                    break

            blocks.append({
                'title_row': title_row,
                'category_col': category_col,
                'period_rows': period_rows,
            })

        return blocks

    def _extract_one_transposed_block(self, df, filepath, sheet_name, source_info,
                                      block, period_keywords):
        """从一个转置表块中提取读数、电价、折扣。"""
        category_col = block['category_col']
        period_rows = block['period_rows']
        title_row = block['title_row']

        # 找数据列：优先 "发电量"/"用电量"，其次 "上网电量"/"反向用电量"
        # 注意：必须在 period_rows 上方的表头行搜索
        first_period = min(period_rows.keys())
        header_search_range = range(max(title_row, 0), first_period)

        # 高优先级关键字（已乘倍率的电量）
        fwd_kw_high = ("发电量", "用电量")
        fwd_kw_low = ("电表用理", "电表用量", "正向用电量")
        rev_kw_high = ("上网电量", "反向用电量", "上网用电量")
        rev_kw_low = ("反向用量",)
        price_kw = ("电价", "优惠后电价")
        amount_kw = ("金额",)
        actual_usage_kw = ("实际用电数",)

        data_cols = {}  # forward, reverse, price, amount, actual_usage
        for row_idx in header_search_range:
            for col_idx in range(len(df.columns)):
                if col_idx == category_col:
                    continue
                cell = _cell_str(df.iloc[row_idx, col_idx])
                if not cell:
                    continue
                # 高优先级覆盖低优先级
                if cell in fwd_kw_high:
                    data_cols["forward"] = col_idx  # 覆盖
                elif cell in fwd_kw_low and "forward" not in data_cols:
                    data_cols["forward"] = col_idx
                if cell in rev_kw_high:
                    data_cols["reverse"] = col_idx
                elif cell in rev_kw_low and "reverse" not in data_cols:
                    data_cols["reverse"] = col_idx
                if cell in price_kw:
                    data_cols["price"] = col_idx  # 取最后一个（优惠后电价 > 原电价）
                if cell in amount_kw:
                    data_cols.setdefault("amount", col_idx)
                if cell in actual_usage_kw:
                    data_cols.setdefault("actual_usage", col_idx)

        # 兜底：如果没找到 forward 列，用第一个有数值的列
        if "forward" not in data_cols:
            for col_idx in range(category_col + 1, min(len(df.columns), 15)):
                for row_idx in period_rows:
                    if _to_float(df.iloc[row_idx, col_idx]) is not None:
                        data_cols["forward"] = col_idx
                        break
                if "forward" in data_cols:
                    break

        # 月份：优先从标题行提取
        month = None
        for r in range(title_row, min(title_row + 3, len(df))):
            for c in range(min(10, len(df.columns))):
                cell_text = _cell_str(df.iloc[r, c])
                m = _MONTH_RE.search(cell_text)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if 2015 <= y <= 2035 and 1 <= mo <= 12:
                        month = f"{y}-{str(mo).zfill(2)}"
                        break
            if month:
                break
        if not month:
            month = self._infer_month(filepath.name, sheet_name, source_info)
        if not month or month == "unknown":
            return

        # 在块的 metadata 行中搜索电表号/用户号
        # 搜索范围：period 行上方（title_row → first_period）+ 下方（last_period+1 → +10）
        first_period = min(period_rows.keys())
        last_period = max(period_rows.keys())
        search_ranges = list(range(title_row, first_period)) + \
                         list(range(last_period + 1, min(last_period + 10, len(df))))
        block_gen_meter = block_grid_meter = block_user_id = None
        block_discount = None
        for r in search_ranges:
            row_text = " ".join(_cell_str(df.iloc[r, c])
                                for c in range(min(len(df.columns), 18)))
            if not row_text.strip():
                continue

            # 用户号
            um = re.search(r'(?:用户编号|用户号|用电户号)\s*[:：]?\s*[\'"]?(\d{6,20})', row_text)
            if um and not block_user_id:
                block_user_id = um.group(1)

            # 发电表号
            gm = re.search(r'(?:发电表号?|发电表)\s*[:：]?\s*(\d{8,16})', row_text)
            if gm and not block_gen_meter:
                block_gen_meter = gm.group(1)

            # 上网表号
            nm = re.search(r'(?:上网表号?|上网电表号?|上网表)\s*[:：]?\s*(\d{8,16})', row_text)
            if nm and not block_grid_meter:
                block_grid_meter = nm.group(1)

            # 折扣
            dm = re.search(r'(\d+\.?\d*)\s*折', row_text)
            if dm:
                dv = float(dm.group(1))
                if dv > 1:
                    dv = dv / 10
                if 0 < dv <= 1:
                    block_discount = dv

        # 如果块内发现新电表号，注册并配对
        if block_gen_meter and is_valid_meter_number(block_gen_meter):
            self._register_meter(block_gen_meter, filepath.name, sheet_name, "发电表")
            if block_user_id and not self.meters[block_gen_meter].get("user_id"):
                self.meters[block_gen_meter]["user_id"] = block_user_id
        if block_grid_meter and is_valid_meter_number(block_grid_meter):
            self._register_meter(block_grid_meter, filepath.name, sheet_name, "上网表")
            if block_user_id and not self.meters[block_grid_meter].get("user_id"):
                self.meters[block_grid_meter]["user_id"] = block_user_id
        if block_gen_meter and block_grid_meter:
            if is_valid_meter_number(block_gen_meter) and is_valid_meter_number(block_grid_meter):
                self.pairs.append((block_gen_meter, block_grid_meter))

        # 确定使用哪个电表：块内 > 文件级
        gen_meter = block_gen_meter
        grid_meter = block_grid_meter
        if not gen_meter or not grid_meter:
            file_meters = [mn for mn, info in self.meters.items()
                           if info["source_file"] == filepath.name]
            for mn in file_meters:
                mt = self.meters[mn]["meter_type"]
                if mt == "发电表" and not gen_meter:
                    gen_meter = mn
                elif mt == "上网表" and not grid_meter:
                    grid_meter = mn

        # 提取读数
        fwd_r, rev_r = {}, {}
        fwd_total = rev_total = None
        prices = {}  # period -> price
        amounts = {}  # period -> amount
        for row_idx, period in period_rows.items():
            fv = _to_float(df.iloc[row_idx, data_cols["forward"]]) if "forward" in data_cols else None
            rv = _to_float(df.iloc[row_idx, data_cols["reverse"]]) if "reverse" in data_cols else None
            pv = _to_float(df.iloc[row_idx, data_cols["price"]]) if "price" in data_cols else None
            av = _to_float(df.iloc[row_idx, data_cols["amount"]]) if "amount" in data_cols else None
            if period == "total":
                fwd_total, rev_total = fv, rv
            elif period in ("sharp_peak", "peak", "flat", "valley"):
                fwd_r[period] = fv
                rev_r[period] = rv
                if pv is not None:
                    prices[period] = pv
                if av is not None:
                    amounts[period] = av

        # 写入读数
        if gen_meter and gen_meter in self.meters and any(v is not None for v in fwd_r.values()):
            parts = [v for v in fwd_r.values() if v is not None]
            self._upsert_reading(gen_meter, month, fwd_r,
                                 fwd_total or (sum(parts) if parts else None),
                                 filepath.name, sheet_name)
            # 设置折扣
            if block_discount and not self.meters[gen_meter].get("discount"):
                self.meters[gen_meter]["discount"] = block_discount
        if grid_meter and grid_meter in self.meters and any(v is not None for v in rev_r.values()):
            parts = [v for v in rev_r.values() if v is not None]
            self._upsert_reading(grid_meter, month, rev_r,
                                 rev_total or (sum(parts) if parts else None),
                                 filepath.name, sheet_name)

        # 记录电价信息（存入 self.prices 供后续入库）
        if prices and block_user_id:
            price_key = (block_user_id, month)
            if price_key not in self.prices:
                self.prices[price_key] = {
                    "sharp_peak_price": prices.get("sharp_peak"),
                    "peak_price": prices.get("peak"),
                    "flat_price": prices.get("flat"),
                    "valley_price": prices.get("valley"),
                    "source_file": filepath.name,
                }

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
            uid = info.get("user_id")
            # 查找对应的价格数据
            price_data = self.prices.get((uid, month), {}) if uid else {}
            records.append({
                "meter_number": mn,
                "asset_number": info.get("asset_number"),
                "user_id": uid,
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
                "sharp_peak_price": price_data.get("sharp_peak_price"),
                "peak_price": price_data.get("peak_price"),
                "flat_price": price_data.get("flat_price"),
                "valley_price": price_data.get("valley_price"),
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
            # 更新配对引用
            self.pairs = [(long if g == short else g, long if n == short else n)
                          for g, n in self.pairs]
            del self.meters[short]
            log.info("  合并: %s -> %s", short, long)

    # ================================================================
    # pass6：交叉验证
    # ================================================================

    def _pass6_cross_validate(self):
        """交叉验证：检测字段冲突，从候选列表中选替代值修复。

        规则：
        1. user_id 不能等于任何已知 meter_number
        2. asset_number 不能等于任何已知 meter_number
        3. user_id 和 asset_number 不能相同

        冲突时从 _user_id_candidates / _asset_candidates 中选下一个合格值。
        注意：不再检查 user_id 是否"像电表号"（原规则4已移除），
        因为中国电网的用户编号本身就是纯数字串。
        """
        log.info("[交叉验证] 检查字段冲突…")
        all_meter_numbers = set(self.meters.keys())
        fixed = 0
        cleared = 0

        # 构建配对关系集合：如果 user_id 是当前电表的配对方，允许
        paired_with = {}  # meter_number -> set of paired meter numbers
        for gen, grid in self.pairs:
            paired_with.setdefault(gen, set()).add(grid)
            paired_with.setdefault(grid, set()).add(gen)

        def _pick_valid_user_id(info):
            """从候选列表中选第一个合格的 user_id。"""
            for candidate in info.get("_user_id_candidates", []):
                if candidate in all_meter_numbers:
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

            # 规则1：user_id 不能是其他电表的 meter_number（自身和配对方除外）
            # 华尔特9个表中 用户号=上网表号 是合法的双重身份
            if uid and uid in all_meter_numbers and uid != mn:
                # 如果 user_id 是配对方的电表号，允许（双重身份）
                if uid in paired_with.get(mn, set()):
                    pass  # 合法：用户号即配对方电表号
                else:
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

            # 规则4 已移除：中国电网的用户编号本身就是纯数字串，
            # 与电表号格式相似是正常的，不应因此清除。

        if fixed or cleared:
            log.info("  交叉验证: 修复 %d 个, 清除 %d 个（留待人工审核）", fixed, cleared)
        else:
            log.info("  交叉验证通过，无冲突")

    # ================================================================
    # 工具方法
    # ================================================================

    # 用户号/资产号前缀正则：出现这些前缀说明单元格不是电表号
    _NON_METER_PREFIX_RE = re.compile(
        r'(?:用户编号|用电户号|用户号|客户编号|户号|'
        r'资产编号|资产号|电表资产号?|设备编号)',
    )

    @staticmethod
    def _looks_like_reading(val: str) -> bool:
        """检查值是否像表码读数（小数或短纯数字），而非资产编号。

        资产编号通常含字母（如09001SF...）且长度>=18。
        纯数字短串或含小数点的值是读数，不是资产号。
        """
        if not val:
            return True
        # 含小数点 → 读数
        if '.' in val:
            return True
        # 纯数字且长度<10 → 不像资产编号
        if re.match(r'^\d+$', val) and len(val) < 10:
            return True
        return False

    def _cell_has_non_meter_prefix(self, raw_cell: str) -> bool:
        """检查单元格原始文本是否以用户号/资产号前缀开头。

        用于"电表号"列中混合了用户号、资产号的情况（如首熙/鑫海盈文件）。
        """
        if not raw_cell:
            return False
        # 去掉引号后检查
        s = raw_cell.strip("'\"''""` ")
        return bool(self._NON_METER_PREFIX_RE.match(s))

    def _register_meter(self, meter_number: str, source_file="", source_sheet="",
                        meter_type=None):
        """注册一个电表。"""
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

    def _find_value_near(self, df, row_idx, col_idx, field_name=None,
                         exclude_values=None) -> str:
        """在标签单元格附近查找字段值。

        搜索策略：收集附近所有候选值，按匹配质量排序返回最优。
        - 优先级：右侧 > 下方 > 右下对角 > 右侧第2格
        - 排除已被其他字段占用的值（exclude_values）
        - 根据 field_name 做格式偏好：
            user_id: 偏好较长数字串（≥10位）
            gen_meter/grid_meter: 偏好 _is_meter_like
            gen_asset/grid_asset: 偏好含字母的编号
            multiplier: 偏好小数字
        """
        max_r, max_c = len(df), len(df.columns)
        exclude = exclude_values or set()
        candidates = []  # [(value, priority)]

        for priority, (dr, dc) in enumerate([(0, 1), (1, 0), (1, 1), (0, 2)]):
            r, c = row_idx + dr, col_idx + dc
            if r < max_r and c < max_c:
                val = _cell_str(df.iloc[r, c])
                if val and not _CHINESE_RE.search(val) and val not in exclude:
                    candidates.append((val, priority))

        if not candidates:
            return ""

        # 无字段提示时，返回位置最近的（向后兼容）
        if not field_name:
            return candidates[0][0]

        # 按字段特征打分：分数越高越好
        def _score(val, positional_priority):
            s = 100 - positional_priority * 10  # 位置越近基础分越高
            if field_name in ("gen_meter", "grid_meter"):
                if _is_meter_like(val):
                    s += 50
                # 纯字母或含字母偏少 → 不太像电表号
                if re.search(r'[A-Za-z]', val):
                    s -= 30
            elif field_name == "user_id":
                if re.match(r'^\d{10,20}$', val):
                    s += 50  # 长数字串更像用户号
                elif re.match(r'^\d{6,9}$', val):
                    s += 20
            elif field_name in ("gen_asset", "grid_asset"):
                if re.search(r'[A-Za-z]', val) and len(val) >= 10:
                    s += 50  # 含字母的长编号更像资产号
                elif re.match(r'^\d+$', val):
                    s -= 20  # 纯数字不太像资产号
            elif field_name == "multiplier":
                fv = _to_float(val)
                if fv and 1 <= fv <= 200:
                    s += 50
            return s

        candidates.sort(key=lambda x: _score(x[0], x[1]), reverse=True)
        return candidates[0][0]

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
        """从表头映射尖峰平谷列（兼容旧调用，只返回正向）。"""
        fwd, _ = self._map_reading_columns_dual(headers)
        return fwd

    def _map_reading_columns_dual(self, headers: dict) -> tuple[dict, dict]:
        """从表头同时映射正向和反向的尖峰平谷列。

        利用"正向有功总"和"反向有功总"列的位置做分界：
        - 正向列组："正向有功总"列附近（之后）的尖峰平谷
        - 反向列组："反向有功总"列附近（之后）的尖峰平谷

        返回 (fwd_cols, rev_cols)，每个都是 {period_key: col_idx} dict。
        """
        # 先找 total 列位置做分界锚点
        fwd_total_col = None
        rev_total_col = None
        for c, hdr in headers.items():
            if self._matches_any(hdr, self._fwd_total_aliases) and fwd_total_col is None:
                fwd_total_col = c
            if self._matches_any(hdr, self._rev_total_aliases) and rev_total_col is None:
                rev_total_col = c

        # 尝试精确匹配（正向有功尖/反向有功尖 等带前缀的列名）
        fwd_cols = {}
        rev_cols = {}
        for sub_field, aliases in self._fwd_readings_cfg.items():
            if not isinstance(aliases, list):
                continue
            key = "sharp_peak" if "sharp" in sub_field else sub_field
            # 只匹配带前缀的别名（排除 bare "尖"/"峰"/"平"/"谷"）
            prefixed = [a for a in aliases if len(a) >= 2]
            for col_idx, hdr in headers.items():
                if any(a in hdr for a in prefixed):
                    fwd_cols.setdefault(key, col_idx)
                    break

        for sub_field, aliases in self._rev_readings_cfg.items():
            if not isinstance(aliases, list):
                continue
            key = "sharp_peak" if "sharp" in sub_field else sub_field
            for col_idx, hdr in headers.items():
                if col_idx in fwd_cols.values():
                    continue  # 已被正向占用
                if any(a in hdr for a in aliases):
                    rev_cols.setdefault(key, col_idx)
                    break

        # 如果精确匹配都找到了，直接返回
        if fwd_cols and rev_cols:
            return fwd_cols, rev_cols

        # 对于 bare "尖(kWh)" 重复列名的情况：用位置区分
        # 规则：在 fwd_total_col 之后、rev_total_col 之前的是正向
        #       在 rev_total_col 之后的是反向
        if not fwd_cols and fwd_total_col is not None:
            bare_map = {"尖": "sharp_peak", "峰": "peak", "平": "flat", "谷": "valley"}
            for col_idx, hdr in sorted(headers.items()):
                if rev_total_col is not None and col_idx >= rev_total_col:
                    break  # 进入反向区域
                if col_idx <= fwd_total_col:
                    continue  # 还没到正向数据区
                for bare, key in bare_map.items():
                    if bare in hdr and key not in fwd_cols:
                        fwd_cols[key] = col_idx
                        break

        if not rev_cols and rev_total_col is not None:
            bare_map = {"尖": "sharp_peak", "峰": "peak", "平": "flat", "谷": "valley"}
            for col_idx, hdr in sorted(headers.items()):
                if col_idx <= rev_total_col:
                    continue  # 还没到反向数据区
                for bare, key in bare_map.items():
                    if bare in hdr and key not in rev_cols:
                        rev_cols[key] = col_idx
                        break

        # 兜底：如果只找到一组，且没有 total 锚点区分，当作正向
        if not fwd_cols and not rev_cols:
            # 用原始逻辑：所有 forward aliases（含 bare 关键字）
            for sub_field, aliases in self._fwd_readings_cfg.items():
                if not isinstance(aliases, list):
                    continue
                key = "sharp_peak" if "sharp" in sub_field else sub_field
                for col_idx, hdr in headers.items():
                    if any(a in hdr for a in aliases):
                        fwd_cols.setdefault(key, col_idx)
                        break

        return fwd_cols, rev_cols

    def _find_paired_meter(self, meter_number: str, target_type: str) -> Optional[str]:
        """在配对关系中找指定类型的配对电表。"""
        for gen, grid in self.pairs:
            if target_type == "上网表":
                if gen == meter_number:
                    return grid
            elif target_type == "发电表":
                if grid == meter_number:
                    return gen
        return None

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

