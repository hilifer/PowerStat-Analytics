"""主处理管线：协调邮件抓取、解析、入库、归档、可视化的完整流程。"""

import os
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.data.models import Database
from src.email_fetcher.fetcher import EmailFetcher, EmailAttachment
from src.parsers.excel_parser import ExcelParser
from src.parsers.pdf_parser import PDFParser
from src.ocr.ocr_engine import OCREngine, OCRResult
from src.archive.archiver import Archiver
from src.visualization.charts import ChartGenerator
from src.logger import log


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}
EXCEL_EXTS = {".xlsx", ".xls"}
PDF_EXTS = {".pdf"}


class Pipeline:
    """端到端处理管线。"""

    def __init__(self, db: Database = None):
        self.db = db or Database()
        self.excel_parser = ExcelParser()
        self.pdf_parser = PDFParser()
        self.ocr_engine = OCREngine()
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

        # 2. 解析附件并入库
        self._process_attachments(attachments)

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
        """从本地临时目录加载已有附件。"""
        temp_dir = Path(config.get("attachments", "temp_dir", default="output/temp_attachments"))
        if not temp_dir.exists():
            return []

        supported = set(config.get("attachments", "supported_formats", default=[]))
        attachments = []
        for f in temp_dir.iterdir():
            if f.is_file() and f.suffix.lower() in supported:
                att = EmailAttachment(
                    filename=f.name,
                    filepath=str(f),
                    content_type="",
                    email_date=None,
                    email_subject="",
                    email_sender="",
                )
                attachments.append(att)
        log.info("从本地加载 %d 个附件", len(attachments))
        return attachments

    def _process_attachments(self, attachments: list[EmailAttachment]):
        """解析所有附件并写入数据库。"""
        log.info("[阶段2] 解析附件并入库...")

        ocr_results: list[OCRResult] = []
        meter_records: list[dict] = []

        for att in attachments:
            ext = Path(att.filepath).suffix.lower()
            source_info = {
                "email_date": att.email_date,
                "email_subject": att.email_subject,
                "filename": att.filename,
            }

            if ext in EXCEL_EXTS:
                log.info("解析 Excel: %s", att.filename)
                records = self.excel_parser.parse(att.filepath, source_info)
                meter_records.extend(records)

            elif ext in PDF_EXTS:
                log.info("解析 PDF: %s", att.filename)
                records = self.pdf_parser.parse(att.filepath, source_info)
                meter_records.extend(records)

            elif ext in IMAGE_EXTS:
                log.info("OCR 图片: %s", att.filename)
                result = self.ocr_engine.extract_from_image(att.filepath, source_info)
                if result.has_price_data() or result.user_id:
                    ocr_results.append(result)

        # 写入电表和抄表数据
        log.info("写入 %d 条电表/抄表记录...", len(meter_records))
        for rec in meter_records:
            try:
                meter_id = self.db.upsert_meter(
                    meter_number=rec.get("meter_number", ""),
                    user_id=rec.get("user_id", ""),
                    meter_type=rec.get("meter_type", "未知"),
                    asset_number=rec.get("asset_number"),
                    multiplier=rec.get("multiplier", 1.0),
                    project_name=rec.get("project_name"),
                )
                if rec.get("reading_month") and rec["reading_month"] != "unknown":
                    self.db.upsert_reading(
                        meter_id=meter_id,
                        reading_month=rec["reading_month"],
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

        # 写入单价数据
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
            # 从已入库数据推断月份和项目
            month = self._infer_month_for_file(att)
            project = self._infer_project_for_file(att)
            self.archiver.archive_attachment(att.filepath, month, project)

        self.archiver.generate_monthly_summaries()

    def _infer_month_for_file(self, att: EmailAttachment) -> str:
        """推断文件对应的月份。"""
        import re
        match = re.search(r'(\d{4})[-_年]?(\d{1,2})', att.filename)
        if match:
            return f"{match.group(1)}-{match.group(2).zfill(2)}"
        if att.email_date:
            return att.email_date.strftime("%Y-%m")
        return "unknown"

    def _infer_project_for_file(self, att: EmailAttachment) -> Optional[str]:
        """推断文件对应的项目（从邮件主题或文件名）。"""
        projects = self.db.get_projects()
        for proj in projects:
            if proj in att.filename or proj in att.email_subject:
                return proj
        return None

    def _generate_visualizations(self):
        """生成可视化图表。"""
        log.info("[阶段4] 生成图表...")
        # 全局图表
        self.chart_gen.generate_all()

        # 按项目生成
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
