"""用大模型独立提取电表档案数据，与程序提取结果做对比校验。

测试方法：
1. 大模型（Claude）独立阅读含完整电表档案格式的 Excel 文件
   （必须同时包含：用户号、电表号、资产号、电表类型、倍率）
2. 程序端用 MultiPassExtractor.extract_meters_only() 提取同样的数据（不提取月度读数）
3. 逐字段对比两者结果，全部一致则通过

注意：仅从同时能提取上述 5 个字段的文件中提取，不提取月度数据。

大模型提取结果作为 fixtures 固化在本文件中（由 Claude 阅读原始 Excel 后生成）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import SmartDispatcher
from src.parsers.multi_pass import MultiPassExtractor

# ============================================================
# 大模型提取的 fixtures（由 Claude 阅读原始 Excel 后生成）
#
# 数据来源：同时包含 用户号、电表号、资产号、电表类型、倍率 的文件
# 不含月度读数，仅电表档案信息
# ============================================================

LLM_FIXTURES = [
    # ================================================================
    # 文件1: 华尔特项目电费统计表202601.xlsx [202601]
    # 格式: 每个用户一个块（约10行），包含正向/反向数据
    # 右侧列标签: 用户号、发电表号、发电表资产编号、上网表号、上网表资产号
    # 列: 类别 | 上月表数 | 本月表数 | 电表用理 | 倍率 | 发电量 | ... | 倍率 | 上网电量
    # 4个用户块，共8个电表（4发电 + 4上网）
    # ================================================================

    # --- Block 1: 用户号 0948030028524999 ---
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041968361",
        "asset_number": "09001SF00000042408595673",
        "user_id": "0948030028524999",
        "meter_type": "发电表",
        "multiplier": 40.0,
        "paired_meter": "0948030042077031",
        "description": "华尔特 Block1 发电表",
    },
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030042077031",
        "asset_number": "09001SF00000042207356623",
        "user_id": "0948030028524999",
        "meter_type": "上网表",
        "multiplier": 60.0,
        "paired_meter": "0948030041968361",
        "description": "华尔特 Block1 上网表",
    },

    # --- Block 2: 用户号 0948030028524986 ---
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041968260",
        "asset_number": "09001SF00000042408595672",
        "user_id": "0948030028524986",
        "meter_type": "发电表",
        "multiplier": 40.0,
        "paired_meter": "0948030041991233",
        "description": "华尔特 Block2 发电表",
    },
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041991233",
        "asset_number": "09001SF00000042207356622",
        "user_id": "0948030028524986",
        "meter_type": "上网表",
        "multiplier": 60.0,
        "paired_meter": "0948030041968260",
        "description": "华尔特 Block2 上网表",
    },

    # --- Block 3: 用户号 0948030027271533 ---
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041968273",
        "asset_number": "09001SF00000042408595704",
        "user_id": "0948030027271533",
        "meter_type": "发电表",
        "multiplier": 60.0,
        "paired_meter": "0948030042119702",
        "description": "华尔特 Block3 发电表",
    },
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030042119702",
        "asset_number": "09001SF00000042207356625",
        "user_id": "0948030027271533",
        "meter_type": "上网表",
        "multiplier": 60.0,
        "paired_meter": "0948030041968273",
        "description": "华尔特 Block3 上网表",
    },

    # --- Block 4: 用户号 0948030027271344 ---
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041302226",
        "asset_number": "09001SF00000042408595705",
        "user_id": "0948030027271344",
        "meter_type": "发电表",
        "multiplier": 60.0,
        "paired_meter": "0948030041991350",
        "description": "华尔特 Block4 发电表",
    },
    {
        "file": "华尔特项目电费统计表202601.xlsx",
        "file_path": "output/archive/2026-01/华尔特项目/华尔特项目电费统计表202601.xlsx",
        "meter_number": "0948030041991350",
        "asset_number": "09001SF00000042207356624",
        "user_id": "0948030027271344",
        "meter_type": "上网表",
        "multiplier": 60.0,
        "paired_meter": "0948030041302226",
        "description": "华尔特 Block4 上网表",
    },

    # ================================================================
    # 文件2: 完美印刷202601.xlsx [完美印刷202601]
    # 格式: 电费单格式，正向/反向数据，标签行含用电户号
    # 行2: 电表号 | 类别 | 上月表数 | 本月表数 | 电表用理 | 倍率 | 用电量 | 上网表号 | ...
    # 行3-7: 数据行，右侧标签：上网电表、上网电表资产表、发电表号、发电资产号
    # ================================================================
    {
        "file": "完美印刷202601.xlsx",
        "file_path": "output/archive/2026-01/完美印刷/完美印刷202601.xlsx",
        "meter_number": "0950050038124222",
        "asset_number": "09001SF00000042207348996",
        "user_id": "0950000047355990",
        "meter_type": "发电表",
        "multiplier": 40.0,
        "paired_meter": "0950050038441905",
        "description": "完美印刷 发电表",
    },
    {
        "file": "完美印刷202601.xlsx",
        "file_path": "output/archive/2026-01/完美印刷/完美印刷202601.xlsx",
        "meter_number": "0950050038441905",
        "asset_number": "09001SF00000042107007791",
        "user_id": "0950000047355990",
        "meter_type": "上网表",
        "multiplier": 20.0,
        "paired_meter": "0950050038124222",
        "description": "完美印刷 上网表",
    },

    # ================================================================
    # 文件3: 首熙202601.xlsx [首熙20261月]
    # 格式: 电费单格式，与完美印刷相似
    # 发电表倍率=100，上网表倍率=600
    # ================================================================
    {
        "file": "首熙202601.xlsx",
        "file_path": "output/archive/2026-01/首熙/首熙202601.xlsx",
        "meter_number": "0950050038124235",
        "asset_number": "09001SF00000042207348994",
        "user_id": "0950000088133431",
        "meter_type": "发电表",
        "multiplier": 100.0,
        "paired_meter": "0950050038441846",
        "description": "首熙 发电表",
    },
    {
        "file": "首熙202601.xlsx",
        "file_path": "output/archive/2026-01/首熙/首熙202601.xlsx",
        "meter_number": "0950050038441846",
        "asset_number": "09001SG00000062105025931",
        "user_id": "0950000088133431",
        "meter_type": "上网表",
        "multiplier": 600.0,
        "paired_meter": "0950050038124235",
        "description": "首熙 上网表",
    },

    # ================================================================
    # 文件4: 鑫海鑫202601.xlsx [鑫海盈印刷包装20261月]
    # 格式: 电费单格式，2个用户块，共4个电表
    # Block1: 用电户号'0950000088022175, 发电倍率=100, 上网倍率=80
    # Block2: 用电户号0950000048227895, 发电倍率=40, 上网倍率=40
    # ================================================================

    # --- Block 1 ---
    # 注意：文件中用户号有两处不同写法：
    #   表头 "用电户号'0950000088022175" vs 数据行 "用户号：'0950000880022175"
    #   每月汇总也用 "0950000880022175"，以数据行为准
    {
        "file": "鑫海鑫202601.xlsx",
        "file_path": "output/archive/2026-01/鑫海鑫/鑫海鑫202601.xlsx",
        "meter_number": "0950050038124248",
        "asset_number": "09001SF00000042207348995",
        "user_id": "0950000880022175",
        "meter_type": "发电表",
        "multiplier": 100.0,
        "paired_meter": "0950050038400663",
        "description": "鑫海鑫 Block1 发电表",
    },
    {
        "file": "鑫海鑫202601.xlsx",
        "file_path": "output/archive/2026-01/鑫海鑫/鑫海鑫202601.xlsx",
        "meter_number": "0950050038400663",
        "asset_number": "09001SF00000041804265458",
        "user_id": "0950000880022175",
        "meter_type": "上网表",
        "multiplier": 80.0,
        "paired_meter": "0950050038124248",
        "description": "鑫海鑫 Block1 上网表",
    },

    # --- Block 2 ---
    {
        "file": "鑫海鑫202601.xlsx",
        "file_path": "output/archive/2026-01/鑫海鑫/鑫海鑫202601.xlsx",
        "meter_number": "0950050038007239",
        "asset_number": "09001SF00000042207348997",
        "user_id": "0950000048227895",
        "meter_type": "发电表",
        "multiplier": 40.0,
        "paired_meter": "0950050038475391",
        "description": "鑫海鑫 Block2 发电表",
    },
    {
        "file": "鑫海鑫202601.xlsx",
        "file_path": "output/archive/2026-01/鑫海鑫/鑫海鑫202601.xlsx",
        "meter_number": "0950050038475391",
        "asset_number": "09001SF00000042107007793",
        "user_id": "0950000048227895",
        "meter_type": "上网表",
        "multiplier": 40.0,
        "paired_meter": "0950050038007239",
        "description": "鑫海鑫 Block2 上网表",
    },

    # ================================================================
    # 文件5: 沙井智荟先进制造产业园 - 副本.xlsx [202601]
    # 格式: 光伏项目发电统计表，与华尔特类似
    # 1个用户块，发电倍率=400，上网倍率=1500
    # ================================================================
    {
        "file": "沙井智荟先进制造产业园 - 副本.xlsx",
        "file_path": "output/archive/unknown/_未分类/沙井智荟先进制造产业园 - 副本.xlsx",
        "meter_number": "0946070038961957",
        "asset_number": "09001SF00000042207349302",
        "user_id": "0946000082501856",
        "meter_type": "发电表",
        "multiplier": 400.0,
        "paired_meter": "0946070038924024",
        "description": "沙井智荟 发电表",
    },
    {
        "file": "沙井智荟先进制造产业园 - 副本.xlsx",
        "file_path": "output/archive/unknown/_未分类/沙井智荟先进制造产业园 - 副本.xlsx",
        "meter_number": "0946070038924024",
        "asset_number": "09001SG00000061804340887",
        "user_id": "0946000082501856",
        "meter_type": "上网表",
        "multiplier": 1500.0,
        "paired_meter": "0946070038961957",
        "description": "沙井智荟 上网表",
    },
]


# ============================================================
# 要对比的电表档案字段
# (LLM fixture 键, 程序输出键)
# ============================================================

METER_FIELDS = [
    ("meter_number", "meter_number"),
    ("asset_number", "asset_number"),
    ("user_id", "user_id"),
    ("meter_type", "meter_type"),
    ("multiplier", "multiplier"),
]


def compare_values(llm_val, prog_val, field: str) -> tuple[bool, str]:
    """比较 LLM 值和程序值。"""
    if llm_val is None and prog_val is None:
        return True, ""
    if llm_val is None and prog_val is not None:
        return False, f"  {field}: LLM=None, 程序={prog_val}"
    if llm_val is not None and prog_val is None:
        return False, f"  {field}: LLM={llm_val}, 程序=None（缺失）"
    # 字符串类字段（编号/ID/类型）：直接比较字符串，不做 float 转换（避免丢失前导零）
    if field in ("meter_number", "asset_number", "user_id", "meter_type", "paired_meter"):
        if str(llm_val).strip() == str(prog_val).strip():
            return True, ""
        return False, f"  {field}: LLM={llm_val}, 程序={prog_val}"
    # 数值比较
    try:
        lv, pv = float(llm_val), float(prog_val)
        if abs(lv - pv) < 0.01:
            return True, ""
        return False, f"  {field}: LLM={lv}, 程序={pv}"
    except (ValueError, TypeError):
        pass
    # 兜底字符串比较
    if str(llm_val).strip() == str(prog_val).strip():
        return True, ""
    return False, f"  {field}: LLM={llm_val}, 程序={prog_val}"


def find_program_meter(prog_records: list[dict], meter_number: str) -> dict | None:
    """在程序结果中找到匹配的电表记录。"""
    for r in prog_records:
        if r["meter_number"] == meter_number:
            return r
    return None


def run_tests():
    """运行全部测试。"""
    # 按文件分组
    files_to_test = {}
    for f in LLM_FIXTURES:
        fp = f["file_path"]
        if fp not in files_to_test:
            files_to_test[fp] = f["file"]

    # 逐文件用 extract_meters_only() 提取（不提取月度数据）
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
        records = extractor.extract_meters_only()
        prog_results[fpath] = records

    # 逐条对比
    total = 0
    passed = 0
    failed = 0
    failed_details = []

    print(f"\n{'='*80}")
    print("电表档案校验：大模型提取 vs 程序提取（仅电表元数据，不含月度数据）")
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

        match = find_program_meter(prog_results[fpath], fixture["meter_number"])
        if not match:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  未找到电表: {fixture['meter_number']}")
            print(f"  程序提取到的电表:")
            for r in prog_results[fpath]:
                print(f"    {r['meter_number']} 类型={r.get('meter_type')} "
                      f"资产={r.get('asset_number')} 用户={r.get('user_id')} "
                      f"倍率={r.get('multiplier')}")
            print()
            failed_details.append(f"{desc}: 未找到电表 {fixture['meter_number']}")
            continue

        # 逐字段对比
        mismatches = []
        for llm_field, prog_field in METER_FIELDS:
            ok, detail = compare_values(fixture.get(llm_field), match.get(prog_field), llm_field)
            if not ok:
                mismatches.append(detail)

        # 配对电表对比（非必须字段，但有则比）
        if fixture.get("paired_meter"):
            ok, detail = compare_values(fixture["paired_meter"], match.get("paired_meter"), "paired_meter")
            if not ok:
                mismatches.append(detail)

        if mismatches:
            failed += 1
            print(f"[FAIL] {desc}")
            print(f"  电表: {match['meter_number']}")
            for m in mismatches:
                print(m)
            print()
            failed_details.append(f"{desc}: {len(mismatches)} 个字段不匹配")
        else:
            passed += 1
            print(f"[PASS] {desc}")
            print(f"  LLM:  用户={fixture.get('user_id')} 资产={fixture.get('asset_number')} "
                  f"类型={fixture.get('meter_type')} 倍率={fixture.get('multiplier')}")
            print(f"  程序: 用户={match.get('user_id')} 资产={match.get('asset_number')} "
                  f"类型={match.get('meter_type')} 倍率={match.get('multiplier')}")

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
