"""电费结算单提取训练。
两阶段：
  1. 文件名过滤（必须包含"电费结算单"5个连续字）
  2. 提取数据（电厂编号+购电月份+电价），对比Excel
"""

import os, sys, re, json, subprocess, time
from pathlib import Path
from collections import defaultdict

import pytesseract
from PIL import Image, ImageEnhance

SOURCE_DIR = Path("output/temp_attachments")
REFERENCE_XLSX = Path("testfile/电费结单统计表(1).xlsx")
OUTPUT_JSON = Path("output/train_extracted.json")
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}

METER_LABEL_RE = re.compile(r"电厂[\s(（]*交易对象[\s)）]*\s*编号?")
METER_16_RE = re.compile(r"\b(\d{16})\b")
MONTH_CLEAN_RE = re.compile(r"(20[2-5]\d)年(1[0-2]|0?[1-9])月")
MONTH_FLEX_RE = re.compile(r"(20[2-5]\d)\D?(1[0-2]|0[1-9]|[1-9])\D?")
PRICE_RE = re.compile(r"(\d+\.\d{4,})")


def preprocess(img):
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.1)
    w, h = img.size
    if w < 2000:
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
    return img


def ocr_text(img):
    return pytesseract.image_to_string(
        preprocess(img), lang="chi_sim+eng", config="--oem 3 --psm 3"
    )


def render_page(pdf_path, page_num, out_prefix):
    subprocess.run(
        ["pdftoppm", "-r", "200", "-f", str(page_num), "-l", str(page_num),
         "-png", pdf_path, out_prefix],
        capture_output=True, timeout=30,
    )
    for p in [f"{out_prefix}-{page_num:02d}.png", f"{out_prefix}-{page_num}.png"]:
        if Path(p).exists():
            return p
    return None


def render_all_pages(pdf_path, out_prefix):
    subprocess.run(
        ["pdftoppm", "-r", "200", "-png", pdf_path, out_prefix],
        capture_output=True, timeout=60,
    )
    paths = []
    i = 1
    while True:
        for p in [f"{out_prefix}-{i:02d}.png", f"{out_prefix}-{i}.png"]:
            if Path(p).exists():
                paths.append(p)
                break
        else:
            break
        i += 1
    return paths


def extract_meter(text):
    m = METER_LABEL_RE.search(text)
    if not m:
        return None
    after = text[m.end():m.end() + 400]
    for fm in METER_16_RE.finditer(after):
        n = fm.group(1)
        if not n.startswith("095000"):
            return n
    return None


def extract_meter_fallback(text):
    for m in METER_16_RE.finditer(text):
        n = m.group(1)
        if not n.startswith("095000"):
            return n
    return None


def extract_month(text):
    idx = text.find("购电月份")
    search_area = text[idx:idx + 300] if idx >= 0 else text
    search_area = search_area.replace("0]", "01").replace("O月", "0月")

    m = MONTH_CLEAN_RE.search(search_area)
    if m:
        return f"{m.group(0)[:4]}-{m.group(2).zfill(2)}"

    for ym in MONTH_FLEX_RE.finditer(search_area):
        raw = ym.group(2)
        end = ym.end()
        if len(raw) == 1 and end < len(search_area) and search_area[end].isdigit():
            continue
        return f"{ym.group(1)}-{raw.zfill(2)}"

    for ym in re.finditer(r"(20[2-5]\d)", search_area):
        yr_end = ym.end()
        after = search_area[yr_end:yr_end + 6]
        for skip in range(len(after)):
            rest = after[skip:]
            cm = re.match(r"(0[1-9]|1[0-2])", rest)
            if cm:
                return f"{ym.group(1)}-{cm.group(1)}"
        for skip in range(len(after)):
            rest = after[skip:]
            cm = re.match(r"([1-9])", rest)
            if cm:
                after_end = yr_end + skip + 1
                if after_end >= len(search_area) or not search_area[after_end].isdigit():
                    return f"{ym.group(1)}-{cm.group(1).zfill(2)}"

    m = MONTH_CLEAN_RE.search(text)
    if m:
        return f"{m.group(0)[:4]}-{m.group(2).zfill(2)}"
    for ym in MONTH_FLEX_RE.finditer(text):
        raw = ym.group(2)
        end = ym.end()
        if len(raw) == 1 and end < len(text) and text[end].isdigit():
            continue
        return f"{ym.group(1)}-{raw.zfill(2)}"

    return None


def extract_price(text):
    price = 0.0
    idx = text.find("电价")
    if idx >= 0:
        for line in text[idx:].split("\n"):
            for pm in PRICE_RE.finditer(line):
                v = float(pm.group(1))
                if 0.01 <= v <= 3.0:
                    price = v
    if price == 0.0:
        for pm in PRICE_RE.finditer(text):
            v = float(pm.group(1))
            if 0.01 <= v <= 3.0:
                price = v
    return price


def has_title_in_text(text):
    return "电费结算单" in text


def has_required_fields(text):
    has_meter_label = METER_LABEL_RE.search(text) is not None or bool(extract_meter_fallback(text))
    has_month_label = "购电月份" in text or bool(extract_month(text))
    return has_meter_label and has_month_label


# ── 文件处理 ──────────────────────────────────────────────────────
def process_single_image(filepath, rel_path):
    img = Image.open(filepath)
    text = ocr_text(img)
    if not has_required_fields(text):
        return []
    month = extract_month(text)
    meter = extract_meter(text) or extract_meter_fallback(text)
    if not meter or not month:
        return []
    price = extract_price(text)
    return [{"meter_id": meter, "month": month, "price": price, "source": rel_path}]


def process_pdf(filepath, rel_path):
    pages = render_all_pages(filepath, "/tmp/ep2")
    records = []
    file_month = None
    for pi, pp in enumerate(pages):
        img = Image.open(pp)
        os.remove(pp)
        text = ocr_text(img)
        month = extract_month(text)
        if month:
            file_month = month
        month = month or file_month
        if not month:
            continue
        meter = extract_meter(text) or extract_meter_fallback(text)
        if not meter:
            continue
        price = extract_price(text)
        records.append({"meter_id": meter, "month": month, "price": price, "source": rel_path})
    return records


# ── 对照 ──────────────────────────────────────────────────────────
def load_reference(xlsx_path):
    import openpyxl
    from datetime import datetime, timedelta
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active

    def excel_serial_to_month(val):
        if isinstance(val, (int, float)):
            if 44000 <= val <= 47000:
                dt = datetime(1899, 12, 30) + timedelta(days=int(val))
                return f"{dt.year}-{dt.month:02d}"
        return str(val)

    ref = {}
    for row in range(2, ws.max_row + 1):
        meter = ws.cell(row, 2).value
        raw_month = ws.cell(row, 3).value
        price = ws.cell(row, 4).value
        if meter and raw_month:
            month = excel_serial_to_month(raw_month)
            ref[(str(meter).strip(), month)] = float(price) if price else 0.0
    return ref


def compare(records, reference):
    stats = {
        "total_extracted": len(records),
        "total_reference": len(reference),
        "matched": 0,
        "price_diff": [],
        "missing": [],
        "extra": [],
        "price_ok": 0,
    }
    extracted_map = {}
    for r in records:
        key = (r["meter_id"], r["month"])
        extracted_map[key] = r["price"]

    for key, expected in reference.items():
        if key in extracted_map:
            stats["matched"] += 1
            got = extracted_map[key]
            if abs(got - expected) < 0.0001:
                stats["price_ok"] += 1
            else:
                stats["price_diff"].append({"key": key, "expected": expected, "got": got})
        else:
            stats["missing"].append(key)

    for key in extracted_map:
        if key not in reference:
            stats["extra"].append(key)
    return stats


# ── 主流程 ────────────────────────────────────────────────────────
def main():
    print("=" * 60, flush=True)
    print("电费结算单提取训练", flush=True)
    print("=" * 60, flush=True)

    all_files = sorted(
        f for f in SOURCE_DIR.rglob("*")
        if f.is_file() and f.suffix.lower() in ALLOWED_EXT
    )
    print(f"\n扫描目录: {SOURCE_DIR}（邮件附件）")
    print(f"共 {len(all_files)} 个候选文件\n", flush=True)

    # ── 第1阶段：文件名过滤 ──
    print(f"{'=' * 40}")
    print("第1阶段：文件名过滤（含\"结算单\"）")
    print(f"{'=' * 40}\n", flush=True)

    matched_paths = []
    for fp in all_files:
        rel = str(fp.relative_to(SOURCE_DIR))
        fname = fp.name
        if "电费结算单" in fname or ("结算单" in fname and "电量" not in fname):
            print(f"  ✓ {rel}", flush=True)
            matched_paths.append(fp)
        else:
            print(f"  ✗ {rel}", flush=True)

    print(f"\n文件名过滤: {len(matched_paths)}/{len(all_files)} 匹配\n", flush=True)

    # ── 第2阶段：OCR内容验证+提取 ──
    print(f"{'=' * 40}")
    print("第2阶段：OCR验证+提取")
    print(f"{'=' * 40}\n", flush=True)

    all_records = []
    valid_files = 0

    for idx, fp in enumerate(matched_paths):
        abs_path = fp.resolve()
        rel = str(fp.relative_to(SOURCE_DIR))
        ext = fp.suffix.lower()
        print(f"\n[{idx+1}/{len(matched_paths)}] {abs_path}", flush=True)

        start = time.time()
        if ext == ".pdf":
            records = process_pdf(str(fp), rel)
        else:
            records = process_single_image(str(fp), rel)
        elapsed = time.time() - start

        if records:
            valid_files += 1
            meters = set(r["meter_id"] for r in records)
            months = set(r["month"] for r in records)
            for r in records:
                print(f"  {r['meter_id']} | {r['month']} | {r['price']:.6f}", flush=True)
            print(f"  ✓ {len(records)}条, {len(meters)}电表, months={sorted(months)} ({elapsed:.0f}s)", flush=True)
            all_records.extend(records)
        else:
            print(f"  ✗ OCR验证失败 ({elapsed:.0f}s)", flush=True)

    print(f"\n{'=' * 60}")
    print(f"文件名匹配: {len(matched_paths)}")
    print(f"内容验证通过: {valid_files}")
    print(f"提取记录: {len(all_records)}")

    by_meter = defaultdict(set)
    for r in all_records:
        by_meter[r["meter_id"]].add(r["month"])
    print(f"\n按电表汇总:")
    for mid in sorted(by_meter):
        months = sorted(by_meter[mid])
        print(f"  {mid} ({len(months)}个月): {months}")

    output_path = Path(OUTPUT_JSON)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump({
        "scanned": len(all_files),
        "filename_matched": len(matched_paths),
        "valid_files": valid_files,
        "total_records": len(all_records),
        "records": all_records,
    }, open(output_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n已保存: {output_path}")

    if REFERENCE_XLSX.exists():
        print(f"\n对照参考: {REFERENCE_XLSX}")
        reference = load_reference(str(REFERENCE_XLSX))
        stats = compare(all_records, reference)

        print(f"  参考总条数: {stats['total_reference']}")
        print(f"  提取总条数: {stats['total_extracted']}")
        print(f"  匹配条数:   {stats['matched']}")
        print(f"  价格一致:   {stats['price_ok']}")
        print(f"  价格差异:   {len(stats['price_diff'])}")
        for d in stats["price_diff"]:
            print(f"    {d['key']}: expected={d['expected']:.6f}, got={d['got']:.6f}")
        print(f"  缺失条目:   {len(stats['missing'])}")
        for k in stats["missing"]:
            print(f"    {k}")
        print(f"  多余条目:   {len(stats['extra'])}")

        match_rate = stats["matched"] / stats["total_reference"] * 100 if stats["total_reference"] else 0
        price_rate = stats["price_ok"] / stats["total_reference"] * 100 if stats["total_reference"] else 0
        print(f"\n  关键匹配率: {match_rate:.1f}% ({stats['matched']}/{stats['total_reference']})")
        print(f"  价格准确率: {price_rate:.1f}% ({stats['price_ok']}/{stats['total_reference']})")

        if stats["matched"] == stats["total_reference"] and stats["price_ok"] == stats["total_reference"]:
            print(f"\n  ✓ 100% 完美！", flush=True)
        else:
            print(f"\n  ✗ 还有差异需要修复", flush=True)
    else:
        print(f"\n参考文件不存在: {REFERENCE_XLSX}，跳过对照")

    print(f"\n{'=' * 60}")
    print("完成", flush=True)


if __name__ == "__main__":
    main()
