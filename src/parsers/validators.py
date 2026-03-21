"""数据验证工具：确保电表号、用户编号等字段值合法。

所有解析器在入库前必须通过此模块验证。
"""

import re
from src.logger import log

# 汇总行关键词
SUMMARY_KEYWORDS = {"合计", "总计", "小计", "总合计", "汇总", "合 计", "总 计", "序号"}

# 编号只能包含数字、英文字母、横杠、点
_VALID_ID_RE = re.compile(r'^[0-9A-Za-z\-\.]+$')

# 包含中文字符
_HAS_CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')


def clean_id(val) -> str:
    """清理编号字段值。去除前缀、引号、.0 后缀等。"""
    if val is None:
        return ""
    val = str(val).strip()
    if not val or val.lower() in ("none", "nan", "null"):
        return ""
    # 去除常见中文前缀（兼容多种标签格式）
    # 注意：长模式必须在短模式前面，避免短模式先匹配
    val = re.sub(
        r'^(?:'
        # 资产类（长模式优先）
        r'上网电表资产表?|上网表?资产[号产表编]?|'
        r'发电表?资产编号?|发电资产[号产]?|'
        r'电表资产号?|资产编号|资产号|设备编号|'
        # 电表类（长模式优先）
        r'上网电表号?|上网表号?|'
        r'发电电表号?|发电表表号|发电表号?|'
        r'电表号|电能表号|表计编号|表号|'
        # 用户类
        r'用户编号|用电户号|用户号|客户编号|户号'
        r')'
        r'(?:\d{1,2}月新装)?\s*[:：]?\s*',
        '', val
    )
    # 去除引号
    val = val.strip("'\"''""` ")
    # 去除 .0 后缀（Excel 数字列常见）
    val = re.sub(r'\.0+$', '', val)
    return val.strip()


def is_valid_meter_number(val: str) -> bool:
    """验证电表号是否合法：纯数字字母，长度10-16，不含中文。

    实际电表号为10-16位纯数字（如0948030044326050）。
    8位以下的数字通常是资产号后缀、读数等，不是电表号。
    资产编号（如09001SF...）通常>18位，长度上限16可避免误识别。
    """
    if not val or len(val) < 10 or len(val) > 16:
        return False
    if _HAS_CHINESE_RE.search(val):
        return False
    if not _VALID_ID_RE.match(val):
        return False
    return True


def is_valid_user_id(val: str) -> bool:
    """验证用户编号是否合法：纯数字字母，长度≥6，不含中文。"""
    if not val:
        return True  # 用户编号允许为空
    if _HAS_CHINESE_RE.search(val):
        return False
    if len(val) < 6:
        return False
    if not _VALID_ID_RE.match(val):
        return False
    return True


def is_valid_asset_number(val: str) -> bool:
    """验证资产编号。"""
    if not val:
        return True
    if _HAS_CHINESE_RE.search(val):
        return False
    if not _VALID_ID_RE.match(val):
        return False
    return True


def is_summary_value(val) -> bool:
    """判断值是否为汇总行标识。"""
    if val is None:
        return False
    s = str(val).strip()
    return s in SUMMARY_KEYWORDS


def validate_record(rec: dict) -> dict | None:
    """验证并清洗一条电表记录。返回 None 表示无效记录。

    要求：
    - meter_number 必须存在且合法
    - user_id 如果存在必须合法，否则清空
    - asset_number 如果存在必须合法，否则清空
    """
    if not rec:
        return None

    # 清洗
    meter_number = clean_id(rec.get("meter_number"))
    user_id = clean_id(rec.get("user_id"))
    asset_number = clean_id(rec.get("asset_number"))

    # 跳过汇总行
    if is_summary_value(meter_number) or is_summary_value(user_id):
        return None

    # 电表号必须合法
    if not is_valid_meter_number(meter_number):
        return None

    # 用户编号：不合法则清空
    if not is_valid_user_id(user_id):
        user_id = ""

    # 资产编号：不合法则清空
    if not is_valid_asset_number(asset_number):
        asset_number = ""

    rec["meter_number"] = meter_number
    rec["user_id"] = user_id or None
    rec["asset_number"] = asset_number or None
    return rec


# ============================================================
# 抄表数据校验
# ============================================================

# 允许的误差（浮点运算 + 四舍五入）
_READING_TOLERANCE = 0.05


def validate_reading(reading: dict) -> list[dict]:
    """校验一条抄表记录的数据一致性。

    返回告警列表，每个告警为 {"level": "warn"|"error", "field": str, "msg": str}。
    空列表表示校验全通过。

    校验规则：
    1. 正向：总 ≈ 尖 + 峰 + 平 + 谷（如果分量和总值都有值）
    2. 反向：总 ≈ 尖 + 峰 + 平 + 谷（如果分量和总值都有值）
    3. 分量不应为负数
    """
    warnings = []

    # --- 正向有功校验 ---
    fwd_parts = {
        "sharp_peak": reading.get("sharp_peak"),
        "peak": reading.get("peak"),
        "flat": reading.get("flat"),
        "valley": reading.get("valley"),
    }
    fwd_total = reading.get("total_kwh")
    warnings.extend(_check_total_vs_parts("正向", fwd_parts, fwd_total))

    # --- 反向有功校验 ---
    rev_parts = {
        "rev_sharp_peak": reading.get("rev_sharp_peak"),
        "rev_peak": reading.get("rev_peak"),
        "rev_flat": reading.get("rev_flat"),
        "rev_valley": reading.get("rev_valley"),
    }
    rev_total = reading.get("rev_total")
    warnings.extend(_check_total_vs_parts("反向", rev_parts, rev_total))

    return warnings


def _check_total_vs_parts(direction: str, parts: dict, total) -> list[dict]:
    """校验总值与分量之和是否一致。"""
    warnings = []

    # 检查负数
    for field, val in parts.items():
        if val is not None and val < 0:
            warnings.append({
                "level": "error",
                "field": field,
                "msg": f"{direction}{field} 为负数: {val}",
            })
    if total is not None and total < 0:
        warnings.append({
            "level": "error",
            "field": f"{direction}_total",
            "msg": f"{direction}总 为负数: {total}",
        })

    # 校验 总 = 尖 + 峰 + 平 + 谷
    non_null = {k: v for k, v in parts.items() if v is not None}
    if total is not None and len(non_null) >= 2:
        calc_sum = sum(non_null.values())
        diff = abs(total - calc_sum)
        if diff > _READING_TOLERANCE:
            label_map = {
                "sharp_peak": "尖", "peak": "峰", "flat": "平", "valley": "谷",
                "rev_sharp_peak": "尖", "rev_peak": "峰", "rev_flat": "平", "rev_valley": "谷",
            }
            parts_str = "+".join(f"{label_map.get(k, k)}({v})" for k, v in non_null.items())
            warnings.append({
                "level": "warn",
                "field": f"{direction}_total_mismatch",
                "msg": f"{direction}总({total}) ≠ {parts_str} = {round(calc_sum, 2)}，差异 {round(diff, 2)}",
            })

    return warnings


def validate_reading_batch(readings: list[dict]) -> list[dict]:
    """批量校验抄表记录。

    返回有问题的记录列表，每条包含原始记录信息和告警：
    [{"meter_number": ..., "reading_month": ..., "warnings": [...]}]
    """
    issues = []
    for r in readings:
        warns = validate_reading(r)
        if warns:
            issues.append({
                "meter_number": r.get("meter_number"),
                "meter_type": r.get("meter_type"),
                "reading_month": r.get("reading_month"),
                "warnings": warns,
            })
    return issues
