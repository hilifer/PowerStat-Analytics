"""用大模型独立提取抄表数据，与程序提取结果做对比校验。

测试方法：
1. 大模型（Claude）独立阅读 Excel 原始数据，提取每个电表当月的正向/反向 总尖峰平谷
2. 程序端用 MultiPassExtractor 提取同样的数据
3. 对比两者结果，一致则通过

大模型提取结果作为 fixtures 固化在本文件中（由 Claude 阅读原始 Excel 后生成）。
每个 fixture 记录了：
  - 来源文件、sheet名
  - 电表号
  - 正向：电表用理（总/尖/峰/平/谷）
  - 反向：电表用理（总/尖/峰/平/谷）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import SmartDispatcher
from src.parsers.multi_pass import MultiPassExtractor

# 允许的数值误差
TOLERANCE = 0.5

# ============================================================
# 大模型提取的 fixtures（由 Claude 阅读原始 Excel 后生成）
#
# 数据来源说明：
#   - "电表用理"列 = 本月表数 - 上月表数（未乘倍率的原始差值）
#   - 这是程序应该提取并存入 monthly_readings 的值
# ============================================================

LLM_FIXTURES = [
    # 注：特旺光伏项目为纯转置表且无电表号，程序当前不提取该格式，不纳入对比

    # ---- 首熙 202601 ----
    # Sheet: 首熙20261月
    # 标准表：发电表 0950050038124235，上网表 0950050038441846
    # 正向电表用理(col E): 55.14, 52.12, 88.43, 1.72, 197.41
    # 反向电表用理(col K): 5.11, 4.91, 10.91, 0.05, 20.97
    {
        "file": "首熙202601.xlsx",
        "meter_number": "0950050038124235",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 55.14,
        "fwd_peak": 52.12,
        "fwd_flat": 88.43,
        "fwd_valley": 1.72,
        "fwd_total": 197.41,
        "rev_sharp_peak": 5.11,
        "rev_peak": 4.91,
        "rev_flat": 10.91,
        "rev_valley": 0.05,
        "rev_total": 20.97,
        "description": "首熙 发电表 0950050038124235",
    },

    # ---- 完美印刷 202601 ----
    # Sheet: 完美印刷202601
    # 发电表 0950050038124222
    # 正向电表用理(col E): 46.27, 42.58, 69.48, 1.15, 159.47 (注意: 42.5799999... ≈ 42.58)
    # 反向电表用理(col K): 76.18, 73.56, 126.67, 1.58, 278 (注意: 76.1800000... ≈ 76.18)
    {
        "file": "完美印刷202601.xlsx",
        "meter_number": "0950050038124222",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 46.27,
        "fwd_peak": 42.58,
        "fwd_flat": 69.48,
        "fwd_valley": 1.15,
        "fwd_total": 159.47,
        "rev_sharp_peak": 76.18,
        "rev_peak": 73.56,
        "rev_flat": 126.67,
        "rev_valley": 1.58,
        "rev_total": 278.0,  # 注意：反向总 = 76.18+73.56+126.67+1.58 = 277.99
        "description": "完美印刷 发电表 0950050038124222",
    },

    # ---- 鑫海鑫 202601 ----
    # Sheet: 鑫海盈印刷包装20261月
    # 电表1: 发电表 0950050038124248
    # 正向电表用理: 48.43, 44.47, 75.93, 1.43, 170.26
    # 反向电表用理: 1.68, 1.72, 23.84, 0.02, 27.26
    {
        "file": "鑫海鑫202601.xlsx",
        "meter_number": "0950050038124248",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 48.43,
        "fwd_peak": 44.47,
        "fwd_flat": 75.93,
        "fwd_valley": 1.43,
        "fwd_total": 170.26,
        "rev_sharp_peak": 1.68,
        "rev_peak": 1.72,
        "rev_flat": 23.84,
        "rev_valley": 0.02,
        "rev_total": 27.26,
        "description": "鑫海鑫 电表1 发电表 0950050038124248",
    },
    # 电表2: 发电表 0950050038007239
    # 正向电表用理: 41.8, 38.81, 66.47, 2.21, 148.3 (注意: 66.4699999... ≈ 66.47)
    # 反向电表用理: 8.08, 7.07, 37.1, 0.41, 52.65 (注意: 0.409999... ≈ 0.41)
    {
        "file": "鑫海鑫202601.xlsx",
        "meter_number": "0950050038007239",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 41.8,
        "fwd_peak": 38.81,
        "fwd_flat": 66.47,
        "fwd_valley": 2.21,
        "fwd_total": 148.3,  # 注意: 41.8+38.81+66.47+2.21 = 149.29 ≠ 148.3，这是Excel原始数据
        "rev_sharp_peak": 8.08,
        "rev_peak": 7.07,
        "rev_flat": 37.1,
        "rev_valley": 0.41,
        "rev_total": 52.65,  # 注意: 8.08+7.07+37.1+0.41 = 52.66 ≈ 52.65
        "description": "鑫海鑫 电表2 发电表 0950050038007239",
    },

    # ---- 华尔特 202601 第1个块 ----
    # Sheet: 202601 (转置表，9个块)
    # 块1: 发电表 0948030044326050, 用户号 0948030027271605
    # 正向电表用理: 49.85, 47.01, 78.58, 1.35, 176.78 (注意: 49.85+47.01+78.58+1.35=176.79)
    # 反向电表用理: 15.5, 14.61, 30.18, 0.27, 60.57 (注意: 15.5+14.61+30.18+0.27=60.56)
    {
        "file": "华尔特项目电费统计表202601 - 9个表.xlsx",
        "meter_number": "0948030044326050",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 49.85,
        "fwd_peak": 47.01,
        "fwd_flat": 78.58,
        "fwd_valley": 1.35,
        "fwd_total": 176.78,
        "rev_sharp_peak": 15.5,
        "rev_peak": 14.61,
        "rev_flat": 30.18,
        "rev_valley": 0.27,
        "rev_total": 60.57,
        "description": "华尔特 块1 发电表 0948030044326050",
    },
    # 块2: 发电表 0948030044194534, 用户号 0948030037341288
    # 正向电表用理: 763.49, 770.27, 1308.19, 22.43, 2864.38
    # 反向电表用理: 3.65, 3.76, 16.21, 0.08, 23.7
    {
        "file": "华尔特项目电费统计表202601 - 9个表.xlsx",
        "meter_number": "0948030044194534",
        "reading_month": "2026-01",
        "fwd_sharp_peak": 763.49,
        "fwd_peak": 770.27,
        "fwd_flat": 1308.19,
        "fwd_valley": 22.43,
        "fwd_total": 2864.38,
        "rev_sharp_peak": 3.65,
        "rev_peak": 3.76,
        "rev_flat": 16.21,
        "rev_valley": 0.08,
        "rev_total": 23.7,
        "description": "华尔特 块2 发电表 0948030044194534",
    },

    # ---- 新丰电器 202601 ----
    # Sheet: 202512 (标题是2026年1月)
    # 块1: 发电表 0946110039693280
    # 只有正有功总行：正向电表用理=183.94, 反向电表用理=0.06
    {
        "file": "新丰电器光伏项目202601.xlsx",
        "meter_number": "0946110039693280",
        "reading_month": "2026-01",
        "fwd_sharp_peak": None,
        "fwd_peak": None,
        "fwd_flat": None,
        "fwd_valley": None,
        "fwd_total": 183.94,
        "rev_sharp_peak": None,
        "rev_peak": None,
        "rev_flat": None,
        "rev_valley": None,
        "rev_total": 0.06,
        "description": "新丰电器 块1 发电表 0946110039693280（只有总值）",
    },
    # 块2: 发电表 0946110039760249
    # 只有正有功总行：正向电表用理=191.44, 反向电表用理=4.26
    {
        "file": "新丰电器光伏项目202601.xlsx",
        "meter_number": "0946110039760249",
        "reading_month": "2026-01",
        "fwd_sharp_peak": None,
        "fwd_peak": None,
        "fwd_flat": None,
        "fwd_valley": None,
        "fwd_total": 191.44,
        "rev_sharp_peak": None,
        "rev_peak": None,
        "rev_flat": None,
        "rev_valley": None,
        "rev_total": 4.26,
        "description": "新丰电器 块2 发电表 0946110039760249（只有总值）",
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
        # LLM 认为没有，程序提取到了 → 可能程序对、也可能误提取
        return False, f"  {field}: LLM=None, 程序={prog_val}"
    if llm_val is not None and prog_val is None:
        return False, f"  {field}: LLM={llm_val}, 程序=None（缺失）"
    try:
        lv, pv = float(llm_val), float(prog_val)
        if abs(lv - pv) <= TOLERANCE:
            return True, ""
        return False, f"  {field}: LLM={lv}, 程序={pv}, 差={abs(lv - pv):.2f}"
    except (ValueError, TypeError):
        return False, f"  {field}: LLM={llm_val}, 程序={prog_val}（类型不匹配）"


def find_program_record(prog_records: list[dict], fixture: dict) -> dict | None:
    """在程序结果中找到匹配的记录。"""
    month = fixture["reading_month"]
    meter = fixture.get("meter_number")

    # 精确匹配 meter_number + month
    if meter:
        for r in prog_records:
            if r["meter_number"] == meter and r["reading_month"] == month:
                return r

    # 如果 fixture 没有 meter_number（如特旺转置表），按 month + fwd_total 模糊匹配
    fwd_total = fixture.get("fwd_total")
    if fwd_total is not None:
        for r in prog_records:
            if r["reading_month"] != month:
                continue
            prog_total = r.get("total_kwh")
            if prog_total is not None and abs(float(prog_total) - float(fwd_total)) <= TOLERANCE:
                return r

    return None


def run_tests():
    """运行全部测试。"""
    # 按文件分组加载
    files_to_test = set(f["file"] for f in LLM_FIXTURES)
    archive_root = ROOT / "output" / "archive" / "2026-01"

    # 找到文件路径
    file_paths = {}
    for project_dir in archive_root.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.xlsx"):
            if f.name in files_to_test:
                file_paths[f.name] = str(f)

    missing_files = files_to_test - set(file_paths.keys())
    if missing_files:
        print(f"[WARN] 缺少测试文件: {missing_files}")

    # 逐文件提取程序结果
    dispatcher = SmartDispatcher()
    prog_results = {}  # filename -> list[dict]

    for fname, fpath in file_paths.items():
        sheets = dispatcher.load_as_dataframes(fpath)
        if not sheets:
            continue
        extractor = MultiPassExtractor()
        extractor.load_dataframes(sheets)
        records = extractor.extract_all()
        # 只保留有 reading 数据的
        prog_results[fname] = [
            r for r in records
            if r.get("reading_month") != "unknown" and (
                r.get("total_kwh") is not None or r.get("rev_total") is not None
            )
        ]

    # 逐条对比
    total = 0
    passed = 0
    failed = 0
    failed_details = []

    print(f"\n{'='*70}")
    print("抄表数据校验：大模型提取 vs 程序提取")
    print(f"{'='*70}\n")

    for fixture in LLM_FIXTURES:
        total += 1
        fname = fixture["file"]
        desc = fixture.get("description", "")

        if fname not in prog_results:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  文件 {fname} 程序未提取到结果\n")
            failed_details.append(f"{desc}: 文件未提取")
            continue

        match = find_program_record(prog_results[fname], fixture)
        if not match:
            failed += 1
            meter = fixture.get("meter_number") or "(无电表号)"
            print(f"[FAIL] {desc}")
            print(f"  未找到匹配记录: {meter} / {fixture['reading_month']}")
            # 打印程序提取的全部记录供调试
            print(f"  程序提取到的记录:")
            for r in prog_results[fname]:
                print(f"    {r['meter_number']} {r['reading_month']}: "
                      f"总={r.get('total_kwh')}, 反总={r.get('rev_total')}")
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
            print(f"[PASS] {desc}")

    # 汇总
    print(f"\n{'='*70}")
    print(f"汇总: {total} 条测试, {passed} 通过, {failed} 失败")
    print(f"{'='*70}")

    if failed_details:
        print("\n失败详情:")
        for d in failed_details:
            print(f"  - {d}")

    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
