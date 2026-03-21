"""主处理管线：智能化协调邮件抓取、解析、入库、归档、可视化。

支持任意格式的附件和邮件正文，自动检测文件类型并路由到对应解析器。
"""

import os
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.data.models import Database
from src.email_fetcher.fetcher import EmailFetcher, EmailAttachment
from src.parsers.multi_pass import MultiPassExtractor
from src.parsers.text_extractor import extract_meters_from_text, read_text_file
from src.ocr.ocr_engine import OCREngine, OCRResult
from src.visualization.charts import ChartGenerator
from src.logger import log


class SmartDispatcher:
    """智能文件分发器：自动检测文件类型并调用对应解析器。

    处理逻辑：
    1. 根据扩展名判断文件类型
    2. 扩展名不可靠时，通过文件头魔数(magic bytes)检测真实类型
    3. 所有能提取数据的格式都处理，未知格式尝试当文本解析
    """

    # 文件头魔数 -> 真实类型
    MAGIC_BYTES = {
        b"PK\x03\x04": "zip",      # ZIP / XLSX / DOCX
        b"\xd0\xcf\x11\xe0": "ole", # XLS / DOC (OLE2)
        b"%PDF": "pdf",
        b"\x89PNG": "image",
        b"\xff\xd8\xff": "image",   # JPEG
        b"GIF8": "image",
        b"BM": "image",             # BMP
        b"II\x2a\x00": "image",     # TIFF LE
        b"MM\x00\x2a": "image",     # TIFF BE
        b"Rar!": "rar",
        b"7z\xbc\xaf": "7z",
    }

    def __init__(self):
        self.ocr_engine = OCREngine()

    def detect_type(self, filepath: str) -> str:
        """检测文件的实际类型，返回类别字符串。"""
        ext = Path(filepath).suffix.lower()

        # 扩展名直接映射
        ext_map = {
            ".xlsx": "excel", ".xls": "excel",
            ".pdf": "pdf",
            ".csv": "csv", ".tsv": "csv",
            ".txt": "text",
            ".html": "html", ".htm": "html",
            ".png": "image", ".jpg": "image", ".jpeg": "image",
            ".bmp": "image", ".tiff": "image", ".tif": "image", ".gif": "image",
            ".zip": "zip", ".rar": "rar", ".7z": "7z",
            ".doc": "ole", ".docx": "zip",
        }

        if ext in ext_map:
            return ext_map[ext]

        # 扩展名不可靠，用魔数检测
        try:
            with open(filepath, "rb") as f:
                header = f.read(8)
            for magic, file_type in self.MAGIC_BYTES.items():
                if header[:len(magic)] == magic:
                    # ZIP 可能是 XLSX
                    if file_type == "zip":
                        return self._check_zip_subtype(filepath)
                    return file_type
        except Exception:
            pass

        # 尝试当文本读
        try:
            with open(filepath, "r", encoding="utf-8", errors="strict") as f:
                sample = f.read(2000)
            if "<table" in sample.lower() or "<html" in sample.lower():
                return "html"
            return "text"
        except (UnicodeDecodeError, Exception):
            pass

        return "unknown"

    def _check_zip_subtype(self, filepath: str) -> str:
        """检查 ZIP 文件是否为 XLSX 等 Office 格式。"""
        import zipfile
        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                names = zf.namelist()
                if any("xl/" in n or "xl\\" in n for n in names):
                    return "excel"
                if any("word/" in n for n in names):
                    return "text"  # DOCX 当文本处理
        except Exception:
            pass
        return "zip"

    def load_as_dataframes(self, filepath: str, source_info: dict = None) -> list:
        """将文件加载为 DataFrame 列表，供多轮扫描使用。

        Returns:
            [(df, filepath_obj, sheet_name, source_info), ...]
        """
        import pandas as pd
        import openpyxl

        fname = Path(filepath).name
        filepath_obj = Path(filepath)

        if fname.startswith("~$"):
            return []

        file_type = self.detect_type(filepath)
        sheets = []

        try:
            if file_type in ("excel", "ole"):
                ext = filepath_obj.suffix.lower()
                if ext == ".xls":
                    xls = pd.ExcelFile(str(filepath), engine="xlrd")
                    for sn in xls.sheet_names:
                        df = pd.read_excel(xls, sheet_name=sn, header=None)
                        sheets.append((df, filepath_obj, sn, source_info))
                else:
                    wb = openpyxl.load_workbook(str(filepath), read_only=True, data_only=True)
                    for sn in wb.sheetnames:
                        data = list(wb[sn].values)
                        if data:
                            df = pd.DataFrame(data)
                            sheets.append((df, filepath_obj, sn, source_info))
                    wb.close()

            elif file_type == "pdf":
                try:
                    import pdfplumber
                    with pdfplumber.open(filepath) as pdf:
                        for i, page in enumerate(pdf.pages):
                            tables = page.extract_tables()
                            for j, table in enumerate(tables):
                                if table and len(table) > 1:
                                    df = pd.DataFrame(table)
                                    sheets.append((df, filepath_obj, f"PDF_p{i+1}_t{j+1}", source_info))
                except Exception as e:
                    log.error("  PDF 加载失败: %s", e)

            elif file_type == "csv":
                for enc in ["utf-8", "gbk", "gb2312", "utf-8-sig"]:
                    try:
                        for sep in [",", "\t", "|"]:
                            df = pd.read_csv(filepath, encoding=enc, sep=sep, header=None)
                            if len(df.columns) > 1:
                                sheets.append((df, filepath_obj, "CSV", source_info))
                                break
                        if sheets:
                            break
                    except Exception:
                        continue

            elif file_type in ("html", "text"):
                try:
                    dfs = pd.read_html(filepath)
                    for i, df in enumerate(dfs):
                        df = df.reset_index(drop=True)
                        df.columns = range(len(df.columns))
                        sheets.append((df, filepath_obj, f"HTML_t{i+1}", source_info))
                except Exception:
                    pass

            else:
                # 不认识的类型：先试当 HTML 表格解析，再试当 CSV 解析
                log.warning("  未知类型 [%s] %s，尝试兜底解析", file_type, fname)
                try:
                    dfs = pd.read_html(filepath)
                    for i, df in enumerate(dfs):
                        df = df.reset_index(drop=True)
                        df.columns = range(len(df.columns))
                        sheets.append((df, filepath_obj, f"FALLBACK_t{i+1}", source_info))
                except Exception:
                    pass
                if not sheets:
                    for enc in ["utf-8", "gbk", "gb2312"]:
                        try:
                            df = pd.read_csv(filepath, encoding=enc, header=None)
                            if len(df.columns) > 1:
                                sheets.append((df, filepath_obj, "FALLBACK_CSV", source_info))
                                break
                        except Exception:
                            continue
                if not sheets:
                    log.warning("  跳过无法解析的文件: %s", fname)

        except Exception as e:
            log.error("  文件加载失败 [%s]: %s", fname, e)

        if sheets:
            log.info("  加载 [%s] %s: %d 个 sheet", file_type.upper(), fname, len(sheets))

        return sheets

    def _extract_zip(self, filepath: str) -> list[str]:
        """解压 ZIP，返回内部文件路径列表。"""
        import zipfile
        if not zipfile.is_zipfile(filepath):
            return []

        extract_dir = Path(filepath).parent / Path(filepath).stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        extracted = []

        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue

                    # 中文文件名编码处理
                    try:
                        inner_name = info.filename.encode("cp437").decode("gbk")
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        try:
                            inner_name = info.filename.encode("cp437").decode("utf-8")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            inner_name = info.filename

                    import re
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', os.path.basename(inner_name))
                    safe_name = safe_name.strip('. ')[:200] or "unnamed"

                    dest_path = extract_dir / safe_name
                    counter = 1
                    orig_stem = dest_path.stem
                    while dest_path.exists():
                        dest_path = extract_dir / f"{orig_stem}_{counter}{dest_path.suffix}"
                        counter += 1

                    with zf.open(info) as src, open(dest_path, "wb") as dst:
                        dst.write(src.read())

                    extracted.append(str(dest_path))
                    log.info("    解压: %s", safe_name)

        except Exception as e:
            log.error("    解压失败: %s", e)

        return extracted


class Pipeline:
    """端到端处理管线。"""

    def __init__(self, db: Database = None):
        self.db = db or Database()
        self.dispatcher = SmartDispatcher()
        self.chart_gen = ChartGenerator(self.db)

    def run_full(self, skip_fetch: bool = False, skip_viz: bool = False):
        """执行完整流程。"""
        log.info("=" * 60)
        log.info("PowerStat-Analytics 管线启动")
        log.info("=" * 60)

        # 1. 抓取邮件附件
        if not skip_fetch:
            attachments = self._fetch_emails()
        else:
            attachments = self._load_local_attachments()

        if not attachments:
            log.info("无附件需处理")
            return

        # 2. 智能解析所有内容并入库
        self._process_all(attachments)

        # 3. 导出 CSV
        self.db.export_csv()

        # 4. 生成图表
        if not skip_viz:
            self._generate_visualizations()

        log.info("=" * 60)
        log.info("管线执行完成")
        log.info("=" * 60)

    def _fetch_emails(self) -> list[EmailAttachment]:
        """从邮箱抓取附件。"""
        log.info("[阶段1] 抓取邮件附件...")
        try:
            with EmailFetcher() as fetcher:
                return fetcher.fetch_attachments()
        except Exception as e:
            log.error("邮件抓取失败: %s", e, exc_info=True)
            return []

    def _load_local_attachments(self) -> list[EmailAttachment]:
        """从本地临时目录加载已有附件（递归扫描，不限格式）。"""
        temp_dir = Path(config.get("attachments", "temp_dir", default="output/temp_attachments"))
        if not temp_dir.exists():
            return []

        attachments = []
        for f in temp_dir.rglob("*"):
            if f.is_file():
                att = EmailAttachment(
                    filename=f.name,
                    filepath=str(f),
                    content_type="",
                    email_date=None,
                    email_subject="",
                    email_sender="",
                )
                attachments.append(att)
        log.info("从本地加载 %d 个文件", len(attachments))
        return attachments

    def _process_all(self, attachments: list[EmailAttachment]):
        """多轮扫描：先加载所有文件为 DataFrame，再多轮提取。"""
        log.info("[阶段2] 加载所有文件...")

        all_sheets = []   # [(df, filepath, sheet_name, source_info), ...]
        all_ocr = []
        all_text_records = []  # 文本文件提取的电表记录

        # 待处理队列（支持递归解压）
        queue = [(att.filepath, {
            "email_date": att.email_date,
            "email_subject": att.email_subject,
            "filename": att.filename,
        }) for att in attachments]

        processed_paths = set()

        while queue:
            filepath, source_info = queue.pop(0)

            if filepath in processed_paths:
                continue
            processed_paths.add(filepath)

            # 加载为 DataFrame
            sheets = self.dispatcher.load_as_dataframes(filepath, source_info)
            all_sheets.extend(sheets)

            # 图片走 OCR
            file_type = self.dispatcher.detect_type(filepath)
            if file_type == "image":
                try:
                    ocr = self.dispatcher.ocr_engine.extract_from_image(filepath, source_info)
                    if ocr.has_any_data():
                        all_ocr.append(ocr)
                except Exception as e:
                    log.error("  OCR 失败: %s", e)

            # 文本文件尝试提取电表档案信息
            if file_type in ("text", "unknown") or (file_type in ("html",) and not sheets):
                try:
                    text = read_text_file(filepath)
                    text_records = extract_meters_from_text(text, filepath)
                    all_text_records.extend(text_records)
                except Exception as e:
                    log.error("  文本提取失败 [%s]: %s", filepath, e)

            # 压缩包解压后加入队列
            if file_type == "zip":
                sub_files = self.dispatcher._extract_zip(filepath)
                for sf in sub_files:
                    queue.append((sf, source_info))

        log.info("共加载 %d 个 sheet", len(all_sheets))

        # 多轮扫描提取
        extractor = MultiPassExtractor()
        extractor.load_dataframes(all_sheets)
        all_records = extractor.extract_all()

        # 合并文本文件提取的记录
        if all_text_records:
            log.info("文本提取: %d 条电表记录", len(all_text_records))
            all_records.extend(all_text_records)

        # 写入数据库
        self._save_records(all_records)
        self._save_prices(all_ocr)

        # 数据关联补齐
        self._reconcile_data(all_records)

        # 推理补全缺失数据
        log.info("[阶段2.6] 推理补全缺失数据...")
        self.db.infer_missing_data()

        # 清理数据不全的电表
        log.info("[阶段2.7] 清理数据不全的电表...")
        self.db.cleanup_incomplete_meters()

    def _save_records(self, records: list[dict]):
        """将电表/抄表记录写入数据库。"""
        log.info("写入 %d 条电表/抄表记录...", len(records))
        for rec in records:
            meter_number = rec.get("meter_number", "").strip()
            if not meter_number:
                continue
            try:
                meter_id = self.db.upsert_meter(
                    meter_number=meter_number,
                    user_id=rec.get("user_id"),
                    meter_type=rec.get("meter_type", "未知"),
                    asset_number=rec.get("asset_number"),
                    multiplier=rec.get("multiplier"),
                    project_name=rec.get("project_name"),
                )
                # 折扣单独更新（仅在有值且未锁定时）
                discount = rec.get("discount")
                if discount and discount != 1.0:
                    self.db.update_meter(meter_number, discount=discount)

                month = rec.get("reading_month")
                if month and month != "unknown":
                    self.db.upsert_reading(
                        meter_id=meter_id,
                        reading_month=month,
                        sharp_peak=rec.get("sharp_peak"),
                        peak=rec.get("peak"),
                        flat=rec.get("flat"),
                        valley=rec.get("valley"),
                        total_kwh=rec.get("total_kwh"),
                        rev_sharp_peak=rec.get("rev_sharp_peak"),
                        rev_peak=rec.get("rev_peak"),
                        rev_flat=rec.get("rev_flat"),
                        rev_valley=rec.get("rev_valley"),
                        rev_total=rec.get("rev_total"),
                        cur_sharp_peak=rec.get("cur_sharp_peak"),
                        cur_peak=rec.get("cur_peak"),
                        cur_flat=rec.get("cur_flat"),
                        cur_valley=rec.get("cur_valley"),
                        cur_total=rec.get("cur_total"),
                        prev_sharp_peak=rec.get("prev_sharp_peak"),
                        prev_peak=rec.get("prev_peak"),
                        prev_flat=rec.get("prev_flat"),
                        prev_valley=rec.get("prev_valley"),
                        prev_total=rec.get("prev_total"),
                        stat_date=rec.get("stat_date"),
                        source_file=rec.get("source_file"),
                        source_sheet=rec.get("source_sheet"),
                    )

                # 如果记录里有电价和月份，也入库为 price_record
                user_id = rec.get("user_id")
                has_period_prices = any(rec.get(k) is not None for k in
                                        ("sharp_peak_price", "peak_price",
                                         "flat_price", "valley_price"))
                unit_price = rec.get("unit_price")

                if user_id and month and month != "unknown" and (has_period_prices or unit_price):
                    self.db.upsert_price(
                        user_id=user_id,
                        reading_month=month,
                        sharp_peak_price=rec.get("sharp_peak_price"),
                        peak_price=rec.get("peak_price"),
                        flat_price=rec.get("flat_price") or unit_price,
                        valley_price=rec.get("valley_price"),
                        source_file=rec.get("source_file"),
                    )

            except Exception as e:
                log.error("入库失败: %s - %s", rec.get("meter_number"), e)

    def _save_prices(self, ocr_results: list):
        """将 OCR 提取的单价写入数据库。

        OCR 提取的 user_id 可能不完整（截断）或错误（如时间戳），
        需要与 meters 表中已知的 user_id 做后缀匹配修正。
        """
        log.info("写入 %d 条单价记录...", len(ocr_results))

        # 获取已知的 user_id 列表用于匹配修正
        known_user_ids = set()
        try:
            meters = self.db.get_meters()
            for m in meters:
                uid = m.get("user_id")
                if uid:
                    known_user_ids.add(uid)
        except Exception:
            pass

        saved, skipped = 0, 0
        for ocr in ocr_results:
            if not ocr.reading_month:
                continue
            # 检查是否有任何有效的单价数据
            has_price = any(v is not None for v in [
                ocr.sharp_peak_price, ocr.peak_price,
                ocr.flat_price, ocr.valley_price, ocr.average_price,
            ])
            if not has_price:
                continue

            user_id = ocr.user_id
            if not user_id:
                skipped += 1
                log.warning("单价无 user_id，跳过: src=%s", ocr.source_file)
                continue

            # 修正 user_id：如果不在已知列表中，尝试后缀匹配
            if user_id not in known_user_ids and known_user_ids:
                matched = self._match_user_id(user_id, known_user_ids)
                if matched:
                    log.info("单价 user_id 修正: %s -> %s (src=%s)",
                             user_id, matched, ocr.source_file)
                    user_id = matched
                else:
                    skipped += 1
                    log.warning("单价 user_id 无法匹配到已知用户，跳过: "
                                "user_id=%s, src=%s", user_id, ocr.source_file)
                    continue

            try:
                self.db.upsert_price(
                    user_id=user_id,
                    reading_month=ocr.reading_month,
                    sharp_peak_price=ocr.sharp_peak_price,
                    peak_price=ocr.peak_price,
                    flat_price=ocr.flat_price,
                    valley_price=ocr.valley_price,
                    average_price=ocr.average_price,
                    source_file=ocr.source_file,
                )
                saved += 1
            except Exception as e:
                log.error("单价入库失败: user=%s - %s", user_id, e)

        log.info("单价入库完成: 成功 %d, 跳过 %d", saved, skipped)

    @staticmethod
    def _match_user_id(ocr_uid: str, known_ids: set) -> Optional[str]:
        """用后缀匹配将 OCR 提取的 user_id 修正为已知的完整 user_id。

        OCR 可能截断前缀（如 '000082501856' 应为 '0946000082501856'），
        用后缀匹配找到唯一对应的已知 user_id。
        """
        import re
        # 排除明显是时间戳的 user_id（20YYMMDD 开头）
        if re.match(r'^20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', ocr_uid):
            return None

        # 去掉前导零后做后缀匹配
        ocr_stripped = ocr_uid.lstrip('0')
        if len(ocr_stripped) < 4:
            return None

        candidates = []
        for kid in known_ids:
            # 完整 user_id 以 OCR 提取的尾部结尾
            if kid.endswith(ocr_stripped):
                candidates.append(kid)
            # 或者 OCR 提取的以完整 user_id 尾部结尾
            elif ocr_stripped.endswith(kid.lstrip('0')):
                candidates.append(kid)

        if len(candidates) == 1:
            return candidates[0]
        return None

    def _reconcile_data(self, all_records: list[dict]):
        """数据关联补齐：跨文件交叉引用，填补缺失字段。

        策略：
        1. 用户编号关联：同一用户编号的电表共享项目名、折扣
        2. 配对电表关联：发电表号 ↔ 上网表号 共享用户编号和项目
        3. 项目名补齐：通过同源文件传递项目名
        """
        log.info("[阶段2.5] 数据关联补齐...")
        meters = self.db.get_meters()
        if not meters:
            log.info("  无电表数据，跳过关联")
            return

        # 建立索引
        by_user = {}     # user_id -> [meter_dict]
        by_meter = {}    # meter_number -> meter_dict
        for m in meters:
            by_meter[m["meter_number"]] = m
            uid = m.get("user_id")
            if uid:
                by_user.setdefault(uid, []).append(m)

        # 从 all_records 收集配对关系和跨文件信息
        pairs = []        # (meter, paired_meter)
        file_meters = {}  # source_file -> [meter_number]
        file_project = {} # source_file -> project_name
        file_user = {}    # source_file -> user_id
        file_discount = {}  # source_file -> discount

        for rec in all_records:
            mn = rec.get("meter_number", "").strip()
            sf = rec.get("source_file", "")
            if mn:
                file_meters.setdefault(sf, []).append(mn)
            if rec.get("project_name"):
                file_project[sf] = rec["project_name"]
            if rec.get("user_id"):
                file_user[sf] = rec["user_id"]
            if rec.get("discount") and rec["discount"] != 1.0:
                file_discount[sf] = rec["discount"]

            # 配对关系（由 multi_pass 提取器产出）
            paired = rec.get("paired_meter", "").strip() if rec.get("paired_meter") else ""
            if paired and mn and paired != mn:
                pairs.append((mn, paired))

        updates_count = 0

        # 1. 通过配对关系传递用户编号和项目名
        for gen, grid in pairs:
            gen_m = by_meter.get(gen)
            grid_m = by_meter.get(grid)
            if not gen_m or not grid_m:
                continue

            # 传递用户编号
            if gen_m.get("user_id") and not grid_m.get("user_id"):
                self.db.update_meter(grid, user_id=gen_m["user_id"])
                updates_count += 1
            elif grid_m.get("user_id") and not gen_m.get("user_id"):
                self.db.update_meter(gen, user_id=grid_m["user_id"])
                updates_count += 1

            # 传递项目名
            if gen_m.get("project_name") and not grid_m.get("project_name"):
                self.db.update_meter(grid, project_name=gen_m["project_name"])
                updates_count += 1
            elif grid_m.get("project_name") and not gen_m.get("project_name"):
                self.db.update_meter(gen, project_name=grid_m["project_name"])
                updates_count += 1

        # 2. 同一用户编号的电表共享项目名和折扣
        for uid, meter_list in by_user.items():
            project = next((m["project_name"] for m in meter_list if m.get("project_name")), None)
            discount = next((m.get("discount") for m in meter_list if m.get("discount") and m["discount"] != 1.0), None)

            for m in meter_list:
                changed = {}
                if project and not m.get("project_name"):
                    changed["project_name"] = project
                if discount and (not m.get("discount") or m["discount"] == 1.0):
                    changed["discount"] = discount
                if changed:
                    self.db.update_meter(m["meter_number"], **changed)
                    updates_count += 1

        # 3. 同源文件的电表共享项目名、用户编号、折扣
        for sf, meter_nums in file_meters.items():
            project = file_project.get(sf)
            user = file_user.get(sf)
            discount = file_discount.get(sf)

            for mn in meter_nums:
                m = by_meter.get(mn)
                if not m:
                    continue
                changed = {}
                if project and not m.get("project_name"):
                    changed["project_name"] = project
                if user and not m.get("user_id"):
                    changed["user_id"] = user
                if discount and (not m.get("discount") or m["discount"] == 1.0):
                    changed["discount"] = discount
                if changed:
                    self.db.update_meter(mn, **changed)
                    updates_count += 1

        log.info("  关联补齐完成: %d 条更新", updates_count)

    def _generate_visualizations(self):
        """生成可视化图表。"""
        log.info("[阶段6] 生成图表...")
        self.chart_gen.generate_all()
        for project in self.db.get_projects():
            self.chart_gen.generate_all(project_name=project)

    def query(self, project_name: str = None, user_id: str = None,
              meter_number: str = None, month_from: str = None,
              month_to: str = None) -> list[dict]:
        """查询账单数据。"""
        return self.db.get_monthly_bill(
            project_name=project_name,
            user_id=user_id,
            meter_number=meter_number,
            month_from=month_from,
            month_to=month_to,
        )
