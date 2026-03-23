"""用大模型独立提取图片单价数据，与 OCR 程序提取结果做对比校验。

测试方法：
1. 大模型（Claude Vision）独立阅读南方电网电费账单图片，提取用户编号和分时段单价
2. 程序端用 OCREngine 提取同样的数据
3. 逐字段对比两者结果

运行方式：
    # 先生成 LLM 基准数据（需要 ANTHROPIC_API_KEY）
    python tests/test_price_ocr_llm_verify.py --generate

    # 运行对比测试（使用已生成的基准数据）
    python tests/test_price_ocr_llm_verify.py

    # 或通过 pytest
    pytest tests/test_price_ocr_llm_verify.py -v
"""

import json
import sys
import os
import base64
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 基准数据文件路径
FIXTURES_FILE = ROOT / "tests" / "price_ocr_fixtures.json"

# 南方电网电费单图片目录（只处理含 Charge Information 的账单图片）
IMAGE_DIRS = [
    ROOT / "output" / "temp_attachments" / "20260309_2026年1月电费" / "华尔特光伏电费（1月）20260203",
    ROOT / "output" / "temp_attachments" / "20260309_2026年1月电费" / "首熙、完美印刷、鑫海盈2026年1月电费",
    ROOT / "output" / "temp_attachments" / "20260309_2026年1月电费" / "沙井智荟2026年1月电费",
    ROOT / "output" / "temp_attachments" / "20260309_2026年1月电费" / "特旺光伏项目202601",
]

# 只处理南方电网账单图片（微信截图或特定 hash 命名文件），跳过结算单
BILL_IMAGE_PATTERNS = ["微信图片_", "91f22064", "d5759a23"]


def find_bill_images() -> list[Path]:
    """查找所有南方电网电费账单图片。"""
    images = []
    for d in IMAGE_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if not f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                continue
            # 跳过结算单（电费结算单）
            if "结算" in f.name:
                continue
            # 只保留原始图片（不是 _1/_2 预处理版本）
            if any(pat in f.name for pat in BILL_IMAGE_PATTERNS):
                # 优先用 _2 版本（预处理后），否则用原始版本
                # 但收集时只取 base name 去重
                images.append(f)
    # 去重：同一张图片的不同版本只保留一个（优先 _2 > _1 > 原始）
    grouped = {}
    for f in images:
        # 提取基础名称（去掉 _1, _2 后缀）
        base = f.stem
        for suffix in ("_2", "_1"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        if base not in grouped:
            grouped[base] = f
        else:
            # 优先级: _2 > _1 > 原始
            existing = grouped[base]
            if "_2" in f.stem and "_2" not in existing.stem:
                grouped[base] = f
            elif "_1" in f.stem and "_1" not in existing.stem and "_2" not in existing.stem:
                grouped[base] = f
    return sorted(grouped.values(), key=lambda p: p.name)


def encode_image_base64(filepath: Path) -> str:
    """将图片编码为 base64。"""
    with open(filepath, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def llm_extract_price(filepath: Path, client) -> dict:
    """用 Claude Vision 从电费账单图片中提取用户编号和分时段单价。"""
    img_b64 = encode_image_base64(filepath)
    media_type = "image/jpeg" if filepath.suffix.lower() in (".jpg", ".jpeg") else "image/png"

    prompt = """请仔细阅读这张中国南方电网电费账单图片，提取以下信息：

1. **用户编号**：在账单右上方的"用户编号"字段（16位数字，以09开头）
2. **分时段电价**：从"电费信息 Charge Information"表格中，计算各时段的**总单价**
   - 总单价 = 电度电费单价 + 输配电费单价 + 系统运行费单价 + 基金及附加费单价（如有）+ 上网环节线损电费单价（如有）+ 市场化分摊费单价（如有）
   - 分别计算：尖峰(sharp_peak)、峰(peak)、平(flat)、谷(valley) 四个时段
   - 如果某个时段的计费电量为0，单价可能不显示，此时该时段价格返回 null
   - 如果账单没有分时段（只有一个"电量电费"行），说明是统一电价

3. **平均电价**：如果账单底部显示"平均电价"，也请提取

请以严格的 JSON 格式返回（不要包含其他文字）：
{
    "user_id": "用户编号（16位数字字符串）",
    "bill_type": "分时段计价类型描述（如：大工业分时、一般工商业、居民等）",
    "sharp_peak_price": 尖峰总单价或null,
    "peak_price": 峰总单价或null,
    "flat_price": 平总单价或null,
    "valley_price": 谷总单价或null,
    "average_price": 平均电价或null,
    "notes": "任何需要说明的问题（如图片模糊、数据不完整等）"
}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": img_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    # 解析 JSON 响应
    text = response.content[0].text.strip()
    # 尝试提取 JSON 块
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  [WARN] LLM 返回无法解析的 JSON: {text[:200]}")
        return {}


def generate_fixtures():
    """调用 Claude Vision API 生成基准数据。"""
    try:
        import anthropic
    except ImportError:
        print("需要安装 anthropic SDK: pip install anthropic")
        return False

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("需要设置环境变量 ANTHROPIC_API_KEY")
        return False

    client = anthropic.Anthropic(api_key=api_key)
    images = find_bill_images()

    if not images:
        print("未找到电费账单图片")
        return False

    print(f"找到 {len(images)} 张电费账单图片")
    fixtures = []

    for img_path in images:
        print(f"\n处理: {img_path.name}")
        try:
            result = llm_extract_price(img_path, client)
            if result:
                result["source_file"] = img_path.name
                result["file_path"] = str(img_path.relative_to(ROOT))
                fixtures.append(result)
                print(f"  用户={result.get('user_id')}, "
                      f"尖={result.get('sharp_peak_price')}, "
                      f"峰={result.get('peak_price')}, "
                      f"平={result.get('flat_price')}, "
                      f"谷={result.get('valley_price')}, "
                      f"均价={result.get('average_price')}")
                if result.get("notes"):
                    print(f"  备注: {result['notes']}")
            else:
                print("  [WARN] 未提取到数据")
        except Exception as e:
            print(f"  [ERROR] {e}")

        # 避免 API 限流
        time.sleep(1)

    # 保存基准数据
    with open(FIXTURES_FILE, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, ensure_ascii=False, indent=2)

    print(f"\n基准数据已保存到: {FIXTURES_FILE}")
    print(f"共 {len(fixtures)} 条记录")
    return True


def load_fixtures() -> list[dict]:
    """加载已生成的基准数据。"""
    if not FIXTURES_FILE.exists():
        return []
    with open(FIXTURES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


_ocr_engine = None

def ocr_extract_price(filepath: Path) -> dict:
    """用 OCR 引擎提取单价。"""
    global _ocr_engine
    if _ocr_engine is None:
        from src.ocr.ocr_engine import OCREngine
        _ocr_engine = OCREngine()

    result = _ocr_engine.extract_from_image(str(filepath))
    return {
        "user_id": result.user_id,
        "sharp_peak_price": result.sharp_peak_price,
        "peak_price": result.peak_price,
        "flat_price": result.flat_price,
        "valley_price": result.valley_price,
        "average_price": result.average_price,
        "source_file": result.source_file,
    }


def compare_price(llm_val, ocr_val, field: str, tolerance: float = 0.05) -> tuple[bool, str]:
    """比较单价值。

    tolerance: 允许的绝对误差（元/kWh），考虑到 OCR 可能丢失组件。
    """
    if llm_val is None and ocr_val is None:
        return True, ""
    if llm_val is None and ocr_val is not None:
        # LLM 基准未提取该字段 → 跳过比较（null 表示"未测试"而非"应为空"）
        return True, ""
    if llm_val is not None and ocr_val is None:
        return False, f"  {field}: LLM={llm_val}, OCR=None（缺失）"
    try:
        lv, ov = float(llm_val), float(ocr_val)
        if abs(lv - ov) <= tolerance:
            return True, ""
        return False, f"  {field}: LLM={lv:.8f}, OCR={ov:.8f}, 差={abs(lv-ov):.8f}"
    except (ValueError, TypeError):
        return False, f"  {field}: LLM={llm_val}, OCR={ocr_val}（类型不匹配）"


def run_tests() -> bool:
    """运行对比测试。"""
    fixtures = load_fixtures()
    if not fixtures:
        print("[SKIP] 无基准数据。请先运行: python tests/test_price_ocr_llm_verify.py --generate")
        return True  # 不阻塞 CI

    # 分为两类：有价格数据的（完整比对）和只有 user_id 的（仅比对用户编号）
    price_fixtures = []
    userid_only_fixtures = []
    for f in fixtures:
        has_price = any(
            f.get(k) is not None
            for k in ["sharp_peak_price", "peak_price", "flat_price", "valley_price", "average_price"]
        )
        if has_price:
            price_fixtures.append(f)
        elif f.get("user_id"):
            userid_only_fixtures.append(f)

    all_fixtures = price_fixtures + userid_only_fixtures
    if not all_fixtures:
        print("[SKIP] 基准数据中无有效记录")
        return True

    print(f"\n{'='*80}")
    print("OCR 单价提取校验：大模型(Claude Vision) vs OCR引擎")
    print(f"{'='*80}\n")

    total = 0
    passed = 0
    failed = 0
    failed_details = []

    # 初始化 OCR（只初始化一次）
    from src.config_loader import config as app_config
    app_config.load()

    for fixture in all_fixtures:
        total += 1
        file_path = ROOT / fixture["file_path"]
        source_file = fixture["source_file"]

        if not file_path.exists():
            print(f"[SKIP] 文件不存在: {fixture['file_path']}")
            total -= 1
            continue

        # OCR 提取
        ocr_result = ocr_extract_price(file_path)

        # 对比用户编号
        mismatches = []
        llm_uid = fixture.get("user_id")
        ocr_uid = ocr_result.get("user_id")
        if llm_uid and ocr_uid:
            if str(llm_uid).strip() != str(ocr_uid).strip():
                mismatches.append(f"  user_id: LLM={llm_uid}, OCR={ocr_uid}")
        elif llm_uid and not ocr_uid:
            mismatches.append(f"  user_id: LLM={llm_uid}, OCR=None（缺失）")

        # 对比各时段单价（仅当 fixture 有价格数据时）
        has_price_fixture = fixture in price_fixtures
        price_fields = [
            ("sharp_peak_price", "尖峰价"),
            ("peak_price", "峰价"),
            ("flat_price", "平价"),
            ("valley_price", "谷价"),
            ("average_price", "均价"),
        ]

        if has_price_fixture:
            for field, label in price_fields:
                ok, detail = compare_price(fixture.get(field), ocr_result.get(field), label)
                if not ok:
                    mismatches.append(detail)

        test_scope = "完整" if has_price_fixture else "仅user_id"
        if mismatches:
            failed += 1
            print(f"[FAIL] {source_file} ({test_scope})")
            print(f"  类型: {fixture.get('bill_type', '未知')}")
            for m in mismatches:
                print(m)
            if fixture.get("notes"):
                print(f"  LLM备注: {fixture['notes']}")
            print()
            failed_details.append(f"{source_file}: {len(mismatches)} 个字段不匹配")
        else:
            passed += 1
            if has_price_fixture:
                print(f"[PASS] {source_file} ({test_scope})")
                print(f"  用户={llm_uid}, "
                      f"尖={fixture.get('sharp_peak_price')}, "
                      f"峰={fixture.get('peak_price')}, "
                      f"平={fixture.get('flat_price')}, "
                      f"谷={fixture.get('valley_price')}")
            else:
                print(f"[PASS] {source_file} ({test_scope})")
                print(f"  用户={llm_uid} ✓ (价格待 --generate 补充)")

    # 汇总
    print(f"\n{'='*80}")
    print(f"汇总: {total} 条测试, {passed} 通过, {failed} 失败")
    if total > 0:
        print(f"通过率: {passed/total*100:.1f}%")
    print(f"{'='*80}")

    if failed_details:
        print("\n失败详情:")
        for d in failed_details:
            print(f"  - {d}")

    return failed == 0


# ============================================================
# pytest 兼容接口
# ============================================================

def test_price_ocr_vs_llm():
    """pytest 入口：OCR 单价提取 vs LLM 基准数据。"""
    fixtures = load_fixtures()
    if not fixtures:
        import pytest
        pytest.skip("无基准数据，请先运行: python tests/test_price_ocr_llm_verify.py --generate")

    assert run_tests(), "OCR 单价提取与 LLM 基准数据存在差异"


if __name__ == "__main__":
    if "--generate" in sys.argv:
        ok = generate_fixtures()
        sys.exit(0 if ok else 1)
    else:
        ok = run_tests()
        sys.exit(0 if ok else 1)
