"""文本文件电表信息提取器。

从非表格的文本文件（txt、PDF 文字、OCR 结果等）中提取电表档案信息。
适用于类似以下格式的文本：

    用电户号0950000088133431
    上网电表：0950050038441846
    上网电表资产表：09001SG000000621050259​31
    发电表号：0950050038124235
    发电资产号09001SF00000042207348994

提取规则：
  - 用电户号 / 用户号 / 户号 → user_id
  - 上网电表 / 上网表 → meter_number (type=上网表)
  - 发电表 / 发电电表 → meter_number (type=发电表)
  - 上网电表资产表 / 上网表资产 / 发电资产号 → asset_number
  - 文件所在目录名 → project_name
"""

import re
from pathlib import Path

from src.logger import log
from src.parsers.validators import is_valid_meter_number, is_valid_asset_number

# ---- 正则模式 ----

# 用电户号
_USER_ID_RE = re.compile(
    r'(?:用电户号|用户编号|用户号|客户编号|户号)\s*[:：]?\s*'
    r'(\d{10,20})'
)

# 上网电表号（不含"资产"二字）
_GRID_METER_RE = re.compile(
    r'(?:上网电表|上网表)\s*(?!资产)(?:号)?\s*[:：]?\s*'
    r'(\d{10,20})'
)

# 上网电表资产编号
_GRID_ASSET_RE = re.compile(
    r'(?:上网电表资产表|上网电表资产|上网表资产|上网资产)\s*(?:号)?\s*[:：]?\s*'
    r'([0-9A-Za-z]{10,30})'
)

# 发电表号（不含"资产"二字）
_GEN_METER_RE = re.compile(
    r'(?:发电表|发电电表)\s*(?!资产)(?:号)?\s*[:：]?\s*'
    r'(\d{10,20})'
)

# 发电资产编号
_GEN_ASSET_RE = re.compile(
    r'(?:发电资产|发电表资产)\s*(?:号)?\s*[:：]?\s*'
    r'([0-9A-Za-z]{10,30})'
)

# 通用电表号（表号：xxx、电表号：xxx）
_GENERIC_METER_RE = re.compile(
    r'(?:电表号|表号|电能表号|表计编号)\s*[:：]\s*'
    r'(\d{10,20})'
)

# 通用资产号
_GENERIC_ASSET_RE = re.compile(
    r'(?:资产编号|资产号|设备编号)\s*[:：]?\s*'
    r'([0-9A-Za-z]{10,30})'
)

# "上网表N月新装XXX" 格式（追加的上网表）
_GRID_NEW_METER_RE = re.compile(
    r'上网表\d+月新装\s*[:：]?\s*'
    r'([0-9A-Za-z]{10,30})'
)


def extract_meters_from_text(text: str, filepath: str = "") -> list[dict]:
    """从文本中提取电表档案记录。

    Args:
        text: 文本内容（可以是多行）
        filepath: 文件路径（用于推断项目名）

    Returns:
        电表记录列表，每条包含 meter_number, user_id, meter_type,
        asset_number, project_name, source_file
    """
    if not text or not text.strip():
        return []

    # 去掉零宽字符等不可见字符
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)

    records = []
    source_file = Path(filepath).name if filepath else ""

    # 推断项目名：使用文件所在目录名
    project_name = _infer_project_from_path(filepath)

    # 提取用户编号（整个文本共享一个）
    user_ids = _USER_ID_RE.findall(text)
    user_id = user_ids[0] if user_ids else None

    # 先收集所有电表号，用于排除电表号被误当用户编号
    all_meter_numbers = set()
    for m in _GRID_METER_RE.finditer(text):
        all_meter_numbers.add(m.group(1))
    for m in _GEN_METER_RE.finditer(text):
        all_meter_numbers.add(m.group(1))
    if user_id and user_id in all_meter_numbers:
        log.warning("  用户编号 %s 与电表号重复，跳过", user_id)
        # 尝试使用第二个候选
        user_id = next((u for u in user_ids if u not in all_meter_numbers), None)

    # 提取上网电表
    for m in _GRID_METER_RE.finditer(text):
        meter_number = m.group(1)
        if is_valid_meter_number(meter_number):
            records.append({
                "meter_number": meter_number,
                "meter_type": "上网表",
                "user_id": user_id,
                "asset_number": None,
                "project_name": project_name,
                "source_file": source_file,
            })

    # 提取上网电表资产号 → 关联到最近的上网表
    for m in _GRID_ASSET_RE.finditer(text):
        asset = m.group(1)
        if is_valid_asset_number(asset):
            # 找到还没有资产号的上网表
            for rec in records:
                if rec["meter_type"] == "上网表" and not rec["asset_number"]:
                    rec["asset_number"] = asset
                    break

    # 提取 "上网表N月新装" 格式
    for m in _GRID_NEW_METER_RE.finditer(text):
        val = m.group(1)
        # 可能是资产号格式（含字母）或电表号（纯数字）
        if re.match(r'^\d+$', val) and is_valid_meter_number(val):
            records.append({
                "meter_number": val,
                "meter_type": "上网表",
                "user_id": user_id,
                "asset_number": None,
                "project_name": project_name,
                "source_file": source_file,
            })
        elif is_valid_asset_number(val):
            # 当作资产号，新建一条上网表记录
            records.append({
                "meter_number": None,
                "meter_type": "上网表",
                "user_id": user_id,
                "asset_number": val,
                "project_name": project_name,
                "source_file": source_file,
            })

    # 提取发电表
    for m in _GEN_METER_RE.finditer(text):
        meter_number = m.group(1)
        if is_valid_meter_number(meter_number):
            records.append({
                "meter_number": meter_number,
                "meter_type": "发电表",
                "user_id": user_id,
                "asset_number": None,
                "project_name": project_name,
                "source_file": source_file,
            })

    # 提取发电资产号 → 关联到最近的发电表
    for m in _GEN_ASSET_RE.finditer(text):
        asset = m.group(1)
        if is_valid_asset_number(asset):
            for rec in records:
                if rec["meter_type"] == "发电表" and not rec["asset_number"]:
                    rec["asset_number"] = asset
                    break

    # 通用电表号（如果以上都没匹配到）
    if not records:
        for m in _GENERIC_METER_RE.finditer(text):
            meter_number = m.group(1)
            if is_valid_meter_number(meter_number):
                records.append({
                    "meter_number": meter_number,
                    "meter_type": "未知",
                    "user_id": user_id,
                    "asset_number": None,
                    "project_name": project_name,
                    "source_file": source_file,
                })

        # 通用资产号关联
        for m in _GENERIC_ASSET_RE.finditer(text):
            asset = m.group(1)
            if is_valid_asset_number(asset):
                for rec in records:
                    if not rec["asset_number"]:
                        rec["asset_number"] = asset
                        break

    # 过滤掉没有 meter_number 的记录
    records = [r for r in records if r.get("meter_number")]

    if records:
        log.info("  文本提取: %s -> %d 个电表, user_id=%s, project=%s",
                 source_file, len(records), user_id, project_name)

    return records


def _infer_project_from_path(filepath: str) -> str | None:
    """从文件路径推断项目名称。

    规则：使用文件所在目录名（如果不是常见的根目录名称）。
    """
    if not filepath:
        return None

    parent = Path(filepath).parent
    dir_name = parent.name

    # 排除通用/临时目录名
    skip_names = {
        "", ".", "..", "temp", "tmp", "output", "temp_attachments",
        "attachments", "downloads", "data", "archive", "input",
    }
    if dir_name.lower() in skip_names:
        # 再往上一级试试
        grandparent = parent.parent.name
        if grandparent.lower() not in skip_names and grandparent:
            return grandparent
        return None

    return dir_name


def read_text_file(filepath: str) -> str:
    """读取文本文件，兼容多种编码。"""
    for enc in ["utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030", "big5"]:
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""
