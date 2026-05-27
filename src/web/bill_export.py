"""月度电费单 Excel 导出模块。

按照发电统计表格式生成每月电费单 Excel 文件：
  - 标题行：{项目名}光伏项目发电统计表（{YYYY}年{M}月）
  - 正向数据（发电量）：上月表数/本月表数/电表用量/倍率/发电量
  - 反向数据（上网电量）：上月表数/本月表数/电表用量/倍率/上网电量
  - 自发用电量/金额：实际用电数/原电价/优惠后电价/金额
  - 每用户一个表格，按尖峰/峰/平/谷/合计分行
  - 电费结算单图片（如有）附加在表格下方
"""

import io
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter
from PIL import Image as PilImage

log = logging.getLogger(__name__)

_TEMP_ATTACHMENTS = Path("output/temp_attachments")

_SETTLEMENT_KEYWORDS = ["电费结算单"]


def _find_settlement_images(source_file: str, user_id: str,
                            project_name: str) -> list[str]:
    """从 source_file 所在目录中查找电费结算单图片。

    策略：
    1. 若目录名包含 project_name → 目录是项目专用，返回目录中所有结算单图片
    2. 否则按文件名匹配 user_id → project_name → 无匹配时返回空

    Args:
        source_file: 数据源文件路径（相对 temp_attachments）
        user_id: 用户编号
        project_name: 项目名

    Returns:
        匹配的图片完整路径列表
    """
    if not source_file:
        return []

    dir_path = _TEMP_ATTACHMENTS / os.path.dirname(source_file)
    if not dir_path.is_dir():
        return []

    image_exts = {".jpg", ".jpeg", ".png"}
    candidates = []
    try:
        for f in os.listdir(str(dir_path)):
            ext = os.path.splitext(f.lower())[1]
            if ext not in image_exts:
                continue
            if not any(kw in f for kw in _SETTLEMENT_KEYWORDS):
                continue
            candidates.append(f)
    except OSError:
        return []

    if not candidates:
        return []

    # 若目录名中包含项目名或用户编号 → 此目录为项目专用，返回所有候选
    if project_name and project_name in str(dir_path):
        return [str(dir_path / f) for f in candidates]
    if user_id and user_id in str(dir_path):
        return [str(dir_path / f) for f in candidates]

    # 多用户共享目录：按文件名精确匹配
    def match_score(fname: str) -> int:
        s = 0
        if user_id and user_id in fname:
            s += 100
        if project_name and project_name in fname:
            s += 90
        if project_name:
            # 项目名的 CJK 首部出现在文件名开头（如"完美"匹配"完美印刷"）
            cjk_chars = re.findall(r'[\u4e00-\u9fff]', project_name)
            for end in range(2, len(cjk_chars) + 1):
                prefix = "".join(cjk_chars[:end])
                if fname.startswith(prefix):
                    s = max(s, 60)
                    break
        return s

    scored = [(match_score(f), f) for f in candidates]
    scored.sort(key=lambda x: -x[0])

    best = scored[0][0] if scored else 0
    if best == 0:
        return []
    return [str(dir_path / f) for _, f in scored if _ == best]

# ---------- 样式常量 ----------

_thin = Side(style="thin")
_border = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
_right = Alignment(horizontal="right", vertical="center")

_title_font = Font(name="微软雅黑", size=14, bold=True)
_header_font = Font(name="微软雅黑", size=10, bold=True)
_data_font = Font(name="微软雅黑", size=10)
_total_font = Font(name="微软雅黑", size=10, bold=True)

_header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
_fwd_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
_rev_fill = PatternFill(start_color="DAEEF3", end_color="DAEEF3", fill_type="solid")
_self_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
_total_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")

# 时段定义
_TIERS = [
    ("正有功尖峰", "sharp_peak", "rev_sharp_peak", "sharp_peak_price"),
    ("正有功峰",   "peak",       "rev_peak",       "peak_price"),
    ("正有功平",   "flat",       "rev_flat",       "flat_price"),
    ("正有功谷",   "valley",     "rev_valley",     "valley_price"),
]

# 列定义 (col_index 1-based, header_text, width, group)
_COLUMNS = [
    (1,  "类别",       10),
    # 正向数据（发电量）
    (2,  "上月表数",   12),
    (3,  "本月表数",   12),
    (4,  "电表用量",   12),
    (5,  "倍率",       8),
    (6,  "发电量",     12),
    # 反向数据（上网电量）
    (7,  "上月表数",   12),
    (8,  "本月表数",   12),
    (9,  "电表用量",   12),
    (10, "倍率",       8),
    (11, "上网电量",   12),
    # 自发用电量/金额
    (12, "实际用电数", 12),
    (13, "原电价",     14),
    (14, "优惠后电价", 14),
    (15, "金额",       14),
    # 上网电量/金额
    (16, "原电价",     14),
    (17, "优惠电价",   14),
    (18, "上网金额",   14),
]


def _apply_cell(cell, value, font=None, fill=None, alignment=None, number_format=None):
    """设置单元格属性。"""
    cell.value = value
    cell.border = _border
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    cell.alignment = alignment or _center
    if number_format:
        cell.number_format = number_format


def _calc_prev_month(month_str: str):
    """计算上一个月份。"""
    m = re.match(r'(\d{4})-(\d{2})', month_str)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return f"{y-1}-12" if mo == 1 else f"{y}-{str(mo-1).zfill(2)}"


def generate_bill_excel(db, project_name: str = None, user_id: str = None,
                        month: str = None, selected_items: list = None,
                        month_is_reading: bool = False) -> io.BytesIO:
    """生成月度电费单 Excel 文件（发电统计表格式）。

    Args:
        db: Database 实例
        project_name: 项目名筛选
        user_id: 用户编号筛选
        month: 月份 (YYYY-MM)，默认作为账期月份
        selected_items: 可选，指定导出的 [{user_id, month}] 列表
        month_is_reading: True 时，month/selected_items 中的月份视为抄表月份
            （原始读数月份），不做 offset 转换；False 时视为账期月份。

    Returns:
        BytesIO 流，可直接发送给浏览器
    """
    from src.config_loader import config as _cfg
    bill_offset = int(_cfg.get("billing_month_offset", default=0))

    # 将账期月份转换为抄表月份查询；若 month_is_reading，则直接用作抄表月份
    if month_is_reading:
        reading_month = month or None
    else:
        reading_month = db.offset_month(month, -bill_offset) if month else None

    # 获取抄表数据
    raw = db.get_readings_grouped(
        project_name=project_name,
        user_id=user_id,
        reading_month=reading_month,
    )

    if selected_items:
        # selected_items 里的 month 默认是账期月份；month_is_reading 时直接作为抄表月份
        selected_keys = set()
        for it in selected_items:
            if month_is_reading:
                rm = it["month"]
            else:
                rm = db.offset_month(it["month"], -bill_offset)
            selected_keys.add((it["user_id"], rm))
        raw = [r for r in raw if (r.get("user_id"), r.get("reading_month")) in selected_keys]

    if not raw:
        wb = Workbook()
        ws = wb.active
        ws.title = "无数据"
        ws["A1"] = "未找到匹配的账单数据"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    # 获取所有涉及月份的上月数据
    months_in_data = set(r["reading_month"] for r in raw)
    prev_months_needed = set()
    for m in months_in_data:
        pm = _calc_prev_month(m)
        if pm:
            prev_months_needed.add(pm)

    prev_months_to_fetch = prev_months_needed - months_in_data
    prev_by_meter = {}  # meter_id -> {month -> reading}
    for pm in prev_months_to_fetch:
        prev_raw = db.get_readings_grouped(
            project_name=project_name,
            user_id=user_id,
            reading_month=pm,
        )
        for pr in prev_raw:
            prev_by_meter.setdefault(pr["meter_id"], {})[pm] = pr

    # 也把当前数据加入 prev_by_meter，以便跨月引用
    for r in raw:
        prev_by_meter.setdefault(r["meter_id"], {})[r["reading_month"]] = r

    # 按月份 -> 用户分组
    # month -> user_id -> {gen, grid, prev_gen, prev_grid}
    month_user_groups = OrderedDict()
    for r in raw:
        month_key = r["reading_month"]
        uid = r["user_id"] or "unknown"

        if month_key not in month_user_groups:
            month_user_groups[month_key] = OrderedDict()
        user_map = month_user_groups[month_key]

        if uid not in user_map:
            user_map[uid] = {
                "user_id": uid,
                "project_name": r["project_name"],
                "gen": None, "grid": None,
                "prev_gen": None, "prev_grid": None,
            }

        if r["meter_type"] == "发电表":
            user_map[uid]["gen"] = r
        elif r["meter_type"] == "上网表":
            user_map[uid]["grid"] = r

    # 附加上月数据引用
    for month_key, user_map in month_user_groups.items():
        pm = _calc_prev_month(month_key)
        for uid, udata in user_map.items():
            if pm:
                if udata["gen"]:
                    mid = udata["gen"]["meter_id"]
                    udata["prev_gen"] = prev_by_meter.get(mid, {}).get(pm)
                if udata["grid"]:
                    mid = udata["grid"]["meter_id"]
                    udata["prev_grid"] = prev_by_meter.get(mid, {}).get(pm)

    wb = Workbook()
    wb.remove(wb.active)

    for month_key in sorted(month_user_groups.keys()):
        user_map = month_user_groups[month_key]
        _build_month_sheet(wb, month_key, user_map, project_name)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _prev_month_key(month_key: str) -> str:
    """YYYY-MM → 上个月 YYYY-MM"""
    y, m = month_key.split("-")
    ny = str(int(y) - 1) if m == "01" else y
    nm = "12" if m == "01" else f"{int(m)-1:02d}"
    return f"{ny}-{nm}"


def _build_month_sheet(wb: Workbook, month_key: str,
                       user_map: dict, project_name: str = None):
    """为单月构建一个 Sheet。"""
    display_key = _prev_month_key(month_key)
    year, mon = display_key.split("-")
    sheet_name = f"{int(mon)}月电费单"
    if sheet_name in wb.sheetnames:
        sheet_name = f"{month_key}电费单"
    ws = wb.create_sheet(title=sheet_name)

    for user_key in sorted(user_map.keys()):
        udata = user_map[user_key]
        _write_user_bill(ws, month_key, udata, project_name)


def _write_user_bill(ws, month_key: str, udata: dict, project_name: str = None):
    """写入单个用户的发电统计表，下方附加电费结算单图片。"""
    display_key = _prev_month_key(month_key)
    year, mon = display_key.split("-")
    gen = udata.get("gen")
    grid = udata.get("grid")
    prev_gen = udata.get("prev_gen")
    prev_grid = udata.get("prev_grid")

    if not gen and not grid:
        return

    gen_mult = gen["multiplier"] if gen else 1.0
    grid_mult = grid["multiplier"] if grid else 1.0
    pricing_src = gen or grid
    pricing_mode = (pricing_src.get("pricing_mode") or "discount") if pricing_src else "discount"
    if pricing_mode == "discount":
        pricing_param = pricing_src.get("pricing_param") or pricing_src.get("discount") or 1.0
    else:
        pricing_param = pricing_src.get("pricing_param") if pricing_src and pricing_src.get("pricing_param") is not None else 0
    pname = project_name or (gen or grid).get("project_name") or ""
    uid = udata["user_id"]

    # 找起始行（跳过已有内容）
    start_row = ws.max_row + 1 if ws.max_row > 1 else 1
    if start_row > 1:
        start_row += 1  # 用户之间空一行

    row = start_row
    total_cols = len(_COLUMNS)

    # ---- 标题行 ----
    title_text = f"{pname}光伏项目发电统计表（{year}年{int(mon)}月）"
    stat_date = (gen or grid or {}).get("stat_date")
    if stat_date:
        title_text = f"{title_text}   抄表日期: {stat_date}"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
    title_cell = ws.cell(row=row, column=1, value=title_text)
    title_cell.font = _title_font
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(1, total_cols + 1):
        ws.cell(row=row, column=c).border = _border

    # ---- 电表信息行（发电表/上网表 表号、资产号、倍率） ----
    row += 1
    meter_info_parts = []
    if gen:
        parts = [f"发电表 {gen.get('meter_number', '')}"]
        if gen.get('asset_number'):
            parts.append(f"资产号 {gen['asset_number']}")
        parts.append(f"倍率 {gen_mult}")
        meter_info_parts.append("  ".join(parts))
    if grid:
        parts = [f"上网表 {grid.get('meter_number', '')}"]
        if grid.get('asset_number'):
            parts.append(f"资产号 {grid['asset_number']}")
        parts.append(f"倍率 {grid_mult}")
        meter_info_parts.append("  ".join(parts))
    if meter_info_parts:
        if pricing_mode == "fixed_discount":
            meter_info_parts.append(f"[优惠固定价 -{pricing_param:.4f}/kWh]")
        elif pricing_mode == "fixed_price":
            meter_info_parts.append(f"[固定价 {pricing_param:.8f}/kWh]")
        elif pricing_param != 1.0:
            meter_info_parts.append(f"[折扣 {pricing_param}]")
        info_text = "    ".join(meter_info_parts)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
        info_cell = ws.cell(row=row, column=1, value=info_text)
        info_cell.font = _data_font
        info_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        for c in range(1, total_cols + 1):
            ws.cell(row=row, column=c).border = _border

    # ---- 分组表头行 ----
    row += 1
    # 类别列（占1列，跨2行后面处理）
    _apply_cell(ws.cell(row=row, column=1), "", _header_font, _header_fill)
    # 正向数据（发电量）列 2-6
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
    _apply_cell(ws.cell(row=row, column=2), "正向数据（发电量）", _header_font, _fwd_fill)
    for c in range(3, 7):
        ws.cell(row=row, column=c).border = _border
        ws.cell(row=row, column=c).fill = _fwd_fill
    # 反向数据（上网电量）列 7-11
    ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=11)
    _apply_cell(ws.cell(row=row, column=7), "反向数据（上网电量）", _header_font, _rev_fill)
    for c in range(8, 12):
        ws.cell(row=row, column=c).border = _border
        ws.cell(row=row, column=c).fill = _rev_fill
    # 自发用电量/金额 列 12-15
    ws.merge_cells(start_row=row, start_column=12, end_row=row, end_column=15)
    _apply_cell(ws.cell(row=row, column=12), "自发用电量/金额", _header_font, _self_fill)
    for c in range(13, 16):
        ws.cell(row=row, column=c).border = _border
        ws.cell(row=row, column=c).fill = _self_fill
    # 上网电量/金额 列 16-18
    ws.merge_cells(start_row=row, start_column=16, end_row=row, end_column=18)
    _apply_cell(ws.cell(row=row, column=16), "上网电量/金额", _header_font, _rev_fill)
    for c in range(17, 19):
        ws.cell(row=row, column=c).border = _border
        ws.cell(row=row, column=c).fill = _rev_fill

    # ---- 列标题行 ----
    row += 1
    for col_idx, header, width in _COLUMNS:
        cell = ws.cell(row=row, column=col_idx, value=header)
        # 按分组设置不同背景色
        if col_idx == 1:
            _apply_cell(cell, header, _header_font, _header_fill)
        elif 2 <= col_idx <= 6:
            _apply_cell(cell, header, _header_font, _fwd_fill)
        elif 7 <= col_idx <= 11:
            _apply_cell(cell, header, _header_font, _rev_fill)
        elif 12 <= col_idx <= 15:
            _apply_cell(cell, header, _header_font, _self_fill)
        elif 16 <= col_idx <= 18:
            _apply_cell(cell, header, _header_font, _rev_fill)
        else:
            _apply_cell(cell, header, _header_font, _header_fill)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # ---- 数据行 ----
    row += 1
    # 累加器
    gen_prev_total = 0.0
    gen_cur_total = 0.0
    gen_diff_total = 0.0
    gen_amount_total = 0.0
    grid_prev_total = 0.0
    grid_cur_total = 0.0
    grid_diff_total = 0.0
    grid_amount_total = 0.0
    self_total = 0.0
    amount_total = 0.0
    grid_revenue_total = 0.0

    for tier_label, fwd_field, rev_field, price_field in _TIERS:
        # 正向（发电表）
        gp = prev_gen.get(fwd_field) if prev_gen else None
        gc = gen.get(fwd_field) if gen else None
        g_diff = (gc - gp) if gc is not None and gp is not None else None
        g_amount = (g_diff * gen_mult) if g_diff is not None else None

        # 反向（上网表）
        rp = prev_grid.get(rev_field) if prev_grid else None
        rc = grid.get(rev_field) if grid else None
        r_diff = (rc - rp) if rc is not None and rp is not None else None
        r_amount = (r_diff * grid_mult) if r_diff is not None else None

        # 自发用电
        self_use = (g_amount - r_amount) if g_amount is not None and r_amount is not None else None
        price = gen.get(price_field) if gen else None
        if pricing_mode == "fixed_discount":
            d_price = (price - pricing_param) if price is not None else None
        elif pricing_mode == "fixed_price":
            d_price = pricing_param
        else:
            d_price = (price * pricing_param) if price is not None else None
        tier_amount = (self_use * d_price) if self_use is not None and d_price is not None else None
        # 上网电价：基于上网表的原电价，按价格类型计算
        grid_price_field = f"grid_{price_field}"
        grid_tier_price = grid.get(grid_price_field) if grid else None
        if grid_tier_price is None:
            grid_tier_price = grid.get(price_field) if grid else None
        grid_avg_price = grid.get("grid_average_price") if grid else None
        if grid_avg_price is None:
            grid_avg_price = grid.get("average_price") if grid else None
        if grid_avg_price is not None:
            grid_tier_price = grid_avg_price  # 电费结算单均价覆盖分时原电价
        grid_pricing_mode = grid.get("pricing_mode") if grid else None
        grid_pricing_param = grid.get("pricing_param") if grid else None
        if grid_pricing_mode == "average":
            grid_price = grid_avg_price
        elif grid_pricing_mode == "fixed_price":
            grid_price = grid_pricing_param
        elif grid_pricing_mode == "fixed_discount" and grid_tier_price is not None:
            grid_price = (grid_tier_price - grid_pricing_param) if grid_pricing_param is not None else grid_tier_price
        else:
            grid_price = grid_tier_price
        grid_revenue = (r_amount * grid_price) if r_amount is not None and grid_price is not None else None

        # 累加
        if gp is not None:
            gen_prev_total += gp
        if gc is not None:
            gen_cur_total += gc
        if g_diff is not None:
            gen_diff_total += g_diff
        if g_amount is not None:
            gen_amount_total += g_amount
        if rp is not None:
            grid_prev_total += rp
        if rc is not None:
            grid_cur_total += rc
        if r_diff is not None:
            grid_diff_total += r_diff
        if r_amount is not None:
            grid_amount_total += r_amount
        if self_use is not None:
            self_total += self_use
        if tier_amount is not None:
            amount_total += tier_amount
        if grid_revenue is not None:
            grid_revenue_total += grid_revenue

        # 写入行
        _apply_cell(ws.cell(row=row, column=1), tier_label, _data_font, None, _left)
        # 正向
        _apply_cell(ws.cell(row=row, column=2), gp, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=3), gc, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=4), g_diff, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=5), gen_mult, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=6), g_amount, _data_font, None, _right, "#,##0.00")
        # 反向
        _apply_cell(ws.cell(row=row, column=7), rp, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=8), rc, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=9), r_diff, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=10), grid_mult, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=11), r_amount, _data_font, None, _right, "#,##0.00")
        # 自发用电
        _apply_cell(ws.cell(row=row, column=12), self_use, _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=13), price, _data_font, None, _right, "0.00000000")
        _apply_cell(ws.cell(row=row, column=14), d_price, _data_font, None, _right, "0.00000000")
        _apply_cell(ws.cell(row=row, column=15), tier_amount, _data_font, None, _right, "#,##0.00")
        # 上网数据
        _apply_cell(ws.cell(row=row, column=16), grid_tier_price, _data_font, None, _right, "0.00000000")
        _apply_cell(ws.cell(row=row, column=17), grid_price, _data_font, None, _right, "0.00000000")
        _apply_cell(ws.cell(row=row, column=18), grid_revenue, _data_font, None, _right, "#,##0.00")

        row += 1

    # ---- 合计行（正向直读总表数，反向直读反向有功总） ----
    gen_cur_total_val = gen.get("cur_total") if gen else None
    gen_prev_total_val = prev_gen.get("cur_total") if prev_gen else None
    gen_diff_total_val = (gen_cur_total_val - gen_prev_total_val) if gen_cur_total_val is not None and gen_prev_total_val is not None else None
    gen_amount_total_val = (gen_diff_total_val * gen_mult) if gen_diff_total_val is not None else None
    grid_rev_total_val = grid.get("rev_total") if grid else None
    grid_prev_total_val = prev_grid.get("rev_total") if prev_grid else None
    grid_diff_total_val = (grid_rev_total_val - grid_prev_total_val) if grid_rev_total_val is not None and grid_prev_total_val is not None else None
    grid_amount_total_val = (grid_diff_total_val * grid_mult) if grid_diff_total_val is not None else None
    self_total_val = (gen_amount_total_val - grid_amount_total_val) if gen_amount_total_val is not None and grid_amount_total_val is not None else None
    _apply_cell(ws.cell(row=row, column=1), "正有功总", _total_font, _total_fill, _left)
    _apply_cell(ws.cell(row=row, column=2), gen_prev_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=3), gen_cur_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=4), gen_diff_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=5), gen_mult, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=6), gen_amount_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=7), grid_prev_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=8), grid_rev_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=9), grid_diff_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=10), grid_mult, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=11), grid_amount_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=12), self_total_val, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=13), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=14), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=15), amount_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=16), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=17), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=18), grid_revenue_total, _total_font, _total_fill, _right, "#,##0.00")

    # ---- 电费结算单源文件 ----
    source_file = (gen or grid or {}).get("source_file")
    price_source = (gen or grid or {}).get("price_source")
    all_source_files = set()

    if source_file:
        dir_path = _TEMP_ATTACHMENTS / os.path.dirname(source_file)

        # 从目录扫描结算单图片
        for img_path in _find_settlement_images(source_file, uid, pname):
            all_source_files.add(img_path)

        # 从 price_records.source_file 查找单价提取源文件
        if price_source:
            p_path = str(dir_path / price_source)
            if os.path.isfile(p_path):
                all_source_files.add(p_path)

    if not all_source_files:
        return

    row += 1
    IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
    for src_path in sorted(all_source_files):
        if not os.path.isfile(src_path):
            log.warning("源文件不存在: %s", src_path)
            continue

        fname = os.path.basename(src_path)
        ext = os.path.splitext(fname)[1].lower()

        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
        label_cell = ws.cell(row=row, column=1,
                             value=f"电费结算单: {fname}")
        label_cell.font = Font(name="微软雅黑", size=10, bold=True, color="333333")
        label_cell.alignment = Alignment(horizontal="left", vertical="center")

        row += 1

        if ext in IMAGE_EXTS:
            try:
                pil_img = PilImage.open(src_path)
                orig_w, orig_h = pil_img.size
                pil_img.close()

                target_w_px = 800
                scale = target_w_px / orig_w if orig_w > 0 else 1.0
                target_h_pt = orig_h * scale * 0.75

                ws.row_dimensions[row].height = target_h_pt
                xl_img = XlImage(src_path)
                xl_img.width = target_w_px
                xl_img.height = orig_h * scale
                xl_img.anchor = f"A{row}"
                ws.add_image(xl_img)
            except Exception as e:
                log.error("插入图片失败 [%s]: %s", src_path, e)
                ws.cell(row=row, column=1, value=f"（图片加载失败: {fname}）")
        else:
            ws.cell(row=row, column=1,
                    value=f"（文件格式不支持嵌入: {fname}，请查看原始附件目录）")
            ws.cell(row=row, column=1).font = Font(name="微软雅黑", size=9, color="999999")

        row += 1
