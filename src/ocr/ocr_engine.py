"""图片 OCR 引擎：从图片中提取单价和用户编号。

支持 PaddleOCR 和 Tesseract 两种引擎，通过配置切换。
单价通过用户编号与抄表数据关联。
"""

import re
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class OCRResult:
    """OCR 提取结果。"""

    def __init__(self):
        self.user_id: Optional[str] = None
        self.sharp_peak_price: Optional[float] = None
        self.peak_price: Optional[float] = None
        self.flat_price: Optional[float] = None
        self.valley_price: Optional[float] = None
        self.reading_month: Optional[str] = None
        self.raw_text: str = ""
        self.source_file: str = ""
        self.confidence: float = 0.0

    def has_price_data(self) -> bool:
        return any(v is not None for v in [
            self.sharp_peak_price, self.peak_price, self.flat_price, self.valley_price
        ])

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "sharp_peak_price": self.sharp_peak_price,
            "peak_price": self.peak_price,
            "flat_price": self.flat_price,
            "valley_price": self.valley_price,
            "reading_month": self.reading_month,
            "source_file": self.source_file,
        }


class OCREngine:
    """OCR 引擎封装。"""

    def __init__(self):
        ocr_cfg = config.get("ocr") or {}
        self.engine_type = ocr_cfg.get("engine", "paddleocr")
        self.confidence_threshold = ocr_cfg.get("confidence_threshold", 0.6)
        self.tesseract_lang = ocr_cfg.get("tesseract_lang", "chi_sim+eng")
        self.extraction_rules = config.get("ocr_extraction_rules") or {}
        self._engine = None

    def _init_engine(self):
        """延迟初始化 OCR 引擎。"""
        if self._engine is not None:
            return

        if self.engine_type == "paddleocr":
            try:
                from paddleocr import PaddleOCR
                self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
                log.info("PaddleOCR 引擎初始化成功")
            except ImportError:
                log.warning("PaddleOCR 未安装，回退到 Tesseract")
                self.engine_type = "tesseract"
                self._init_engine()
        elif self.engine_type == "tesseract":
            try:
                import pytesseract
                pytesseract.get_tesseract_version()
                self._engine = pytesseract
                log.info("Tesseract OCR 引擎初始化成功")
            except Exception as e:
                log.error("Tesseract 不可用: %s", e)
                raise RuntimeError("无可用 OCR 引擎") from e

    def extract_from_image(self, filepath: str, source_info: dict = None) -> OCRResult:
        """从单张图片提取单价和用户编号。"""
        self._init_engine()

        filepath = Path(filepath)
        result = OCRResult()
        result.source_file = filepath.name

        if not filepath.exists():
            log.error("图片文件不存在: %s", filepath)
            return result

        try:
            raw_text = self._run_ocr(str(filepath))
            result.raw_text = raw_text
            log.debug("OCR 原始文本 [%s]:\n%s", filepath.name, raw_text[:500])

            # 提取用户编号
            result.user_id = self._extract_user_id(raw_text)

            # 提取单价
            prices = self._extract_prices(raw_text)
            result.sharp_peak_price = prices.get("sharp_peak_price")
            result.peak_price = prices.get("peak_price")
            result.flat_price = prices.get("flat_price")
            result.valley_price = prices.get("valley_price")

            # 推断月份
            result.reading_month = self._infer_month(raw_text, source_info, filepath.name)

            if result.user_id:
                log.info("  OCR 提取 [%s]: 用户=%s, 尖峰=%.4f, 峰=%.4f, 平=%.4f, 谷=%.4f",
                         filepath.name, result.user_id,
                         result.sharp_peak_price or 0,
                         result.peak_price or 0,
                         result.flat_price or 0,
                         result.valley_price or 0)

        except Exception as e:
            log.error("OCR 处理失败 [%s]: %s", filepath, e, exc_info=True)

        return result

    def _run_ocr(self, filepath: str) -> str:
        """执行 OCR 识别，返回全文文本。"""
        if self.engine_type == "paddleocr":
            ocr_result = self._engine.ocr(filepath, cls=True)
            lines = []
            if ocr_result:
                for line_result in ocr_result:
                    if line_result:
                        for item in line_result:
                            if len(item) >= 2:
                                text = item[1][0] if isinstance(item[1], (list, tuple)) else str(item[1])
                                conf = item[1][1] if isinstance(item[1], (list, tuple)) and len(item[1]) > 1 else 1.0
                                if conf >= self.confidence_threshold:
                                    lines.append(text)
            return "\n".join(lines)

        elif self.engine_type == "tesseract":
            from PIL import Image
            img = Image.open(filepath)
            text = self._engine.image_to_string(img, lang=self.tesseract_lang)
            return text

        return ""

    def _extract_user_id(self, text: str) -> Optional[str]:
        """从文本中提取用户编号。"""
        rules = self.extraction_rules.get("user_id", {})
        patterns = rules.get("patterns", [])

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        # 兜底：查找连续数字串（8-20位）
        numbers = re.findall(r'\b(\d{8,20})\b', text)
        if len(numbers) == 1:
            return numbers[0]

        return None

    def _extract_prices(self, text: str) -> dict:
        """从文本中提取尖峰平谷单价。"""
        prices = {}
        rules = self.extraction_rules.get("unit_price", {})

        price_fields = {
            "sharp_peak_price": rules.get("sharp_peak_price", []),
            "peak_price": rules.get("peak_price", []),
            "flat_price": rules.get("flat_price", []),
            "valley_price": rules.get("valley_price", []),
        }

        for field, patterns in price_fields.items():
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    try:
                        prices[field] = float(match.group(1))
                    except (ValueError, IndexError):
                        pass
                    break

        # 如果未通过专用规则提取到，尝试通用模式
        if not prices:
            general_patterns = rules.get("patterns", [])
            for pattern in general_patterns:
                matches = re.findall(pattern, text)
                if len(matches) >= 4:
                    try:
                        prices["sharp_peak_price"] = float(matches[0])
                        prices["peak_price"] = float(matches[1])
                        prices["flat_price"] = float(matches[2])
                        prices["valley_price"] = float(matches[3])
                    except (ValueError, IndexError):
                        pass
                    break

        return prices

    def _infer_month(self, text: str, source_info: dict, filename: str) -> Optional[str]:
        """推断月份。"""
        # 从 OCR 文本中提取
        month_patterns = [
            r'(\d{4})[-/年](\d{1,2})[-/月]',
            r'(\d{4})(\d{2})(?:月|期)',
        ]
        for pattern in month_patterns:
            match = re.search(pattern, text)
            if match:
                return f"{match.group(1)}-{match.group(2).zfill(2)}"

        # 从文件名推断
        match = re.search(r'(\d{4})[-_年]?(\d{1,2})', filename)
        if match:
            return f"{match.group(1)}-{match.group(2).zfill(2)}"

        # 从邮件日期
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")

        return None
