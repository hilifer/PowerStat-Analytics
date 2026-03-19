"""图片 OCR 引擎：从图片中提取单价和用户编号。

支持 PaddleOCR 和 Tesseract 两种引擎，通过配置切换。
单价通过用户编号与抄表数据关联。

优化点：
- 图片预处理（灰度化、对比度增强、二值化）提升 OCR 准确率
- 多级正则匹配策略（表格行级 → 标签级 → 通用模式）
- 从文件名推断月份和用户编号作为补充
"""

import os
import re
from pathlib import Path
from typing import Optional

from src.config_loader import config
from src.logger import log


class OCRResult:
    """OCR 提取结果：包含单价数据和/或电表记录。"""

    def __init__(self):
        self.user_id: Optional[str] = None
        self.sharp_peak_price: Optional[float] = None
        self.peak_price: Optional[float] = None
        self.flat_price: Optional[float] = None
        self.valley_price: Optional[float] = None
        self.average_price: Optional[float] = None
        self.reading_month: Optional[str] = None
        self.raw_text: str = ""
        self.source_file: str = ""
        self.confidence: float = 0.0
        # 从图片中提取的电表记录（电费单等）
        self.meter_records: list[dict] = []

    def has_price_data(self) -> bool:
        return any(v is not None for v in [
            self.sharp_peak_price, self.peak_price, self.flat_price, self.valley_price,
            self.average_price,
        ])

    def has_any_data(self) -> bool:
        return self.has_price_data() or bool(self.user_id) or bool(self.meter_records)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "sharp_peak_price": self.sharp_peak_price,
            "peak_price": self.peak_price,
            "flat_price": self.flat_price,
            "valley_price": self.valley_price,
            "average_price": self.average_price,
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
        self._unavailable = False
        self._warned = False

    def _init_engine(self):
        """延迟初始化 OCR 引擎。"""
        if self._engine is not None or self._unavailable:
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
                return

        if self.engine_type == "tesseract":
            try:
                import pytesseract
                import shutil
                # 显式查找 tesseract 路径，避免 Flask 等服务环境 PATH 不完整
                tess_path = shutil.which("tesseract")
                if not tess_path:
                    # 常见安装路径回退
                    for p in ["/usr/bin/tesseract", "/usr/local/bin/tesseract"]:
                        if os.path.isfile(p):
                            tess_path = p
                            break
                if tess_path:
                    pytesseract.pytesseract.tesseract_cmd = tess_path
                pytesseract.get_tesseract_version()
                self._engine = pytesseract
                log.info("Tesseract OCR 引擎初始化成功 (路径: %s)", tess_path or "default")
            except Exception as e:
                log.warning("Tesseract 不可用: %s。图片 OCR 功能将被跳过。", e)
                self._unavailable = True

    def _preprocess_image(self, filepath: str):
        """图片预处理：灰度化 + 对比度增强 + 自适应二值化，提升 OCR 准确率。"""
        from PIL import Image, ImageEnhance, ImageFilter

        img = Image.open(filepath)

        # 转灰度
        if img.mode != "L":
            img = img.convert("L")

        # 对比度增强 (1.5x)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.5)

        # 锐化
        img = img.filter(ImageFilter.SHARPEN)

        # 放大小图片（宽度 < 1000px 时放大2倍）
        w, h = img.size
        if w < 1000:
            img = img.resize((w * 2, h * 2), Image.LANCZOS)

        return img

    def extract_from_image(self, filepath: str, source_info: dict = None) -> OCRResult:
        """从单张图片提取单价和用户编号。"""
        self._init_engine()

        if self._unavailable:
            if not self._warned:
                log.warning("无可用 OCR 引擎（PaddleOCR/Tesseract 均未安装），跳过所有图片处理")
                self._warned = True
            result = OCRResult()
            result.source_file = Path(filepath).name
            return result

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

            # 提取用户编号（OCR文本 + 文件名双重匹配）
            result.user_id = self._extract_user_id(raw_text)
            if not result.user_id:
                result.user_id = self._extract_user_id_from_filename(filepath.name)

            # 提取单价（多级策略）
            prices = self._extract_prices(raw_text)
            result.sharp_peak_price = prices.get("sharp_peak_price")
            result.peak_price = prices.get("peak_price")
            result.flat_price = prices.get("flat_price")
            result.valley_price = prices.get("valley_price")
            result.average_price = prices.get("average_price")

            # 推断月份
            result.reading_month = self._infer_month(raw_text, source_info, filepath.name)

            # 从图片文本中提取电表记录（电费单图片等）
            result.meter_records = self._extract_meter_records(
                raw_text, filepath, source_info, result
            )

            if result.user_id or result.has_price_data():
                log.info("  OCR 提取 [%s]: 用户=%s, 月份=%s, "
                         "尖峰=%.4f, 峰=%.4f, 平=%.4f, 谷=%.4f, 均价=%.4f, 电表记录=%d",
                         filepath.name, result.user_id or "未识别",
                         result.reading_month or "未知",
                         result.sharp_peak_price or 0,
                         result.peak_price or 0,
                         result.flat_price or 0,
                         result.valley_price or 0,
                         result.average_price or 0,
                         len(result.meter_records))
            else:
                log.warning("  OCR [%s]: 未提取到用户编号或单价，原文前300字: %s",
                            filepath.name, raw_text[:300].replace('\n', ' | '))

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
            # 使用预处理后的图片
            img = self._preprocess_image(filepath)
            # 使用 PSM 6（假设为均匀的文本块）对表格类图片效果更好
            custom_config = r'--oem 3 --psm 6'
            text = self._engine.image_to_string(img, lang=self.tesseract_lang,
                                                 config=custom_config)
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

        # 增强匹配：各种常见标签格式
        extra_patterns = [
            r'(?:用户编号|用户号|户号|客户编号|用户编码|客户号)\s*[:：\s]\s*(\d{6,20})',
            r'(?:编号|No\.?|NO\.?)\s*[:：\s]\s*(\d{8,20})',
            r'户\s*号\s*[:：\s]\s*(\d{6,20})',
        ]
        for pattern in extra_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()

        # 兜底：查找连续数字串（8-20位），只取唯一一个
        numbers = re.findall(r'\b(\d{8,20})\b', text)
        if len(numbers) == 1:
            return numbers[0]

        return None

    def _extract_user_id_from_filename(self, filename: str) -> Optional[str]:
        """从文件名提取用户编号（文件名可能包含用户编号）。"""
        # 匹配8-16位纯数字
        matches = re.findall(r'(\d{8,16})', filename)
        for m in matches:
            # 排除日期/时间戳格式：
            # - 8位日期 (20240115)
            # - 14位时间戳 (20260227093114)
            # - 以及介于两者之间的长度
            if re.match(r'^20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', m):
                continue
            return m
        return None

    def _extract_meter_records(self, text: str, filepath: Path,
                               source_info: dict, ocr_result) -> list[dict]:
        """从 OCR 文本中提取电表记录（电费单、抄表单等图片）。"""
        from src.parsers.validators import validate_record

        records = []
        if not text or len(text) < 20:
            return records

        field_mapping = config.get("field_mapping") or {}

        # 提取电表号
        meter_numbers = []
        for alias in field_mapping.get("meter_number", []):
            for m in re.finditer(rf'{re.escape(alias)}\s*[:：]?\s*(\d{{6,20}})', text):
                mn = m.group(1)
                if mn not in meter_numbers:
                    meter_numbers.append(mn)

        # 提取资产号
        asset_numbers = []
        for alias in field_mapping.get("asset_number", []):
            for m in re.finditer(rf'{re.escape(alias)}\s*[:：]?\s*([A-Za-z0-9]{{8,30}})', text):
                an = m.group(1)
                if an not in asset_numbers:
                    asset_numbers.append(an)

        # 没有电表号时用资产号
        if not meter_numbers and asset_numbers:
            meter_numbers = asset_numbers

        # 提取读数
        sharp_peak = self._extract_reading_value(text, ["尖峰?", "尖\\s*[:：]"])
        peak = self._extract_reading_value(text, ["(?<!尖)峰\\s*[:：]", "峰段"])
        flat = self._extract_reading_value(text, ["平\\s*[:：]", "平段"])
        valley = self._extract_reading_value(text, ["谷\\s*[:：]", "谷段"])
        total = self._extract_reading_value(text, ["总电量", "总用电", "合计.*电量", "总\\s*[:：]"])

        # 提取项目名
        project = None
        try:
            import re as _re
            for t in [text[:1000], filepath.name]:
                m = _re.search(r'([\u4e00-\u9fff]{2,10}(?:项目|电站|光伏))', t)
                if m:
                    project = m.group(1)
                    break
        except Exception:
            pass

        for mn in meter_numbers:
            record = validate_record({
                "meter_number": mn,
                "asset_number": None,
                "user_id": ocr_result.user_id,
                "meter_type": "未知",
                "multiplier": 1.0,
                "project_name": project,
                "reading_month": ocr_result.reading_month or "unknown",
                "sharp_peak": sharp_peak,
                "peak": peak,
                "flat": flat,
                "valley": valley,
                "total_kwh": total,
                "source_file": filepath.name,
                "source_sheet": "OCR",
            })
            if record:
                records.append(record)

        return records

    def _extract_reading_value(self, text: str, patterns: list[str]) -> Optional[float]:
        """从文本中按模式提取电量读数。"""
        for pattern in patterns:
            m = re.search(rf'{pattern}\s*(\d+\.?\d*)', text)
            if m:
                try:
                    val = float(m.group(1))
                    if val > 0:
                        return val
                except ValueError:
                    pass
        return None

    def _extract_prices(self, text: str) -> dict:
        """从电费账单文本中提取单价。

        先识别账单类型（参照《电费单价收取方式》五种），再按对应方式计算。

        第1种: 电能量电费+输配电费+系统运行费+基金附加费 → 组件求和
               (账单中各组件分时段列出，需要逐项求和得到各时段总单价)
        第2种: 电能电费+输配电费+上网环节线损电费+系统运行费+基金附加费 → 组件求和
        第3种: 尖期电量电费=尖峰期单价 → 各时段电费行直接含单价列
        第4种: 电量电费=尖=峰=平=谷 → 单一电价，所有时段统一
        第5种: (电费1+电费2)/2 → 同一时段两行取平均
        """
        # ---- Step 1: 检测账单类型 ----
        bill_type = self._detect_bill_type(text)
        log.info("  账单类型检测: 第%s种", bill_type)

        prices = {}

        # ---- Step 2: 按类型提取 ----
        if bill_type == 5:
            # 第5种: 两行同类取平均
            prices = self._extract_prices_two_average(text)

        elif bill_type in (1, 2):
            # 第1/2种: 多组件明细行求和
            prices = self._extract_prices_industrial_components(text)

        elif bill_type == 3:
            # 第3种: 各时段电费行直接含单价列
            prices = self._extract_prices_charge_table(text)

        elif bill_type == 4:
            # 第4种: 单一电价，所有时段统一
            single = self._extract_single_price(text)
            if single is not None:
                prices = {
                    "sharp_peak_price": single,
                    "peak_price": single,
                    "flat_price": single,
                    "valley_price": single,
                    "average_price": single,
                }

        # ---- Step 3: 若主策略不足，尝试后续策略 ----
        if not self._has_enough_prices(prices):
            # 尝试表格行级匹配
            fallback = self._extract_prices_table_row(text)
            if self._has_enough_prices(fallback):
                prices = fallback

        if not self._has_enough_prices(prices):
            # 配置文件标签匹配
            fallback = self._extract_prices_label_match(text)
            if self._has_enough_prices(fallback):
                prices = fallback

        if not self._has_enough_prices(prices):
            # 通用模式（连续4个价格数字）
            rules = self.extraction_rules.get("unit_price", {})
            general_patterns = rules.get("patterns", [])
            for pattern in general_patterns:
                matches = re.findall(pattern, text)
                if len(matches) >= 4:
                    try:
                        candidates = [float(m) for m in matches[:4]]
                        if all(self._is_valid_price(v) for v in candidates):
                            prices["sharp_peak_price"] = candidates[0]
                            prices["peak_price"] = candidates[1]
                            prices["flat_price"] = candidates[2]
                            prices["valley_price"] = candidates[3]
                    except (ValueError, IndexError):
                        pass
                    break

        if not self._has_enough_prices(prices):
            # 单一电价兜底（第4种）
            single = self._extract_single_price(text)
            if single is not None:
                prices = {
                    "sharp_peak_price": single,
                    "peak_price": single,
                    "flat_price": single,
                    "valley_price": single,
                    "average_price": single,
                }

        if not self._has_enough_prices(prices):
            # 最终兜底：所有像价格的数字（降序 → 尖 > 峰 > 平 > 谷）
            price_candidates = re.findall(r'(\d\.\d{2,8})', text)
            valid = sorted(set(
                float(p) for p in price_candidates if self._is_valid_price(float(p))
            ), reverse=True)
            if len(valid) >= 4:
                prices["sharp_peak_price"] = valid[0]
                prices["peak_price"] = valid[1]
                prices["flat_price"] = valid[2]
                prices["valley_price"] = valid[3]

        # 补充平均电价
        if "average_price" not in prices:
            avg = self._extract_average_price(text)
            if avg is not None:
                prices["average_price"] = avg

        return prices

    def _detect_bill_type(self, text: str) -> int:
        """检测电费账单类型（1-5），返回类型编号。

        检测特征（按优先级从高到低）：
        第5种: 同一时段出现 "电费1"/"电费2" 两行且单价不同 → 取平均
        第2种: 含上网环节线损电费 + 其他组件 → 组件求和
        第1种: 含≥3种不同组件明细行（电能/输配/系统运行/基金） → 组件求和
        第3种: 含 "X期电量电费"/"X期电费" 行 → 各时段直接读单价
        第4种: 只有 "电量电费"（无尖峰平谷标记）→ 单一电价=所有时段
        """
        lines = text.split("\n")

        # 统计各种特征
        has_period_fee_12 = False      # "电费1"/"电费2"
        has_period_charge_rows = False  # "尖期电量电费"/"峰期电费" 等
        has_single_charge = False      # "电量电费"（无时段前缀）
        component_types = set()        # 不同的组件类型

        component_groups = {
            "电能": ["电度电费", "电能电费", "电能量电费", "电脑电费"],
            "输配": ["输配电费", "输配电"],
            "线损": ["上网环节", "环节线损"],
            "运行": ["系统运行", "运行费用"],
            "基金": ["基金及附加", "基金附加"],
            "分摊": ["市场化分摊", "分摊费用"],
        }

        period_markers = ["尖", "峰", "平", "谷"]

        # 收集"电费1"/"电费2"行的单价，用于区分第1种和第5种
        fee1_prices = {}  # period -> price from 电费1
        fee2_prices = {}  # period -> price from 电费2

        for line in lines:
            lc = line.strip()
            if not lc:
                continue

            # 检查"电费1"/"电费2"标记，并记录其中的单价
            m12 = re.search(r'([尖峰平谷])\s*期?\s*电费\s*([12])', lc)
            if m12:
                has_period_fee_12 = True
                period_char = m12.group(1)
                fee_num = m12.group(2)
                # 提取行中的单价
                nums = re.findall(r'(\d+\.\d{2,8})', lc)
                price_val = None
                for n in nums:
                    v = float(n)
                    if self._is_valid_price(v):
                        price_val = v
                        break
                if price_val is not None:
                    target = fee1_prices if fee_num == "1" else fee2_prices
                    target[period_char] = price_val

            # 检查分时段电费行（"尖期电量电费" 等）
            if re.search(r'[尖峰平谷]\s*期?\s*(?:电量)?电费', lc):
                has_period_charge_rows = True

            # 检查无时段前缀的"电量电费"
            if re.search(r'^(?:.*\s)?电量电费(?:\s|$)', lc) and \
               not any(m in lc for m in period_markers):
                has_single_charge = True

            # 统计组件类型
            for group, keywords in component_groups.items():
                for kw in keywords:
                    if kw in lc:
                        # 确认是带时段标记的组件行
                        if any(m in lc for m in period_markers):
                            component_types.add(group)
                        break

        # 判定类型
        # 第5种：电费1和电费2都有不同的非零单价 → 取平均
        if has_period_fee_12:
            # 检查是否真的有两套不同单价
            common_periods = set(fee1_prices.keys()) & set(fee2_prices.keys())
            has_diff_prices = any(
                fee1_prices[p] != fee2_prices[p]
                for p in common_periods
            ) if common_periods else False
            if has_diff_prices:
                return 5  # 第5种：两行取平均
            # 否则当作第1种（电费1/电费2只是标签不同，实际单价列可直接读）

        if "线损" in component_types and len(component_types) >= 2:
            return 2  # 第2种：电能+输配+上网环节线损+系统运行+基金 → 组件求和

        if len(component_types) >= 3:
            return 1  # 第1种：电能量+输配+系统运行+基金附加 → 组件求和

        if has_single_charge:
            return 4  # 第4种：电量电费=所有时段统一单价

        if has_period_charge_rows:
            return 3  # 第3种：各时段电费行直接含单价

        # 默认按第3种处理（最通用，直接读单价列）
        return 3

    def _has_enough_prices(self, prices: dict) -> bool:
        """至少有2个分时段价格或有平均电价即认为足够。"""
        period_count = sum(1 for k in ["sharp_peak_price", "peak_price", "flat_price", "valley_price"]
                          if prices.get(k) is not None)
        return period_count >= 2 or prices.get("average_price") is not None

    def _extract_prices_charge_table(self, text: str) -> dict:
        """从「电费信息 Charge Information」表格中提取分时段单价。

        匹配格式（南方电网工业分时）：
          格式A: 尖期电量电费  0       0         0
                 峰期电量电费  5725    0.95786875  5483.81
          格式B: 电度电费(尖)  0       0         0
                 电度电费(峰)  17550   0.62779000  11017.72
          格式C: 电脑电费(峰)  17550   0.62779000  11017.72  (OCR误读)

        每行: 标签  计费电量  单价  金额 — 单价是第2个小数（8位精度）
        """
        prices = {}
        lines = text.split("\n")

        # X期电量电费 行匹配 + 电度电费(X) / 电脑电费(X) 括号格式
        period_map = {
            "sharp_peak_price": [r'尖\s*期?\s*电量电费', r'尖\s*期?\s*电[量费]',
                                 r'电[度脑]\s*电费\s*[(\(]\s*尖'],
            "peak_price":       [r'(?<!尖)\s*峰\s*期?\s*电量电费', r'(?<!尖)\s*峰\s*期?\s*电[量费]',
                                 r'电[度脑]\s*电费\s*[(\(]\s*峰'],
            "flat_price":       [r'平\s*期?\s*电量电费', r'平\s*期?\s*电[量费]',
                                 r'电[度脑]\s*电费\s*[(\(]\s*平'],
            "valley_price":     [r'谷\s*期?\s*电量电费', r'谷\s*期?\s*电[量费]',
                                 r'电[度脑]\s*电费\s*[(\(]\s*谷'],
        }

        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue

            for field, patterns in period_map.items():
                if field in prices:
                    continue
                for pat in patterns:
                    if re.search(pat, line_clean):
                        # 特殊处理：peak 行不能含"尖"
                        if field == "peak_price" and "尖" in line_clean:
                            continue
                        # 提取该行所有数字
                        nums = re.findall(r'(\d+\.?\d*)', line_clean)
                        float_nums = []
                        for n in nums:
                            try:
                                float_nums.append(float(n))
                            except ValueError:
                                pass
                        # 单价通常是有多位小数的数字（区别于电量和金额的整数/2位小数）
                        price_candidates = [v for v in float_nums if self._is_valid_price(v)]
                        # 优先选小数位数最多的（单价精度高于电量和金额）
                        if price_candidates:
                            best = max(price_candidates,
                                       key=lambda v: len(str(v).split('.')[-1]) if '.' in str(v) else 0)
                            prices[field] = best
                        break

        return prices

    def _extract_prices_industrial_components(self, text: str) -> dict:
        """大工业用电：从组件费用行提取并求和各时段单价。

        大工业电费账单包含多个组件行，每个组件分别列出尖/峰/平/谷的单价。
        总单价 = 各组件单价之和。

        重要：账单原始标签是正确的 (尖)(峰)(平)(谷)，但 OCR 可能把
        "(平)" 误识别为 "(峰)"，导致出现两个"峰"标签。
        修正策略：每个组件的行按出现顺序固定映射为 尖→峰→平→谷，
        不依赖 OCR 读取的标签文字，以避免 OCR 误识别导致错误。
        """
        lines = text.split("\n")

        # 组件行关键字（匹配所有可能的费用组件）
        component_keywords = [
            "电度电费", "电脑电费", "电能电费", "电能量电费", "电量电费",
            "输配电费", "输配电",
            "上网环节", "环节线损",
            "系统运行", "运行费用",
            "基金及附加", "基金附加", "附加费",
            "市场化分摊", "分摊费用",
        ]

        # 不分时段的固定费用（基金及附加费等，加到每个时段）
        flat_fee_keywords = ["基金及附加", "基金附加"]
        flat_fee_price = None

        # 按组件类型分组收集行数据：component_type -> [(ocr_label, price), ...]
        # 保持行出现顺序
        component_groups: dict[str, list[tuple[str, float]]] = {}

        # 用于识别组件类型
        component_type_map = {
            "电能": ["电度电费", "电脑电费", "电能电费", "电能量电费", "电量电费"],
            "输配": ["输配电费", "输配电"],
            "线损": ["上网环节", "环节线损"],
            "运行": ["系统运行", "运行费用"],
            "分摊": ["市场化分摊", "分摊费用"],
        }

        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue

            # 必须包含至少一个组件关键字
            if not any(kw in line_clean for kw in component_keywords):
                continue

            # 检查是否是不分时段的固定费用行（基金附加费，无时段标记）
            is_flat_fee = any(kw in line_clean for kw in flat_fee_keywords)
            has_period_marker = bool(re.search(r'[(\(]\s*[尖峰平谷]\s*[)\)]', line_clean)) or \
                                bool(re.search(r'[尖峰平谷]\s*期', line_clean))

            if is_flat_fee and not has_period_marker:
                nums = re.findall(r'(\d+\.\d{2,8})', line_clean)
                for n in nums:
                    val = float(n)
                    if 0.001 <= val <= 1.0:
                        flat_fee_price = val
                        break
                continue

            if not has_period_marker:
                continue

            # 识别组件类型
            comp_type = None
            for ct, keywords in component_type_map.items():
                if any(kw in line_clean for kw in keywords):
                    comp_type = ct
                    break
            if not comp_type:
                continue

            # 识别时段标签（取括号内或"X期"的字符）
            period_label = None
            pm = re.search(r'[(\(]\s*([尖峰平谷])\s*[)\)]', line_clean)
            if pm:
                period_label = pm.group(1)
            else:
                pm = re.search(r'([尖峰平谷])\s*期', line_clean)
                if pm:
                    period_label = pm.group(1)

            if not period_label:
                continue

            # 提取该行中的单价
            nums = re.findall(r'(\d+\.\d{2,8})', line_clean)
            price_val = None
            for n in nums:
                val = float(n)
                if 0.001 <= val <= 3.0:
                    price_val = val
                    break

            # 记录所有行（含价格为 None 的），保持出现顺序
            component_groups.setdefault(comp_type, []).append(
                (period_label, price_val)
            )

        # ---- 按行顺序分配时段 ----
        # 标准顺序：尖(0)→峰(1)→平(2)→谷(3)
        # OCR 可能把 (平) 误读为 (峰)，所以不完全信任标签，
        # 而是结合标签和出现顺序来决定时段。
        positional_order = ["sharp_peak_price", "peak_price", "flat_price", "valley_price"]

        period_prices = {
            "sharp_peak_price": [],
            "peak_price": [],
            "flat_price": [],
            "valley_price": [],
        }

        label_to_field = {
            "尖": "sharp_peak_price",
            "峰": "peak_price",
            "平": "flat_price",
            "谷": "valley_price",
        }

        for comp_type, rows in component_groups.items():
            # 检查该组件是否存在标签异常（如两个"峰"缺少"平"）
            labels_in_group = [label for label, _ in rows]
            label_set = set(labels_in_group)
            has_all_four = {"尖", "峰", "平", "谷"}.issubset(label_set)
            has_dup_labels = len(labels_in_group) != len(label_set)

            if has_all_four and not has_dup_labels:
                # 标签完整无重复 → 直接按标签映射
                for label, price in rows:
                    if price is None:
                        continue
                    field = label_to_field.get(label)
                    if field:
                        period_prices[field].append(price)
            elif len(rows) == 4 and has_dup_labels:
                # 有重复标签（OCR 误读）→ 按位置顺序映射
                log.info("  组件[%s] OCR标签异常 %s，按行顺序映射为尖→峰→平→谷",
                         comp_type, labels_in_group)
                for idx, (label, price) in enumerate(rows):
                    if price is None:
                        continue
                    if idx < len(positional_order):
                        period_prices[positional_order[idx]].append(price)
            elif len(rows) == 3 and "尖" not in label_set:
                # 只有3行（无尖）→ 映射为峰/平/谷
                for idx, (label, price) in enumerate(rows):
                    if price is None:
                        continue
                    field_idx = idx + 1  # 跳过尖
                    if field_idx < len(positional_order):
                        period_prices[positional_order[field_idx]].append(price)
            else:
                # 其他情况：尽量按标签映射，遇到重复则按位置
                seen_fields = set()
                for idx, (label, price) in enumerate(rows):
                    if price is None:
                        continue
                    field = label_to_field.get(label)
                    if field and field not in seen_fields:
                        period_prices[field].append(price)
                        seen_fields.add(field)
                    elif idx < len(positional_order):
                        # 标签已用过或无法识别 → 按位置
                        fallback_field = positional_order[idx]
                        if fallback_field not in seen_fields:
                            period_prices[fallback_field].append(price)
                            seen_fields.add(fallback_field)

        # 求和得到各时段总单价（加上固定费用）
        prices = {}
        for field, components in period_prices.items():
            if components:
                total = sum(components)
                if flat_fee_price is not None:
                    total += flat_fee_price
                if self._is_valid_price(total):
                    prices[field] = round(total, 8)

        return prices

    def _extract_prices_two_average(self, text: str) -> dict:
        """第5种计价方式：(电费1 + 电费2) / 2 = 各时段单价。

        账单中同一时段出现两行电费（如 尖期电费1、尖期电费2），
        分别含不同单价，取平均值作为该时段最终单价。
        """
        lines = text.split("\n")

        period_map = {
            "sharp_peak_price": r'尖',
            "peak_price": r'(?<!尖)峰',
            "flat_price": r'平',
            "valley_price": r'谷',
        }

        # 收集每个时段的所有单价
        period_prices: dict[str, list[float]] = {k: [] for k in period_map}

        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue

            # 匹配含"电费"且含时段标记的行
            if "电费" not in line_clean:
                continue

            for field, pat in period_map.items():
                if re.search(pat, line_clean):
                    # 排除 peak 行含 "尖"
                    if field == "peak_price" and "尖" in line_clean:
                        continue
                    # 提取单价（高精度小数）
                    nums = re.findall(r'(\d+\.\d{2,8})', line_clean)
                    for n in nums:
                        val = float(n)
                        if self._is_valid_price(val):
                            period_prices[field].append(val)
                            break
                    break

        # 检查是否有时段出现了恰好 2 个不同单价 → 取平均
        prices = {}
        has_multi = any(len(v) >= 2 for v in period_prices.values())
        if has_multi:
            for field, vals in period_prices.items():
                if len(vals) >= 2:
                    prices[field] = round(sum(vals) / len(vals), 8)
                elif len(vals) == 1:
                    prices[field] = vals[0]

        return prices

    def _extract_prices_table_row(self, text: str) -> dict:
        """从表格行结构中提取价格（每行一个时段）。

        支持多种表格格式：
        - 每行一个时段: "尖峰 1.2345" 或 "尖 0.95786875"
        - 合并行: "尖峰1.2345峰0.9876平0.5678谷0.3210"
        - 表格列标题行 + 数据行
        """
        prices = {}
        lines = text.split("\n")

        field_map = {
            "sharp_peak_price": ["尖峰", "尖"],
            "peak_price": ["峰"],
            "flat_price": ["平"],
            "valley_price": ["谷"],
        }

        # 费用组件行关键字（大工业明细行，不是完整时段单价）
        _fee_component_keywords = [
            "电输电费", "输配电费", "上网环节", "系统运行", "线损",
            "力调电费", "环节线损", "运行费用", "基金及附加",
            "输配电", "配电费", "市场化分摊",
        ]

        # 策略A：逐行匹配
        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue

            # 跳过费用组件行（大工业明细，单价是组件价非总价）
            if any(kw in line_clean for kw in _fee_component_keywords):
                continue

            for field, keywords in field_map.items():
                if field in prices:
                    continue
                for kw in keywords:
                    if kw in line_clean:
                        if field == "peak_price" and "尖" in line_clean:
                            continue
                        nums = re.findall(r'(\d+\.\d{2,8})', line_clean)
                        for n in nums:
                            val = float(n)
                            if self._is_valid_price(val):
                                prices[field] = val
                                break
                        break

        if len(prices) >= 3:
            return prices

        # 策略B：单行内连续出现"尖峰X.XXXX峰X.XXXX平X.XXXX谷X.XXXX"
        full_text = text.replace("\n", " ")
        m = re.search(
            r'尖峰?\s*[:：]?\s*(\d+\.\d{2,8})\s*[元/度kWh]*\s*'
            r'(?:(?!尖)峰)\s*[:：]?\s*(\d+\.\d{2,8})\s*[元/度kWh]*\s*'
            r'平\s*[:：]?\s*(\d+\.\d{2,8})\s*[元/度kWh]*\s*'
            r'谷\s*[:：]?\s*(\d+\.\d{2,8})',
            full_text
        )
        if m:
            vals = [float(m.group(i)) for i in range(1, 5)]
            if all(self._is_valid_price(v) for v in vals):
                return {
                    "sharp_peak_price": vals[0],
                    "peak_price": vals[1],
                    "flat_price": vals[2],
                    "valley_price": vals[3],
                }

        # 策略C：表格列标题行 + 数据行（标题和值在相邻行）
        for i, line in enumerate(lines):
            line_clean = line.strip()
            kw_hits = sum(1 for kw in ["尖", "峰", "平", "谷"] if kw in line_clean)
            if kw_hits >= 3 and i + 1 < len(lines):
                data_line = lines[i + 1].strip()
                nums = re.findall(r'(\d+\.\d{2,8})', data_line)
                valid = [float(n) for n in nums if self._is_valid_price(float(n))]
                if len(valid) >= 3:
                    positions = []
                    for field, kws in field_map.items():
                        for kw in kws:
                            pos = line_clean.find(kw)
                            if pos >= 0:
                                if field == "peak_price" and "尖" in line_clean[:pos+1]:
                                    continue
                                positions.append((pos, field))
                                break
                    positions.sort()
                    for idx, (_, field) in enumerate(positions):
                        if idx < len(valid):
                            prices[field] = valid[idx]
                    if len(prices) >= 3:
                        return prices

        return prices

    def _extract_prices_label_match(self, text: str) -> dict:
        """配置文件正则 + 增强标签匹配。"""
        prices = {}
        rules = self.extraction_rules.get("unit_price", {})

        # 配置文件中的专用标签
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
                        val = float(match.group(1))
                        if self._is_valid_price(val):
                            prices[field] = val
                    except (ValueError, IndexError):
                        pass
                    break

        if len(prices) >= 3:
            return prices

        # 增强模式（处理 OCR 噪声、8位小数）
        enhanced_patterns = {
            "sharp_peak_price": [
                r'尖\s*峰?\s*[:：\s价单]*\s*(\d+\.?\d{2,8})',
                r'尖\s*[:：]\s*(\d+\.\d+)',
            ],
            "peak_price": [
                r'(?<!尖)\s*峰\s*[:：\s价单]*\s*(\d+\.?\d{2,8})',
                r'(?<![尖a-zA-Z])峰\s*[:：]\s*(\d+\.\d+)',
            ],
            "flat_price": [
                r'平\s*[:：\s价单]*\s*(\d+\.?\d{2,8})',
                r'平\s*段?\s*[:：]\s*(\d+\.\d+)',
            ],
            "valley_price": [
                r'谷\s*[:：\s价单]*\s*(\d+\.?\d{2,8})',
                r'谷\s*段?\s*[:：]\s*(\d+\.\d+)',
            ],
        }
        for field, patterns in enhanced_patterns.items():
            if field in prices:
                continue
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    try:
                        val = float(match.group(1))
                        if self._is_valid_price(val):
                            prices[field] = val
                            break
                    except (ValueError, IndexError):
                        pass

        return prices

    def _extract_average_price(self, text: str) -> Optional[float]:
        """提取平均电价 / 综合电价。

        匹配格式：
          平均电价  0.76133230
          平均电价: 0.76133230 (元/千瓦时)
          平均电价  0.69986875
        """
        patterns = [
            r'平均电价\s*[:：]?\s*(\d+\.\d{2,8})',
            r'(?:综合|平均)\s*(?:电价|单价)\s*[:：]?\s*(\d+\.\d{2,8})',
            r'平均电价\s*[:：]?\s*(\d+\.\d+)\s*(?:\(|（|元)',
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                try:
                    val = float(m.group(1))
                    if self._is_valid_price(val):
                        return val
                except (ValueError, IndexError):
                    pass
        return None

    def _extract_single_price(self, text: str) -> Optional[float]:
        """提取单一电价（居民合表等无分时段账单）。

        匹配格式：
          电量电费  6066.6  0.69986875  4245.82
          → 单价是第2个带多位小数的数字
        """
        lines = text.split("\n")
        for line in lines:
            line_clean = line.strip()
            # 匹配"电量电费"但不含"尖/峰/平/谷"前缀
            if "电量电费" not in line_clean:
                continue
            if any(kw in line_clean for kw in ["尖", "峰", "平", "谷"]):
                continue
            # 这行是单一电价行，提取单价（多位小数的那个数字）
            nums = re.findall(r'(\d+\.\d{2,8})', line_clean)
            # 单价通常在 0.1~3.0 之间，选精度最高的
            candidates = []
            for n in nums:
                val = float(n)
                if self._is_valid_price(val):
                    candidates.append((len(n.split('.')[-1]), val))
            if candidates:
                candidates.sort(reverse=True)  # 小数位数最多的优先
                return candidates[0][1]
        return None

    def _is_valid_price(self, val: float) -> bool:
        """判断是否为合理的电价（元/kWh）。"""
        return 0.1 <= val <= 3.0

    def _infer_month(self, text: str, source_info: dict, filename: str) -> Optional[str]:
        """推断月份。"""
        # 从 OCR 文本中提取
        month_patterns = [
            r'(\d{4})[-/年](\d{1,2})[-/月]',
            r'(\d{4})(\d{2})(?:月|期)',
            r'(\d{4})[-/.](\d{1,2})',
            r'(\d{4})\s*年\s*(\d{1,2})\s*月',
        ]
        for pattern in month_patterns:
            match = re.search(pattern, text)
            if match:
                year, month = int(match.group(1)), int(match.group(2))
                if 2015 <= year <= 2035 and 1 <= month <= 12:
                    return f"{year}-{str(month).zfill(2)}"

        # 从文件名推断
        for match in re.finditer(r'(\d{4})[-_年.]?(\d{1,2})', filename):
            year, month = int(match.group(1)), int(match.group(2))
            if 2015 <= year <= 2035 and 1 <= month <= 12:
                return f"{year}-{str(month).zfill(2)}"

        # 从邮件日期
        if source_info and source_info.get("email_date"):
            d = source_info["email_date"]
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m")

        return None
