"""主处理管线：智能化协调邮件抓取、解析、入库、归档、可视化。

支持任意格式的附件和邮件正文，自动检测文件类型并路由到对应解析器。
"""

import os
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.data.models import Database
from src.email_fetcher.fetcher import EmailFetcher, EmailAttachment
from src.parsers.excel_parser import ExcelParser
from src.parsers.pdf_parser import PDFParser
from src.parsers.csv_parser import CSVParser
from src.parsers.html_parser import HTMLParser
from src.ocr.ocr_engine import OCREngine, OCRResult
from src.archive.archiver import Archiver
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
        self.excel_parser = ExcelParser()
        self.pdf_parser = PDFParser()
        self.csv_parser = CSVParser()
        self.html_parser = HTMLParser()
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

    def process(self, filepath: str, source_info: dict = None) -> dict:
        """智能处理单个文件，返回提取结果。

        Returns:
            {
                "records": list[dict],      # 电表/抄表记录
                "ocr_results": list[OCRResult],  # OCR 单价结果
                "sub_files": list[str],     # 解压出的子文件路径
            }
        """
        fname = Path(filepath).name

        # 跳过 Office 临时锁文件（~$ 开头）
        if fname.startswith("~$"):
            log.debug("  跳过 Office 临时文件: %s", fname)
            return {"records": [], "ocr_results": [], "sub_files": []}

        file_type = self.detect_type(filepath)
        log.info("  [%s] %s", file_type.upper(), fname)

        result = {"records": [], "ocr_results": [], "sub_files": []}

        try:
            if file_type == "excel":
                result["records"] = self.excel_parser.parse(filepath, source_info)

            elif file_type == "pdf":
                result["records"] = self.pdf_parser.parse(filepath, source_info)

            elif file_type == "csv":
                result["records"] = self.csv_parser.parse(filepath, source_info)

            elif file_type == "image":
                ocr = self.ocr_engine.extract_from_image(filepath, source_info)
                if ocr.has_price_data() or ocr.user_id:
                    result["ocr_results"].append(ocr)

            elif file_type in ("html", "text"):
                result["records"] = self.html_parser.parse(filepath, source_info)

            elif file_type == "zip":
                result["sub_files"] = self._extract_zip(filepath)

            elif file_type == "ole":
                # 旧版 XLS 也走 Excel 解析
                result["records"] = self.excel_parser.parse(filepath, source_info)

            else:
                # 未知类型：尝试当文本解析
                log.info("    未知类型，尝试文本解析: %s", fname)
                result["records"] = self.html_parser.parse(filepath, source_info)

        except Exception as e:
            log.error("    解析失败 [%s]: %s", fname, e, exc_info=True)

        rec_count = len(result["records"])
        ocr_count = len(result["ocr_results"])
        sub_count = len(result["sub_files"])
        if rec_count or ocr_count or sub_count:
            log.info("    结果: %d 条记录, %d 条OCR, %d 个子文件",
                     rec_count, ocr_count, sub_count)

        return result

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
        self.archiver = Archiver(self.db)
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

        # 3. 归档
        self._archive_all(attachments)

        # 4. 导出 CSV
        self.db.export_csv()

        # 5. 生成图表
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
        """智能解析所有附件和邮件正文，递归处理压缩包。"""
        log.info("[阶段2] 智能解析所有内容...")

        all_records = []
        all_ocr = []

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

            result = self.dispatcher.process(filepath, source_info)

            all_records.extend(result["records"])
            all_ocr.extend(result["ocr_results"])

            # 如果解压出子文件，加入队列继续处理
            for sub_file in result["sub_files"]:
                queue.append((sub_file, source_info))

        # 写入数据库
        self._save_records(all_records)
        self._save_prices(all_ocr)

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
                        source_file=rec.get("source_file"),
                        source_sheet=rec.get("source_sheet"),
                    )
            except Exception as e:
                log.error("入库失败: %s - %s", rec.get("meter_number"), e)

    def _save_prices(self, ocr_results: list):
        """将 OCR 提取的单价写入数据库。"""
        log.info("写入 %d 条单价记录...", len(ocr_results))
        for ocr in ocr_results:
            if ocr.user_id and ocr.reading_month:
                try:
                    self.db.upsert_price(
                        user_id=ocr.user_id,
                        reading_month=ocr.reading_month,
                        sharp_peak_price=ocr.sharp_peak_price,
                        peak_price=ocr.peak_price,
                        flat_price=ocr.flat_price,
                        valley_price=ocr.valley_price,
                        source_file=ocr.source_file,
                    )
                except Exception as e:
                    log.error("单价入库失败: user=%s - %s", ocr.user_id, e)

    def _archive_all(self, attachments: list[EmailAttachment]):
        """归档所有附件到月份/项目目录。"""
        log.info("[阶段3] 归档附件...")
        for att in attachments:
            if att.is_body:
                continue  # 邮件正文不归档
            month = self._infer_month_for_file(att)
            project = self._infer_project_for_file(att)
            self.archiver.archive_attachment(att.filepath, month, project)

        self.archiver.generate_monthly_summaries()

    def _infer_month_for_file(self, att: EmailAttachment) -> str:
        """推断文件对应的月份。"""
        import re
        for text in [att.filename, att.email_subject]:
            for match in re.finditer(r'(\d{4})[-_年]?(\d{1,2})', text):
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2030 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"
        if att.email_date:
            return att.email_date.strftime("%Y-%m")
        return "unknown"

    def _infer_project_for_file(self, att: EmailAttachment) -> Optional[str]:
        """推断文件对应的项目（从邮件主题或文件名）。"""
        projects = self.db.get_projects()
        for proj in projects:
            if proj in att.filename or proj in att.email_subject:
                return proj
        # 用邮件主题作为项目名的兜底
        if att.email_subject:
            return att.email_subject
        return None

    def _generate_visualizations(self):
        """生成可视化图表。"""
        log.info("[阶段4] 生成图表...")
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
