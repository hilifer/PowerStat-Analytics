"""月度电费单 Excel 导出模块。

按照模板格式生成每月电费单 Excel 文件：
  - 标题行：{项目名} {X}月电费单
  - 正向数据（发电表/用电表）+ 反向数据（上网表）
  - 每个电表分行列出尖峰/峰/平/谷/合计
  - 含折扣行和合计行
"""

import io
import logging
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side, numbers,
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
_total_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")

# 类别标签
_TIER_LABELS = [
    ("sharp_peak", "正有功尖峰"),
    ("peak",       "正有功峰"),
    ("flat",       "正有功平"),
    ("valley",     "正有功谷"),
    ("total",      "正有功总"),
]

# 列定义 (col_index 1-based, header_text, width)
_COLUMNS = [
    (1,  "电表号",   14),
    (2,  "用户编号", 18),
    (3,  "类别",     12),
    (4,  "用电量",   12),
    (5,  "倍率",     8),
    (6,  "实际用电量", 12),
    (7,  "上网表号", 14),
    (8,  "反向用电量", 12),
    (9,  "净用电量", 12),
    (10, "电价",     12),
    (11, "金额",     14),
    (12, "备注",     30),
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


def generate_bill_excel(db, project_name: str = None, user_id: str = None,
                        month: str = None) -> io.BytesIO:
    """生成月度电费单 Excel 文件。

    Args:
        db: Database 实例
        project_name: 项目名筛选
        user_id: 用户编号筛选
        month: 月份 (YYYY-MM)，不传则导出所有月份

    Returns:
        BytesIO 流，可直接发送给浏览器
    """
    bills = db.get_monthly_bill(
        project_name=project_name, user_id=user_id,
        month_from=month, month_to=month,
    )

    if not bills:
        # 返回空工作簿
        wb = Workbook()
        ws = wb.active
        ws.title = "无数据"
        ws["A1"] = "未找到匹配的账单数据"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf

    # 按月份分组
    months_data = defaultdict(list)
    for b in bills:
        months_data[b["reading_month"]].append(b)

    wb = Workbook()
    wb.remove(wb.active)  # 删除默认 sheet

    for month_key in sorted(months_data.keys()):
        month_bills = months_data[month_key]
        _build_month_sheet(wb, db, month_key, month_bills, project_name)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _build_month_sheet(wb: Workbook, db, month_key: str,
                       month_bills: list, project_name: str = None):
    """为单月构建一个 Sheet。"""
    # Sheet 名
    year, mon = month_key.split("-")
    sheet_name = f"{mon}月电费单"
    if sheet_name in wb.sheetnames:
        sheet_name = f"{month_key}电费单"
    ws = wb.create_sheet(title=sheet_name)

    # 按用户分组（一个用户可能有多个电表）
    user_groups = defaultdict(list)
    for b in month_bills:
        key = b["user_id"] or b["meter_number"]
        user_groups[key].append(b)

    # ---- 标题行 ----
    title_text = f"{project_name or '电费单'} {int(mon)}月电费单"
    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row,
                   end_column=len(_COLUMNS))
    title_cell = ws.cell(row=row, column=1, value=title_text)
    title_cell.font = _title_font
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    # ---- 正向 / 反向 分组表头 ----
    row = 2
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    _apply_cell(ws.cell(row=row, column=1), "正向数据", _header_font, _header_fill)
    ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=9)
    _apply_cell(ws.cell(row=row, column=7), "反向数据", _header_font, _header_fill)
    ws.merge_cells(start_row=row, start_column=10, end_row=row, end_column=12)
    _apply_cell(ws.cell(row=row, column=10), "", _header_font, _header_fill)
    # 填充空单元格边框
    for c in range(1, len(_COLUMNS) + 1):
        cell = ws.cell(row=row, column=c)
        cell.border = _border
        if not cell.fill or cell.fill.fill_type is None:
            cell.fill = _header_fill

    # ---- 列标题 ----
    row = 3
    for col_idx, header, width in _COLUMNS:
        cell = ws.cell(row=row, column=col_idx, value=header)
        _apply_cell(cell, header, _header_font, _header_fill)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # ---- 数据行 ----
    row = 4
    grand_total_amount = 0.0
    grand_total_kwh = 0.0

    for user_key in sorted(user_groups.keys()):
        user_bills = user_groups[user_key]

        # 将同一用户的电表分为 发电表/用电表 和 上网表
        forward_meters = {}   # meter_number -> bill_dict
        reverse_meters = {}   # meter_number -> bill_dict

        for b in user_bills:
            if b["meter_type"] == "上网表":
                reverse_meters[b["meter_number"]] = b
            else:
                forward_meters[b["meter_number"]] = b

        # 如果没有正向电表，把所有数据当正向处理
        if not forward_meters:
            forward_meters = {b["meter_number"]: b for b in user_bills}
            reverse_meters = {}

        # 尝试找到配对的上网表
        for meter_num, fwd in forward_meters.items():
            # 查找配对上网表
            paired_reverse = None
            try:
                meter_info = db.get_meter(meter_num)
                if meter_info and meter_info.get("paired_meter_id"):
                    paired = db.get_paired_meter(meter_info["id"])
                    if paired and paired["meter_number"] in reverse_meters:
                        paired_reverse = reverse_meters[paired["meter_number"]]
            except Exception:
                pass

            row = _write_meter_rows(ws, row, fwd, paired_reverse)

        # 如果有未配对的上网表，也输出
        used_reverse = set()
        for meter_num, fwd in forward_meters.items():
            try:
                meter_info = db.get_meter(meter_num)
                if meter_info and meter_info.get("paired_meter_id"):
                    paired = db.get_paired_meter(meter_info["id"])
                    if paired:
                        used_reverse.add(paired["meter_number"])
            except Exception:
                pass

        for rev_num, rev in reverse_meters.items():
            if rev_num not in used_reverse:
                row = _write_meter_rows(ws, row, rev, None)

    # ---- 合计行 ----
    # 重新计算合计
    for b in month_bills:
        grand_total_amount += b.get("total_amount") or 0
        grand_total_kwh += (b.get("total_kwh") or 0) * (b.get("multiplier") or 1)

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    _apply_cell(ws.cell(row=row, column=1), "合计", _total_font, _total_fill, _center)
    for c in range(2, len(_COLUMNS) + 1):
        cell = ws.cell(row=row, column=c)
        cell.border = _border
        cell.fill = _total_fill

    _apply_cell(ws.cell(row=row, column=6), grand_total_kwh, _total_font, _total_fill,
                _right, "#,##0.00")
    _apply_cell(ws.cell(row=row, column=11), grand_total_amount, _total_font, _total_fill,
                _right, "#,##0.00")

    # ---- 折扣行 ----
    discount_vals = set(b.get("discount", 1.0) for b in month_bills if b.get("discount"))
    if discount_vals and discount_vals != {1.0}:
        row += 1
        discount_str = "、".join(f"{d}" for d in sorted(discount_vals))
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
        _apply_cell(ws.cell(row=row, column=1), f"折扣系数: {discount_str}",
                    _data_font, None, _left)
        for c in range(1, len(_COLUMNS) + 1):
            ws.cell(row=row, column=c).border = _border


def _write_meter_rows(ws, start_row: int, fwd_bill: dict,
                      rev_bill: dict = None) -> int:
    """写入单个电表的尖峰/峰/平/谷/合计行。返回下一可用行号。"""
    row = start_row
    meter_num = fwd_bill["meter_number"]
    user_id = fwd_bill.get("user_id") or ""
    multiplier = fwd_bill.get("multiplier") or 1.0
    discount = fwd_bill.get("discount") or 1.0

    # 配对上网表号
    rev_meter_num = rev_bill["meter_number"] if rev_bill else ""
    rev_multiplier = rev_bill.get("multiplier", 1.0) if rev_bill else 1.0

    tier_data = _get_tier_data(fwd_bill)
    rev_tier_data = _get_tier_data(rev_bill) if rev_bill else {}

    first_row = row
    for tier_key, tier_label in _TIER_LABELS:
        usage = tier_data.get(tier_key)
        price = _get_price(fwd_bill, tier_key)

        # 正向用电量（原始表码差 = 实际用电量 / 倍率）
        raw_usage = usage  # 数据库存的就是原始读数差
        actual_usage = (raw_usage or 0) * multiplier

        # 反向用电量
        rev_usage = rev_tier_data.get(tier_key)
        rev_actual = (rev_usage or 0) * rev_multiplier if rev_usage else None

        # 净用电量 = 实际用电量 - 反向用电量
        if rev_actual is not None:
            net_usage = actual_usage - rev_actual
        else:
            net_usage = actual_usage

        # 金额 = 净用电量 × 电价 × 折扣
        amount = None
        if price and net_usage:
            amount = round(net_usage * price * discount, 2)

        # 写入行
        _apply_cell(ws.cell(row=row, column=1), meter_num if row == first_row else "",
                    _data_font, None, _left)
        _apply_cell(ws.cell(row=row, column=2), user_id if row == first_row else "",
                    _data_font, None, _left)
        _apply_cell(ws.cell(row=row, column=3), tier_label, _data_font, None, _center)
        _apply_cell(ws.cell(row=row, column=4),
                    raw_usage if raw_usage else None,
                    _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=5),
                    multiplier if row == first_row else None,
                    _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=6),
                    actual_usage if actual_usage else None,
                    _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=7),
                    rev_meter_num if row == first_row else "",
                    _data_font, None, _left)
        _apply_cell(ws.cell(row=row, column=8),
                    rev_actual if rev_actual else None,
                    _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=9),
                    net_usage if (rev_actual is not None) else None,
                    _data_font, None, _right, "#,##0.00")
        _apply_cell(ws.cell(row=row, column=10),
                    price, _data_font, None, _right, "#,##0.00000000")
        _apply_cell(ws.cell(row=row, column=11),
                    amount, _data_font, None, _right, "#,##0.00")

        # 备注列
        note = ""
        if row == first_row:
            notes = []
            if fwd_bill.get("asset_number"):
                notes.append(f"资产号: {fwd_bill['asset_number']}")
            if fwd_bill.get("project_name"):
                notes.append(f"项目: {fwd_bill['project_name']}")
            note = "; ".join(notes)
        _apply_cell(ws.cell(row=row, column=12), note, _data_font, None, _left)

        row += 1

    # 合并电表号单元格
    if row - first_row > 1:
        ws.merge_cells(start_row=first_row, start_column=1,
                       end_row=row - 1, end_column=1)
        ws.merge_cells(start_row=first_row, start_column=2,
                       end_row=row - 1, end_column=2)
        ws.merge_cells(start_row=first_row, start_column=5,
                       end_row=row - 1, end_column=5)
        ws.merge_cells(start_row=first_row, start_column=7,
                       end_row=row - 1, end_column=7)

    return row


def _get_tier_data(bill: dict) -> dict:
    """从账单字典提取各时段用电量。"""
    return {
        "sharp_peak": bill.get("sharp_peak"),
        "peak": bill.get("peak"),
        "flat": bill.get("flat"),
        "valley": bill.get("valley"),
        "total": bill.get("total_kwh"),
    }


def _get_price(bill: dict, tier_key: str):
    """获取指定时段的电价。"""
    price_map = {
        "sharp_peak": "sharp_peak_price",
        "peak": "peak_price",
        "flat": "flat_price",
        "valley": "valley_price",
        "total": "average_price",
    }
    return bill.get(price_map.get(tier_key))
