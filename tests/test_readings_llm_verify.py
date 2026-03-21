"""用大模型独立提取表码数据，与程序提取结果做对比校验。

测试方法：
1. 大模型（Claude）独立阅读含完整表码格式的 Excel/XLS 文件
   （必须同时包含：日期、正向 总/尖/峰/平/谷、反向 总/尖/峰/平/谷）
2. 程序端用 MultiPassExtractor 提取同样的数据
3. 逐字段对比两者结果，全部一致则通过

大模型提取结果作为 fixtures 固化在本文件中（由 Claude 阅读原始 Excel 后生成）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import SmartDispatcher
from src.parsers.multi_pass import MultiPassExtractor

# 允许的数值误差
TOLERANCE = 0.01

# ============================================================
# 大模型提取的 fixtures（由 Claude 阅读原始 Excel 后生成）
#
# 数据来源：表码数据表（每行一个电表，包含日期+正反向有功读数）
# 这些值是累计表码读数（本月表数），不是差值（电表用理）
# ============================================================

LLM_FIXTURES = [
    # ================================================================
    # 文件1: 耀嵘12月表码数据.xlsx (Sheet1)
    # 列: 用户名称|用户编号|表计资产编号|用户类别|数据时间|
    #      正向有功总(kWh)|正向有功尖(kWh)|正向有功峰(kWh)|正向有功平(kWh)|正向有功谷(kWh)|
    #      正向无功总(kVarh)|
    #      反向有功总(kWh)|反向有功尖(kWh)|反向有功峰(kWh)|反向有功平(kWh)|反向有功谷(kWh)|...
    # ================================================================
    {
        "file": "耀嵘12月表码数据.xlsx",
        "file_path": "output/archive/unknown/耀嵘/耀嵘12月表码数据.xlsx",
        "meter_number": "0319009900152123",
        "reading_month": "2026-01",
        "fwd_total": 11403.54, "fwd_sharp_peak": 1149.03, "fwd_peak": 2149.65,
        "fwd_flat": 4437.61, "fwd_valley": 3667.24,
        "rev_total": 31.95, "rev_sharp_peak": 8.31, "rev_peak": 7.98,
        "rev_flat": 14.79, "rev_valley": 0.86,
        "description": "耀嵘表码 公线专变客户 0319009900152123 2026-01",
    },
    {
        "file": "耀嵘12月表码数据.xlsx",
        "file_path": "output/archive/unknown/耀嵘/耀嵘12月表码数据.xlsx",
        "meter_number": "0319700356999007",
        "reading_month": "2026-01",
        "fwd_total": 11403.54, "fwd_sharp_peak": 1149.03, "fwd_peak": 2149.65,
        "fwd_flat": 4437.61, "fwd_valley": 3667.24,
        "rev_total": 31.95, "rev_sharp_peak": 8.31, "rev_peak": 7.98,
        "rev_flat": 14.79, "rev_valley": 0.86,
        "description": "耀嵘表码 地方电厂户 0319700356999007 2026-01",
    },
    {
        "file": "耀嵘12月表码数据.xlsx",
        "file_path": "output/archive/unknown/耀嵘/耀嵘12月表码数据.xlsx",
        "meter_number": "0319700337706620",
        "reading_month": "2026-01",
        "fwd_total": 2421.86, "fwd_sharp_peak": 653.03, "fwd_peak": 624.5,
        "fwd_flat": 1053.65, "fwd_valley": 90.67,
        "rev_total": 1.04, "rev_sharp_peak": 0.0, "rev_peak": 0.04,
        "rev_flat": 0.44, "rev_valley": 0.55,
        "description": "耀嵘表码 光伏发电客户 0319700337706620 2026-01",
    },

    # ================================================================
    # 文件2: 耀嵘.xls (用户表码1)
    # 列: 电表资产号|用户编号|统计日期|正向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)|
    #      反向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)
    # 含2个月数据 (2026-02 和 2026-01)
    # ================================================================
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "0946000082501856",
        "reading_month": "2026-02",
        "fwd_total": 10828.33, "fwd_sharp_peak": 940.39, "fwd_peak": 3061.03,
        "fwd_flat": 4092.24, "fwd_valley": 2734.65,
        "rev_total": 32.05, "rev_sharp_peak": 7.21, "rev_peak": 6.28,
        "rev_flat": 18.51, "rev_valley": 0.03,
        "description": "耀嵘.xls 0946000082501856 2026-02",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "0946070038961957",
        "reading_month": "2026-02",
        "fwd_total": 2628.87, "fwd_sharp_peak": 717.44, "fwd_peak": 682.36,
        "fwd_flat": 1137.93, "fwd_valley": 91.12,
        "rev_total": 1.01, "rev_sharp_peak": 0.0, "rev_peak": 0.07,
        "rev_flat": 0.42, "rev_valley": 0.51,
        "description": "耀嵘.xls 0946070038961957 2026-02",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "0946000082501856",
        "reading_month": "2026-01",
        "fwd_total": 10671.06, "fwd_sharp_peak": 924.71, "fwd_peak": 3034.93,
        "fwd_flat": 4034.88, "fwd_valley": 2676.52,
        "rev_total": 27.92, "rev_sharp_peak": 6.4, "rev_peak": 5.7,
        "rev_flat": 15.77, "rev_valley": 0.03,
        "description": "耀嵘.xls 0946000082501856 2026-01",
    },
    {
        "file": "耀嵘.xls",
        "file_path": "output/archive/unknown/_未分类/耀嵘.xls",
        "meter_number": "0946070038961957",
        "reading_month": "2026-01",
        "fwd_total": 2419.41, "fwd_sharp_peak": 657.21, "fwd_peak": 626.48,
        "fwd_flat": 1046.07, "fwd_valley": 89.63,
        "rev_total": 0.9, "rev_sharp_peak": 0.0, "rev_peak": 0.06,
        "rev_flat": 0.38, "rev_valley": 0.46,
        "description": "耀嵘.xls 0946070038961957 2026-01",
    },

    # ================================================================
    # 文件3: 1月用户表码（全部） (洲千).xls (用户表码1)
    # 列: 用户编号|用户名称|用户类型|用电地址|电表资产号|终端资产编号|终端地址|测量点号|
    #      统计日期|正向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)|
    #      反向有功总(kWh)|尖(kWh)|峰(kWh)|平(kWh)|谷(kWh)
    # 26条记录，统计日期=2026-02-01
    # 选取部分代表性记录作为测试样本
    # ================================================================
    # 公变客户
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030028524986",
        "reading_month": "2026-02",
        "fwd_total": 1076.68, "fwd_sharp_peak": 241.35, "fwd_peak": 308.29,
        "fwd_flat": 476.10, "fwd_valley": 50.93,
        "rev_total": 156.39, "rev_sharp_peak": 25.54, "rev_peak": 25.08,
        "rev_flat": 95.17, "rev_valley": 10.58,
        "description": "华尔特表码 公变客户 0948030028524986",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030037341288",
        "reading_month": "2026-02",
        "fwd_total": 408.23, "fwd_sharp_peak": 135.90, "fwd_peak": 144.04,
        "fwd_flat": 117.98, "fwd_valley": 10.30,
        "rev_total": 66.69, "rev_sharp_peak": 9.16, "rev_peak": 9.57,
        "rev_flat": 46.44, "rev_valley": 1.50,
        "description": "华尔特表码 公变客户(上网) 0948030037341288",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030027271605",
        "reading_month": "2026-02",
        "fwd_total": 266.66, "fwd_sharp_peak": 55.41, "fwd_peak": 74.91,
        "fwd_flat": 121.56, "fwd_valley": 14.78,
        "rev_total": 146.24, "rev_sharp_peak": 36.62, "rev_peak": 33.62,
        "rev_flat": 74.64, "rev_valley": 1.35,
        "description": "华尔特表码 公变客户(上网) 0948030027271605",
    },
    # 光伏发电客户
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030044326050",
        "reading_month": "2026-02",
        "fwd_total": 435.15, "fwd_sharp_peak": 118.77, "fwd_peak": 113.40,
        "fwd_flat": 198.02, "fwd_valley": 4.95,
        "rev_total": 0.20, "rev_sharp_peak": 0.0, "rev_peak": 0.05,
        "rev_flat": 0.06, "rev_valley": 0.08,
        "description": "华尔特表码 光伏发电 0948030044326050",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030044194534",
        "reading_month": "2026-02",
        "fwd_total": 7074.07, "fwd_sharp_peak": 1846.27, "fwd_peak": 1849.74,
        "fwd_flat": 3291.94, "fwd_valley": 86.11,
        "rev_total": 6.16, "rev_sharp_peak": 0.0, "rev_peak": 1.11,
        "rev_flat": 2.0, "rev_valley": 3.04,
        "description": "华尔特表码 光伏发电 0948030044194534",
    },
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030044235097",
        "reading_month": "2026-02",
        "fwd_total": 5468.16, "fwd_sharp_peak": 1430.97, "fwd_peak": 1403.80,
        "fwd_flat": 2571.47, "fwd_valley": 61.91,
        "rev_total": 6.91, "rev_sharp_peak": 0.0, "rev_peak": 2.21,
        "rev_flat": 1.88, "rev_valley": 2.81,
        "description": "华尔特表码 光伏发电 0948030044235097",
    },
    # 地方电厂户
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030041991233",
        "reading_month": "2026-02",
        "fwd_total": 906.69, "fwd_sharp_peak": 230.01, "fwd_peak": 234.41,
        "fwd_flat": 410.87, "fwd_valley": 31.40,
        "rev_total": 0.33, "rev_sharp_peak": 0.0, "rev_peak": 0.04,
        "rev_flat": 0.13, "rev_valley": 0.16,
        "description": "华尔特表码 地方电厂户 0948030041991233",
    },
    # Sheet1 中的记录
    {
        "file": "1月用户表码（全部） (洲千).xls",
        "file_path": "output/archive/unknown/_未分类/1月用户表码（全部） (洲千).xls",
        "meter_number": "0948030030377367",
        "reading_month": "2026-02",
        "fwd_total": 673.75, "fwd_sharp_peak": 78.10, "fwd_peak": 115.78,
        "fwd_flat": 242.06, "fwd_valley": 237.79,
        "rev_total": 76.29, "rev_sharp_peak": 18.72, "rev_peak": 11.07,
        "rev_flat": 46.47, "rev_valley": 0.01,
        "description": "华尔特表码 Sheet1 公变客户 0948030030377367",
    },
]


# ============================================================
# 比较逻辑
# ============================================================

READING_FIELDS = [
    ("fwd_total", "total_kwh"),
    ("fwd_sharp_peak", "sharp_peak"),
    ("fwd_peak", "peak"),
    ("fwd_flat", "flat"),
    ("fwd_valley", "valley"),
    ("rev_total", "rev_total"),
    ("rev_sharp_peak", "rev_sharp_peak"),
    ("rev_peak", "rev_peak"),
    ("rev_flat", "rev_flat"),
    ("rev_valley", "rev_valley"),
]


def compare_values(llm_val, prog_val, field: str) -> tuple[bool, str]:
    """比较 LLM 值和程序值。"""
    if llm_val is None and prog_val is None:
        return True, ""
    if llm_val is None and prog_val is not None:
        return False, f"  {field}: LLM=None, 程序={prog_val}"
    if llm_val is not None and prog_val is None:
        return False, f"  {field}: LLM={llm_val}, 程序=None（缺失）"
    try:
        lv, pv = float(llm_val), float(prog_val)
        if abs(lv - pv) <= TOLERANCE:
            return True, ""
        return False, f"  {field}: LLM={lv}, 程序={pv}, 差={abs(lv - pv):.4f}"
    except (ValueError, TypeError):
        return False, f"  {field}: LLM={llm_val}, 程序={prog_val}（类型不匹配）"


def find_program_record(prog_records: list[dict], fixture: dict) -> dict | None:
    """在程序结果中找到匹配的记录。"""
    month = fixture["reading_month"]
    meter = fixture["meter_number"]
    for r in prog_records:
        if r["meter_number"] == meter and r["reading_month"] == month:
            return r
    return None


def run_tests():
    """运行全部测试。"""
    # 按文件分组加载
    files_to_test = {}
    for f in LLM_FIXTURES:
        fp = f["file_path"]
        if fp not in files_to_test:
            files_to_test[fp] = f["file"]

    # 逐文件提取程序结果
    dispatcher = SmartDispatcher()
    prog_results = {}  # file_path -> list[dict]

    for fpath, fname in files_to_test.items():
        full_path = ROOT / fpath
        if not full_path.exists():
            print(f"[WARN] 文件不存在: {fpath}")
            continue
        sheets = dispatcher.load_as_dataframes(str(full_path))
        if not sheets:
            continue
        extractor = MultiPassExtractor()
        extractor.load_dataframes(sheets)
        records = extractor.extract_all()
        prog_results[fpath] = [
            r for r in records
            if r.get("reading_month") != "unknown"
        ]

    # 逐条对比
    total = 0
    passed = 0
    failed = 0
    failed_details = []

    print(f"\n{'='*80}")
    print("表码数据校验：大模型提取 vs 程序提取")
    print(f"{'='*80}\n")

    for fixture in LLM_FIXTURES:
        total += 1
        fpath = fixture["file_path"]
        desc = fixture.get("description", "")

        if fpath not in prog_results:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  文件 {fpath} 程序未提取到结果\n")
            failed_details.append(f"{desc}: 文件未提取")
            continue

        match = find_program_record(prog_results[fpath], fixture)
        if not match:
            failed += 1
            meter = fixture["meter_number"]
            print(f"[FAIL] {desc}")
            print(f"  未找到匹配记录: {meter} / {fixture['reading_month']}")
            print(f"  程序提取到的记录:")
            for r in prog_results[fpath]:
                print(f"    {r['meter_number']} {r['reading_month']}: "
                      f"正总={r.get('total_kwh')}, 反总={r.get('rev_total')}")
            print()
            failed_details.append(f"{desc}: 未找到匹配记录")
            continue

        # 逐字段对比
        mismatches = []
        for llm_field, prog_field in READING_FIELDS:
            ok, detail = compare_values(fixture.get(llm_field), match.get(prog_field), llm_field)
            if not ok:
                mismatches.append(detail)

        if mismatches:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  电表: {match['meter_number']}, 月份: {match['reading_month']}")
            for m in mismatches:
                print(m)
            print()
            failed_details.append(f"{desc}: {len(mismatches)} 个字段不匹配")
        else:
            passed += 1
            # 打印双方数据以供人工确认
            print(f"[PASS] {desc}")
            print(f"  LLM: 正={fixture['fwd_total']}/{fixture['fwd_sharp_peak']}/{fixture['fwd_peak']}/{fixture['fwd_flat']}/{fixture['fwd_valley']}"
                  f" 反={fixture['rev_total']}/{fixture['rev_sharp_peak']}/{fixture['rev_peak']}/{fixture['rev_flat']}/{fixture['rev_valley']}")
            print(f"  程序: 正={match.get('total_kwh')}/{match.get('sharp_peak')}/{match.get('peak')}/{match.get('flat')}/{match.get('valley')}"
                  f" 反={match.get('rev_total')}/{match.get('rev_sharp_peak')}/{match.get('rev_peak')}/{match.get('rev_flat')}/{match.get('rev_valley')}")

    # 汇总
    print(f"\n{'='*80}")
    print(f"汇总: {total} 条测试, {passed} 通过, {failed} 失败")
    print(f"{'='*80}")

    if failed_details:
        print("\n失败详情:")
        for d in failed_details:
            print(f"  - {d}")

    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
