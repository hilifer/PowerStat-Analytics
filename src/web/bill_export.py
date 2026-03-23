"""月度电费单 Excel 导出模块。

按照发电统计表格式生成每月电费单 Excel 文件：
  - 标题行：{项目名}光伏项目发电统计表（{YYYY}年{M}月）
  - 正向数据（发电量）：上月表数/本月表数/电表用量/倍率/发电量
  - 反向数据（上网电量）：上月表数/本月表数/电表用量/倍率/上网电量
  - 自发用电量/金额：实际用电数/原电价/优惠后电价/金额
  - 每用户一个表格，按尖峰/峰/平/谷/合计分行
"""

import io
import logging
import re
from collections import defaultdict, OrderedDict

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter

log = logging.getLogger(__name__)

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
                        month: str = None, selected_items: list = None) -> io.BytesIO:
    """生成月度电费单 Excel 文件（发电统计表格式）。

    Args:
        db: Database 实例
        project_name: 项目名筛选
        user_id: 用户编号筛选
        month: 月份 (YYYY-MM)，不传则导出所有月份
        selected_items: 可选，指定导出的 [{user_id, month}] 列表

    Returns:
        BytesIO 流，可直接发送给浏览器
    """
    # 获取抄表数据
    raw = db.get_readings_grouped(
        project_name=project_name,
        user_id=user_id,
        reading_month=month,
    )

    if selected_items:
        selected_keys = {(it["user_id"], it["month"]) for it in selected_items}
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


def _build_month_sheet(wb: Workbook, month_key: str,
                       user_map: dict, project_name: str = None):
    """为单月构建一个 Sheet。"""
    year, mon = month_key.split("-")
    sheet_name = f"{mon}月电费单"
    if sheet_name in wb.sheetnames:
        sheet_name = f"{month_key}电费单"
    ws = wb.create_sheet(title=sheet_name)

    for user_key in sorted(user_map.keys()):
        udata = user_map[user_key]
        _write_user_bill(ws, month_key, udata, project_name)


def _write_user_bill(ws, month_key: str, udata: dict, project_name: str = None):
    """写入单个用户的发电统计表。"""
    year, mon = month_key.split("-")
    gen = udata.get("gen")
    grid = udata.get("grid")
    prev_gen = udata.get("prev_gen")
    prev_grid = udata.get("prev_grid")

    if not gen and not grid:
        return

    gen_mult = gen["multiplier"] if gen else 1.0
    grid_mult = grid["multiplier"] if grid else 1.0
    discount = (gen.get("discount") or 1.0) if gen else 1.0
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
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=total_cols)
    title_cell = ws.cell(row=row, column=1, value=title_text)
    title_cell.font = _title_font
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
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
        d_price = (price * discount) if price is not None else None
        tier_amount = (self_use * d_price) if self_use is not None and d_price is not None else None

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

        row += 1

    # ---- 合计行 ----
    _apply_cell(ws.cell(row=row, column=1), "正有功总", _total_font, _total_fill, _left)
    _apply_cell(ws.cell(row=row, column=2), gen_prev_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=3), gen_cur_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=4), gen_diff_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=5), gen_mult, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=6), gen_amount_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=7), grid_prev_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=8), grid_cur_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=9), grid_diff_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=10), grid_mult, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=11), grid_amount_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=12), self_total, _total_font, _total_fill, _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=13), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=14), None, _total_font, _total_fill, _right)
    _apply_cell(ws.cell(row=row, column=15), amount_total, _total_font, _total_fill, _right, "#,##0.00")
