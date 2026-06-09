# -*- coding: utf-8 -*-
"""
电费通知单 尖峰平谷提取与比对工具
=================================

封装三个核心方法，并以通用工具类 :class:`BillPriceTool` 对外提供：

方法 1  ``collect_images``
    从文件路径或目录递归收集所有图片/PDF 文件，按内容去重。

方法 2  ``extract_prices``
    对每张图调用 OCREngine 提取尖峰平谷单价，
    只保留 OCR 文本包含「中国南方电网」的结果（真实电费通知单）。

方法 3  ``match_with_excel``
    把提取结果与 电费通知单统计.xlsx 做精确逐项比对：

        - 用户编号 + 购电月份 + 四项单价（尖/峰/平/谷）全部精确相等才算命中
        - 支持逐项打印差异明细

OCR 引擎
    复用 src/ocr/ocr_engine.OCREngine，与页面全量提取同一条路径，
    确保 CLI 与网页端结果一致。

依赖::

    pip install rapidocr-onnxruntime onnxruntime pymupdf pillow numpy openpyxl

命令行用法::

    python bill_price_tool.py <文件/目录>... --xlsx 电费通知单统计.xlsx [选项]
"""

from __future__ import annotations

import os
import re
import sys
import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

import numpy as np


# ----------------------------------------------------------------------------- #
# 数据结构
# ----------------------------------------------------------------------------- #

@dataclass
class PriceRecord:
    """从电费通知单图片提取出来的一条单价数据。"""

    user_id: Optional[str]              # 用户编号
    reading_month: Optional[str]        # 统计月（DB 维度，如 2026-02）
    purchase_month: Optional[str]       # 购电月（XLSX 维度，统计月-1，如 2026-01）
    sharp_peak_price: Optional[float]   # 尖
    peak_price: Optional[float]         # 峰
    flat_price: Optional[float]         # 平
    valley_price: Optional[float]       # 谷
    average_price: Optional[float]      # 均价
    bill_type: int = 0                  # 账单类型（1-5）
    source_file: str = ""               # 来源文件
    confidence: float = 0.0             # 置信度

    def compare_key(self) -> Tuple[Optional[str], Optional[str], Optional[float],
                                   Optional[float], Optional[float], Optional[float]]:
        return (self.user_id, self.purchase_month,
                self.sharp_peak_price, self.peak_price,
                self.flat_price, self.valley_price)

    def has_price(self) -> bool:
        return self.sharp_peak_price is not None or self.peak_price is not None


@dataclass
class ExcelRow:
    """Excel 中一行对比基准。"""

    user_id: str
    purchase_month: str          # 如 2026-01
    sharp_peak_price: Optional[float]
    peak_price: Optional[float]
    flat_price: Optional[float]
    valley_price: Optional[float]
    note: str = ""

    def compare_key(self):
        return (self.user_id.strip(), self.purchase_month,
                self.sharp_peak_price, self.peak_price,
                self.flat_price, self.valley_price)


@dataclass
class RowDetail:
    """单条 Excel 行与提取数据的逐项对比明细。"""

    excel: ExcelRow
    record: Optional[PriceRecord]
    id_ok: bool
    month_ok: bool
    sp_ok: bool
    p_ok: bool
    f_ok: bool
    v_ok: bool
    source: str = ""

    @property
    def matched(self) -> bool:
        return self.id_ok and self.month_ok and self.sp_ok and self.p_ok and self.f_ok and self.v_ok

    def line(self) -> str:
        def m(v):
            return "Y" if v else "N"
        rec = self.record
        uid = rec.user_id if rec else "—"
        mon = rec.purchase_month if rec else "—"
        sp = f"{rec.sharp_peak_price}" if rec and rec.sharp_peak_price is not None else "—"
        p = f"{rec.peak_price}" if rec and rec.peak_price is not None else "—"
        fl = f"{rec.flat_price}" if rec and rec.flat_price is not None else "—"
        v = f"{rec.valley_price}" if rec and rec.valley_price is not None else "—"
        e = self.excel
        return (
            f"[{'OK' if self.matched else '!!'}] "
            f"用户 {e.user_id}({m(self.id_ok)}={uid}) "
            f"月 {e.purchase_month}({m(self.month_ok)}={mon}) "
            f"尖 {e.sharp_peak_price}({m(self.sp_ok)}={sp}) "
            f"峰 {e.peak_price}({m(self.p_ok)}={p}) "
            f"平 {e.flat_price}({m(self.f_ok)}={fl}) "
            f"谷 {e.valley_price}({m(self.v_ok)}={v}) "
            f"[{e.note}] {self.source}"
        )


@dataclass
class MatchResult:
    """比对结果汇总。"""

    total_excel: int
    matched: int
    unmatched_rows: List[ExcelRow] = field(default_factory=list)
    extracted_records: List[PriceRecord] = field(default_factory=list)
    details: List[RowDetail] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.total_excel > 0 and self.matched == self.total_excel

    def summary(self) -> str:
        return (
            f"\n{'=' * 60}\n"
            f"  比对结果：{self.matched}/{self.total_excel}  "
            f"-> {'全部命中' if self.success else '有遗漏'}\n"
            f"{'=' * 60}"
        )

    def detail_report(self, only_fail: bool = False) -> str:
        rows = [d for d in self.details if (not only_fail or not d.matched)]
        lines = []
        if rows:
            lines.append(f"明细（{'仅失败' if only_fail else '全部'}，共 {len(self.details)} 条）：")
            for i, d in enumerate(rows, 1):
                lines.append(f"  {i:>3}. {d.line()}")
        lines.append(self.summary())
        return "\n".join(lines)


# ----------------------------------------------------------------------------- #
# 工具类
# ----------------------------------------------------------------------------- #

class BillPriceTool:
    """电费通知单 尖峰平谷提取 / 比对 工具类。"""

    SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
    FILTER_KEYWORD = "中国南方电网"
    # 文件名预过滤：跳过明显不是电费通知单的文件
    SKIP_FILENAME_PATTERNS = ["结算单", "核算单", "电量", "补贴"]

    def __init__(self, ocr_engine=None):
        self._ocr = ocr_engine

    @property
    def ocr(self):
        if self._ocr is None:
            from src.ocr.ocr_engine import OCREngine
            self._ocr = OCREngine()
        return self._ocr

    # ================================================================== #
    # 方法 1：收集文件 + 去重
    # ================================================================== #

    @classmethod
    def collect_images(cls, paths: List[str], verbose: bool = False) -> List[str]:
        """从路径列表收集可处理的文件，按内容去重。

        :param paths: 文件路径或目录路径
        :param verbose: 打印收集过程
        :return: 去重后的文件路径列表
        """
        files: List[str] = []
        skipped_name = 0
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isfile(p):
                ext = os.path.splitext(p)[1].lower()
                if ext in cls.SUPPORTED_EXT:
                    files.append(p)
                elif verbose:
                    print(f"  跳过(不支持格式) {os.path.basename(p)}")
            elif os.path.isdir(p):
                for root, _dirs, fnames in os.walk(p):
                    for fname in sorted(fnames):
                        ext = os.path.splitext(fname)[1].lower()
                        if ext not in cls.SUPPORTED_EXT:
                            continue
                        if any(kw in fname for kw in cls.SKIP_FILENAME_PATTERNS):
                            skipped_name += 1
                            continue
                        files.append(os.path.join(root, fname))
            else:
                print(f"  路径不存在: {p}")

        if verbose and skipped_name:
            print(f"  文件名预过滤跳过 {skipped_name} 个（结算单/核算单等）")

        # 按内容去重
        seen: dict = {}
        deduped: List[str] = []
        dup = 0
        for fp in files:
            h = cls._file_sha256(fp)
            if h in seen:
                dup += 1
                continue
            seen[h] = fp
            deduped.append(fp)
        deduped.sort()

        if verbose:
            total_raw = len(files)
            print(f"  收集到 {total_raw} 个文件，去重跳过 {dup} 个，实际 {len(deduped)} 个")
        return deduped

    @staticmethod
    def _file_sha256(path: str, chunk: int = 1 << 20) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fp:
            while True:
                block = fp.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    # ================================================================== #
    # 方法 2：提取尖峰平谷
    # ================================================================== #

    def extract_prices(self, files: List[str], verbose: bool = False,
                       log_fn: Optional[Callable[[str], None]] = None) -> List[PriceRecord]:
        """从文件列表中提取尖峰平谷单价。

        :param files: 文件路径列表
        :param verbose: 打印每张图的提取过程
        :param log_fn: 可选日志回调
        :return: PriceRecord 列表
        """
        records: List[PriceRecord] = []
        total = len(files)
        skipped_content = 0
        skipped_no_price = 0

        TYPE_NAMES = {1: "组件求和(电能+输配+系统运行+基金)",
                      2: "组件求和(含上网环节线损)",
                      3: "直接读单价行",
                      4: "单一电价(所有时段相同)",
                      5: "两行取平均"}

        for fi, fpath in enumerate(files, 1):
            fname = os.path.basename(fpath)

            if not verbose and (fi <= 3 or fi % 10 == 0 or fi == total):
                print(f"  [{fi}/{total}] OCR 处理中… ({len(records)} 条有效)", end="\r")

            try:
                ocr_result = self.ocr.extract_from_image(fpath)
            except Exception as e:
                msg = f"[{fi}/{total}] OCR 失败 {fname}: {e}"
                if log_fn:
                    log_fn(msg)
                elif verbose:
                    print(msg)
                continue

            # 内容过滤：文本必须包含"中国南方电网"
            if self.FILTER_KEYWORD not in ocr_result.raw_text:
                skipped_content += 1
                if verbose:
                    print(f"[{fi}/{total}] OCR {fname} → ✗ 内容不包含\"{self.FILTER_KEYWORD}\"，跳过")
                continue

            # 必须提取到单价数据
            if not ocr_result.has_price_data():
                skipped_no_price += 1
                if verbose:
                    print(f"[{fi}/{total}] OCR {fname} → ✗ 未提取到单价，跳过")
                continue

            # 计算购电月（统计月 - 1）
            reading_month = ocr_result.reading_month
            purchase_month = self._month_minus_one(reading_month) if reading_month else None

            rec = PriceRecord(
                user_id=ocr_result.user_id,
                reading_month=reading_month,
                purchase_month=purchase_month,
                sharp_peak_price=ocr_result.sharp_peak_price,
                peak_price=ocr_result.peak_price,
                flat_price=ocr_result.flat_price,
                valley_price=ocr_result.valley_price,
                average_price=ocr_result.average_price,
                bill_type=ocr_result.bill_type,
                source_file=fpath,
                confidence=ocr_result.confidence,
            )
            records.append(rec)

            if verbose:
                bt = ocr_result.bill_type
                type_info = TYPE_NAMES.get(bt, f"第{bt}种")
                ym = reading_month or "—"
                pm = purchase_month or "—"
                uid = ocr_result.user_id or "?"
                sp = f"{ocr_result.sharp_peak_price}" if ocr_result.sharp_peak_price is not None else "-"
                p = f"{ocr_result.peak_price}" if ocr_result.peak_price is not None else "-"
                fl = f"{ocr_result.flat_price}" if ocr_result.flat_price is not None else "-"
                v = f"{ocr_result.valley_price}" if ocr_result.valley_price is not None else "-"
                print(f"[{fi}/{total}] OCR {fname}")
                print(f"        用户={uid} 统计月={ym} 购电月={pm} 类型=第{bt}种({type_info})")
                print(f"        尖={sp} 峰={p} 平={fl} 谷={v}  均价={ocr_result.average_price}")

        if verbose:
            total_ok = len(records)
            print(f"\n-- 提取完成：有效 {total_ok} 条，内容过滤跳过 {skipped_content} 张，"
                  f"无单价跳过 {skipped_no_price} 张 --\n")
        return records

    @staticmethod
    def _month_minus_one(ym: str) -> Optional[str]:
        """统计月 → 购电月（-1 个月）。"""
        try:
            parts = ym.split("-")
            y, m = int(parts[0]), int(parts[1])
            d = datetime(y, m, 1) - timedelta(days=1)
            return d.strftime("%Y-%m")
        except (IndexError, ValueError):
            return None

    # ================================================================== #
    # 方法 3：与 Excel 比对
    # ================================================================== #

    @classmethod
    def load_excel(cls, xlsx_path: str) -> List[ExcelRow]:
        """读取对比基准 Excel（电费通知单统计.xlsx）。

        约定列顺序：序号 | 用户编号 | 月份 | 正有功尖峰 | 正有功峰 | 正有功平 | 正有功谷 | 备注
        用户编号在合并单元格中只有第一行有值，需向下填充。
        """
        import openpyxl

        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb[wb.sheetnames[0]]

        rows: List[ExcelRow] = []
        cur_uid: Optional[str] = None

        for raw in ws.iter_rows(min_row=2, values_only=True):
            if raw is None or len(raw) < 5:
                continue
            seq, uid, month_val, sp, p, fl, v = raw[:7]
            note = str(raw[7]).strip() if len(raw) > 7 and raw[7] is not None else ""

            # 用户编号向下填充
            if uid is not None:
                cur_uid = str(uid).strip()

            if cur_uid is None or month_val is None:
                continue

            purchase_month = cls._excel_serial_to_month(month_val)
            if purchase_month is None:
                continue

            rows.append(ExcelRow(
                user_id=cur_uid,
                purchase_month=purchase_month,
                sharp_peak_price=float(sp) if sp is not None else None,
                peak_price=float(p) if p is not None else None,
                flat_price=float(fl) if fl is not None else None,
                valley_price=float(v) if v is not None else None,
                note=note,
            ))
        return rows

    @staticmethod
    def _excel_serial_to_month(value) -> Optional[str]:
        """Excel 日期序列号 → 'YYYY-MM'。"""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m")
        if isinstance(value, (int, float)):
            d = datetime(1899, 12, 30) + timedelta(days=int(value))
            return d.strftime("%Y-%m")
        m = re.search(r"(20\d{2})\D+?(\d{1,2})", str(value))
        if m:
            return f"{int(m.group(1))}-{int(m.group(2)):02d}"
        return None

    def match_with_excel(self, records: List[PriceRecord], xlsx_path: str) -> MatchResult:
        """把提取记录与 Excel 精确比对。

        Excel 每一条都必须被提取数据命中；提取数据允许有多余。
        四项单价（尖/峰/平/谷）全部精确相等才算命中。
        """
        excel_rows = self.load_excel(xlsx_path)

        # 构建精确索引：compare_key → PriceRecord
        exact_map: dict = {}
        for r in records:
            if r.user_id and r.purchase_month and r.has_price():
                exact_map[r.compare_key()] = r

        # 退化索引：user_id + purchase_month → list (不比对价格)
        by_uid_month: dict = {}
        by_uid: dict = {}
        for r in records:
            if r.user_id and r.purchase_month:
                by_uid_month.setdefault((r.user_id.strip(), r.purchase_month), r)
            if r.user_id:
                by_uid.setdefault(r.user_id.strip(), r)

        matched = 0
        unmatched_rows: List[ExcelRow] = []
        details: List[RowDetail] = []

        for row in excel_rows:
            key = row.compare_key()
            rec = exact_map.get(key)

            if rec is not None:
                matched += 1
                details.append(self._make_detail(row, rec, full_match=True))
            else:
                unmatched_rows.append(row)
                # 展示对照：优先同用户+月份，其次同用户
                fallback = by_uid_month.get((row.user_id, row.purchase_month))
                if fallback is None:
                    fallback = by_uid.get(row.user_id)
                details.append(self._make_detail(row, fallback, full_match=False))

        return MatchResult(
            total_excel=len(excel_rows),
            matched=matched,
            unmatched_rows=unmatched_rows,
            extracted_records=records,
            details=details,
        )

    @staticmethod
    def _make_detail(row: ExcelRow, rec: Optional[PriceRecord], full_match: bool) -> RowDetail:
        if rec is None:
            return RowDetail(row, None, False, False, False, False, False, False,
                             "未提取到对应记录")
        id_ok = (rec.user_id or "").strip() == row.user_id.strip()
        month_ok = (rec.purchase_month or "") == row.purchase_month
        sp_ok = rec.sharp_peak_price == row.sharp_peak_price
        p_ok = rec.peak_price == row.peak_price
        f_ok = rec.flat_price == row.flat_price
        v_ok = rec.valley_price == row.valley_price
        src = os.path.basename(rec.source_file) if rec.source_file else ""
        bt = f"类型{rec.bill_type}" if rec.bill_type else ""
        src_extra = f"{bt} {src}" if bt else src
        return RowDetail(row, rec, id_ok, month_ok, sp_ok, p_ok, f_ok, v_ok, src_extra)


# ----------------------------------------------------------------------------- #
# 命令行入口
# ----------------------------------------------------------------------------- #

def _main(argv: List[str]) -> int:
    flags = set()
    positional = []
    xlsx_path = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--xlsx" and i + 1 < len(argv):
            xlsx_path = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            flags.add(a)
            i += 1
        else:
            positional.append(a)
            i += 1

    if not positional or not xlsx_path:
        print(__doc__)
        print("用法: python bill_price_tool.py <文件/目录>... --xlsx <对比用Excel.xlsx> [选项]")
        print("选项:")
        print("  --verbose      打印每张图的提取过程")
        print("  --detail       逐项输出比对明细（默认只输出汇总）")
        print("  --fail-only    只打印未匹配的行")
        print("  --save <json>  保存提取结果到 JSON 文件")
        return 1

    verbose = "--verbose" in flags
    show_detail = "--detail" in flags
    fail_only = "--fail-only" in flags
    save_path = None
    if "--save" in flags:
        idx = argv.index("--save")
        if idx + 1 < len(argv):
            save_path = argv[idx + 1]

    if not os.path.exists(xlsx_path):
        print(f"错误: 对比文件不存在 {xlsx_path}")
        return 1

    tool = BillPriceTool()

    # 步骤 1：收集文件
    print("[方法1] 收集文件（按内容去重）…")
    files = tool.collect_images(positional, verbose=verbose)
    if not files:
        print("没有可处理的文件")
        return 1
    print(f"[方法1] 共 {len(files)} 个文件\n")

    # 步骤 2：提取尖峰平谷
    print(f"[方法2] OCR 提取尖峰平谷（只保留含\"{BillPriceTool.FILTER_KEYWORD}\"的图片）…")
    records = tool.extract_prices(files, verbose=verbose or show_detail)
    print(f"[方法2] 有效结果 {len(records)} 条\n")

    # 保存结果
    if save_path and records:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "w") as f:
            json.dump([
                dict(user_id=r.user_id,
                     reading_month=r.reading_month,
                     purchase_month=r.purchase_month,
                     sharp_peak_price=r.sharp_peak_price,
                     peak_price=r.peak_price,
                     flat_price=r.flat_price,
                     valley_price=r.valley_price,
                     average_price=r.average_price,
                     bill_type=r.bill_type,
                     source=r.source_file)
                for r in records
            ], f, ensure_ascii=False, indent=2)
        print(f"结果已保存: {save_path}\n")

    # 步骤 3：与 Excel 比对
    print(f"[方法3] 与 {os.path.basename(xlsx_path)} 精确比对…")
    result = tool.match_with_excel(records, xlsx_path)
    print(result.detail_report(only_fail=fail_only))
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
