"""OCR processing service for invoice images using PaddleOCR."""

import re
import logging
import threading
from io import BytesIO
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from PIL import Image, ImageOps
import numpy as np

from app.services.item_layout import extract_item_lines, strip_internal

logger = logging.getLogger(__name__)

# Geometry of a photographed (not scanned) Chinese VAT invoice. PaddleOCR
# reads the encrypted column and both margin banners and splices them into the
# party blocks, where they pollute item_name (their strings contain "*") and
# block the 纳税人识别号 pattern from ever seeing the real value. All values
# are fractions of image width / height so they survive resizing.
#
# These live at module level on purpose: a name defined in the class body is
# not in scope inside the methods, so `_CIPHER_LEFT = 0.55` written inside the
# class would be silently shadowed by the module-level 0.52 below.
_CIPHER_TOP = 0.15             # encrypted column starts here
_CIPHER_BOTTOM = 0.45          # ... and ends here
_CIPHER_LEFT = 0.52            # ... and starts this far right
_CIPHER_SYMBOLS = set("+*/-<>")
_CIPHER_MIN_WIDTH = 0.08       # narrower than this cannot be the column
# Vertical banner: spans at least this share of the page height while staying
# narrower than _BANNER_MAX_WIDTH, and hugs one of the two outer margins.
_BANNER_MIN_HEIGHT = 0.25
_BANNER_MAX_WIDTH = 0.12
_BANNER_LEFT = 0.15
_BANNER_RIGHT = 0.90

# Reading order: two OCR lines belong to the same visual row when their
# vertical boxes overlap by more than this share of the shorter box.
_ROW_OVERLAP_RATIO = 0.45
# A merged row may not grow taller than this multiple of the shorter of the
# two boxes merging. Chinese invoices use tall overlapping label boxes
# (名称： y212-272 vs 纳税人识别号： y251-296); without this cap every label in
# a party block chains together into a single "row" and the layout is lost.
_MAX_ROW_HEIGHT_FACTOR = 3.0

# Thread lock for singleton initialization
_ocr_lock = threading.Lock()
_extractor_lock = threading.Lock()


def normalize_tax_rate(value: Any) -> Optional[str]:
    """把税率归一成票面写法 —— 数字一律带上百分号。

    税率是页面上唯一「有没有百分号都算同一个值」的字段，而 OCR 恰好有两条
    路径会把「%」丢掉：

    1. ``PATTERNS_CN['tax_rate']`` 写成 ``(\\d+%?|免税)``，百分号是可选的。
       OCR 把「3%」读成「3」时正则照样匹配，照样把「3」交出来；
    2. ``_reconcile_totals`` 从 税额/金额 反推税率时写的是 ``str(int(hit))``，
       这条路径从来没有百分号。

    后果不是「少个符号不好看」这么轻：``_values_are_equal`` 对 tax_rate 走
    字符串比较，「11」与 LLM 给的「11%」不等 -> 判成 conflict -> 每张票都要
    在比对表里手点一次「选OCR / 选LLM」，而这两者本来就是同一个税率。

    归一化因此放在 ``extract_fields`` 的出口，而不是只改正则：比较、入库、
    明细行必须共用同一个写法，否则这头补上、那头还漏。

    非数字写法（``免税`` / ``不征税`` / ``***``）原样返回；``0.11`` 这类小数
    写法按百分数理解 —— 票面印的是 11%，写 0.11 只是同一个数的另一种写法。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # 全角百分号、以及数字和百分号之间的空格：OCR 会把「1 1 %」拆成三段
    text = text.replace("％", "%").replace(" ", "").replace("\u3000", "")
    digits = text.rstrip("%")
    try:
        number = Decimal(digits)
    except InvalidOperation:
        return text
    if not number.is_finite():
        return text

    if Decimal(0) < number < Decimal(1):
        number *= 100
    # normalize() 会把 100 变成 1E+2，所以用 :f 输出成普通记数法
    return f"{number.normalize():f}%"


class OCRService:
    """Handles OCR processing for invoice images using PaddleOCR."""

    def __init__(self):
        self._ocr = None
        self._init_lock = threading.Lock()

    @property
    def ocr(self):
        """Lazy-load PaddleOCR instance (thread-safe)."""
        if self._ocr is None:
            with self._init_lock:
                # Double-check after acquiring lock
                if self._ocr is None:
                    from paddleocr import PaddleOCR
                    import logging as log_module
                    # Suppress PaddleOCR logging
                    log_module.getLogger('ppocr').setLevel(log_module.WARNING)
                    log_module.getLogger('paddlex').setLevel(log_module.WARNING)
                    logger.info("Initializing PaddleOCR...")
                    self._ocr = PaddleOCR(
                        use_angle_cls=True,
                        lang='ch',
                    )
                    logger.info("PaddleOCR initialized successfully")
        return self._ocr

    def _extract_text_from_result(self, result) -> Tuple[List[str], List[float], List[Dict[str, Any]]]:
        """Extract text, confidences, and line metadata from OCR result (v2.x API)."""
        text_lines: List[str] = []
        confidences: List[float] = []
        line_items: List[Dict[str, Any]] = []

        if not result:
            return text_lines, confidences, line_items

        # PaddleOCR v2.x returns list of [bbox, (text, confidence)]
        for item in result:
            if isinstance(item, list) and len(item) >= 2:
                bbox = item[0]
                if isinstance(item[1], tuple) and len(item[1]) >= 2:
                    text, confidence = item[1]
                    if not text:
                        continue

                    conf_score = confidence * 100 if confidence <= 1.0 else confidence
                    text_lines.append(text)
                    confidences.append(conf_score)

                    if isinstance(bbox, list) and len(bbox) >= 4:
                        xs = [point[0] for point in bbox if isinstance(point, (list, tuple)) and len(point) >= 2]
                        ys = [point[1] for point in bbox if isinstance(point, (list, tuple)) and len(point) >= 2]
                        if xs and ys:
                            min_x = min(xs)
                            max_x = max(xs)
                            min_y = min(ys)
                            max_y = max(ys)
                            line_items.append({
                                "text": text,
                                "confidence": conf_score,
                                "bbox": bbox,
                                "min_x": min_x,
                                "max_x": max_x,
                                "min_y": min_y,
                                "max_y": max_y,
                                "center_x": (min_x + max_x) / 2,
                                "center_y": (min_y + max_y) / 2,
                            })

        return text_lines, confidences, line_items

    def _sort_lines_by_position(self, lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort OCR lines by approximate reading order (top-to-bottom, left-to-right).

        PaddleOCR hands back boxes, not a reading order, and every field
        regex in ``FieldExtractor`` is applied to the flattened text built
        from them. The old implementation bucketed ``min_y`` into 10px
        buckets, which reorders any label and value straddling a bucket
        boundary -- exactly the kind of lines a form is made of.

        Lines are therefore clustered into visual rows by vertical overlap
        and each row is emitted left-to-right. See ``_MAX_ROW_HEIGHT_FACTOR``
        for why a plain overlap merge is not enough on its own.
        """
        if not lines:
            return []

        rows: List[Dict[str, Any]] = []
        for line in sorted(lines, key=lambda item: (item["min_y"], item["min_x"])):
            top, bottom = line["min_y"], line["max_y"]
            best, best_overlap = None, 0.0
            for row in rows:
                new_height = max(bottom, row["ymax"]) - min(top, row["ymin"])
                old_height = row["ymax"] - row["ymin"]
                if new_height > _MAX_ROW_HEIGHT_FACTOR * min(new_height, old_height, bottom - top):
                    continue
                overlap = min(bottom, row["ymax"]) - max(top, row["ymin"])
                ref = min(bottom - top, old_height)
                if ref > 0 and overlap / ref > _ROW_OVERLAP_RATIO and overlap / ref > best_overlap:
                    best, best_overlap = row, overlap / ref

            if best is not None:
                best["items"].append(line)
                best["ymin"] = min(best["ymin"], top)
                best["ymax"] = max(best["ymax"], bottom)
            else:
                rows.append({"items": [line], "ymin": top, "ymax": bottom})

        ordered: List[Dict[str, Any]] = []
        for row in sorted(rows, key=lambda row: row["ymin"]):
            ordered.extend(sorted(row["items"], key=lambda item: item["min_x"]))
        return ordered

    def process_image(self, image_data: bytes) -> Tuple[str, float, List[Dict[str, Any]]]:
        """Process image bytes and extract text.

        Args:
            image_data: Raw image bytes

        Returns:
            Tuple of (extracted_text, average_confidence, ocr_lines)
        """
        try:
            image = Image.open(BytesIO(image_data))
            # Photos carry an EXIF orientation tag; PIL does not apply it on
            # load. Without this the whole page can reach PaddleOCR rotated
            # 90 degrees, and use_angle_cls only flips individual text
            # lines between 0 and 180 degrees, so it cannot help here.
            transposed = ImageOps.exif_transpose(image)
            if transposed is not None:
                image = transposed
            image_array = np.array(image)

            # PaddleOCR v2.x uses ocr() method
            result = self.ocr.ocr(image_array, cls=True)
            if result and result[0]:
                result = result[0]

            text_lines, confidences, line_items = self._extract_text_from_result(result)
            line_items = self._sort_lines_by_position(line_items)
            combined_text = "\n".join([line["text"] for line in line_items] or text_lines)
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

            return combined_text, avg_confidence, line_items

        except Exception as e:
            logger.error(f"OCR processing failed: {e}")
            raise

    def _extract_pdf_text_layer(self, pdf_data: bytes) -> tuple[str, bool]:
        """Try to extract embedded text from PDF using pdfplumber.

        pdfplumber is more robust than pdftotext for complex PDF structures,
        especially Chinese e-invoices which often have unusual text layouts.

        Args:
            pdf_data: Raw PDF bytes

        Returns:
            Tuple of (extracted_text, has_useful_text)
        """
        try:
            import pdfplumber
            import tempfile
            import os

            # Write PDF to temp file
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
                f.write(pdf_data)
                temp_pdf = f.name

            try:
                all_text = []
                with pdfplumber.open(temp_pdf) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            all_text.append(text)

                combined_text = "\n".join(all_text)

                # Check if we got meaningful invoice data
                # Look for actual invoice content: numbers, dates, amounts
                has_invoice_number = bool(re.search(r'\d{10,20}', combined_text))  # Long number (invoice number)
                has_tax_id = bool(re.search(r'[A-Z0-9]{15,20}', combined_text))  # Tax ID pattern
                has_amount = bool(re.search(r'[¥￥]?\s*\d+\.\d{2}', combined_text))  # Currency amount
                has_keywords = any(kw in combined_text for kw in ['发票', '税', '金额', '合计', '购买方', '销售方'])

                # Consider useful if has invoice-like content
                has_useful = len(combined_text) > 100 and has_keywords and (has_invoice_number or has_tax_id or has_amount)

                if has_useful:
                    logger.info(f"PDF text layer extraction successful (pdfplumber): {len(combined_text)} chars")
                else:
                    logger.debug(f"PDF text extraction got {len(combined_text)} chars but no useful invoice data")

                return combined_text, has_useful
            finally:
                os.unlink(temp_pdf)

        except Exception as e:
            logger.warning(f"PDF text layer extraction failed: {e}")
            return "", False

    def process_pdf(self, pdf_data: bytes) -> Tuple[str, float, List[Dict[str, Any]]]:
        """Process PDF and extract text from all pages.

        First tries to extract embedded text layer (faster, more accurate for digital PDFs),
        then falls back to OCR for scanned PDFs.

        Args:
            pdf_data: Raw PDF bytes

        Returns:
            Tuple of (extracted_text, average_confidence, ocr_lines)
        """
        # First try to extract embedded text layer
        text_layer, has_useful_text = self._extract_pdf_text_layer(pdf_data)

        if has_useful_text:
            logger.info("Using PDF embedded text layer instead of OCR")
            # High confidence for embedded text
            return text_layer, 99.0, []

        # Fall back to OCR for scanned PDFs
        logger.info("PDF has no useful text layer, falling back to OCR")

        try:
            from pdf2image import convert_from_bytes

            # Use higher DPI (300) for better text recognition
            # Some PDFs have text rendered as graphics which need higher resolution
            images = convert_from_bytes(pdf_data, dpi=300)
            logger.info(f"PDF converted to {len(images)} images at 300 DPI")

            all_text = []
            all_confidences = []
            all_lines: List[Dict[str, Any]] = []

            for image in images:
                image_array = np.array(image)

                # PaddleOCR v2.x uses ocr() method
                result = self.ocr.ocr(image_array, cls=True)
                if result and result[0]:
                    result = result[0]

                text_lines, confidences, line_items = self._extract_text_from_result(result)
                all_text.extend(text_lines)
                all_confidences.extend(confidences)
                all_lines.extend(line_items)

            all_lines = self._sort_lines_by_position(all_lines)
            combined_text = "\n".join([line["text"] for line in all_lines] or all_text)
            avg_confidence = sum(all_confidences) / len(all_confidences) if all_confidences else 0.0

            return combined_text, avg_confidence, all_lines

        except Exception as e:
            logger.error(f"PDF processing failed: {e}")
            raise


class FieldExtractor:
    """Extracts structured fields from invoice text (Chinese and English)."""

    # Regex patterns for Chinese invoice fields
    # Note: PaddleOCR often reads two-column invoices in interleaved order
    # This causes buyer/seller info to be mixed. We use position-based extraction.
    # Both invoice numbers also appear without their label: a paper VAT
    # invoice prints "No 09750785" and a bare 10-digit code in the top-left
    # corner, so the label-only patterns never fire on photographed invoices.
    # The second pattern group keeps the labelled form working for
    # fill-in-style invoices.
    PATTERNS_CN = {
        'invoice_number': r'(?:发票号码[：:]\s*(\d+)|No\.?\s*(\d{8,20}))',
        'invoice_code': r'(?:发票代码[：:]\s*(\d+)|(?<!\d)(\d{10})(?!\d))',
        'issue_date': r'开票日期[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2})',
        # For buyer/seller, we use special extraction in extract_fields() method
        # These patterns are placeholders - actual extraction uses _extract_buyer_seller()
        'buyer_name': None,  # Extracted specially
        'buyer_tax_id': None,  # Extracted specially
        'seller_name': None,  # Extracted specially
        'seller_tax_id': None,  # Extracted specially
        # Match items with * markers in Chinese invoices
        # Format is typically *类别*商品名称, we want to capture "类别*商品名称"
        # Example: *家具*办公椅 → captures "家具*办公椅"
        'item_name': r'\*([^*\n]+(?:\*[^*\n]+)*)',
        # This label is followed by the capitalised amount ("（大写）叁万捌仟
        # …圆整") and only then by the ￥ figure, so the pattern below cannot
        # reach it. It is kept for fill-in templates where label, ￥ and digits
        # do share a line; the real source of truth is the position-based pass
        # in _extract_fields_layout, which reads the 小写 cell directly.
        'total_with_tax': r'(?:小写|价税合计)[）)（(]*\s*[¥￥]\s*([\d,.]+)',
        'amount': r'(?:合\s*计|金\s*额)\s*[¥￥]?\s*([\d,.]+)',
        # The old second branch ([¥￥]...[¥￥]...) matched the two amounts on
        # the 合计 line ("¥32705.98 ¥5560.02") and returned whichever came
        # second. It happened to be the tax amount on the test invoice, but
        # it has nothing to do with the tax amount: any layout change there
        # and it silently returns a wrong number. Removed; the position-based
        # pass below recovers this field from the tax column instead.
        'tax_amount': r'(?:税\s*额\s*[¥￥]?\s*([\d,.]+)|\u7f34\s*税\s*额\s*[¥￥]?\s*([\d,.]+))',
        'tax_rate': r'(?:税率[/／征收率]*[：:]?\s*|金额\n)(\d+%?|免税)',
    }

    #: 法定增值税税率（百分比）。只在能从 税额/金额 反推出精确值时使用。
    TAX_RATES = (3, 5, 6, 9, 10, 11, 13, 16, 17)

    # Regex patterns for English invoice fields
    PATTERNS_EN = {
        'invoice_number': r'(?:Invoice\s*#?|INV[_-]?|#)\s*[:#]?\s*([A-Z0-9_-]+)',
        'issue_date': r'(?:Date|Invoice\s+Date)[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        'buyer_name': r'Bill\s*To[:\s]*\n(?:.*\n)*?([A-Za-z][A-Za-z0-9\s]+?)(?:\n\d|\n[A-Z][a-z])',
        'seller_name': r'INVOICE\n#[^\n]+\n([A-Za-z][A-Za-z\s]+)',
        'total_with_tax': r'(?:Total|Balance\s+Due|Amount\s+Due)[:\s]*\$?\s*([\d,.]+)',
        'amount': r'(?:Subtotal)[:\s]*\$?\s*([\d,.]+)',
    }

    def _is_chinese_invoice(self, text: str) -> bool:
        """Detect if the invoice is in Chinese."""
        chinese_keywords = ['发票', '购买方', '销售方', '税额', '价税合计', '纳税人']
        return any(kw in text for kw in chinese_keywords)

    def _find_label_positions(self, text: str) -> Dict[str, list[int]]:
        """Find positions of buyer/seller label markers in text.

        Returns:
            Dictionary with 'buyer' and 'seller' keys, each containing
            list of character positions where labels were found.
        """
        # Define label markers for buyer and seller
        buyer_labels = ['购买方', '购方', '购货单位']
        # Seller labels include contextual markers that only appear in seller section
        seller_labels = ['销售方', '销方', '销货单位', '开票人', '收款人', '复核']

        positions: Dict[str, list[int]] = {'buyer': [], 'seller': []}

        for label in buyer_labels:
            for match in re.finditer(re.escape(label), text):
                positions['buyer'].append(match.start())

        for label in seller_labels:
            for match in re.finditer(re.escape(label), text):
                positions['seller'].append(match.start())

        # Handle vertical text format: 购/买/方 or 销/售/方 (with spaces/newlines)
        vertical_buyer = re.finditer(r'购\s*买\s*方', text)
        for match in vertical_buyer:
            positions['buyer'].append(match.start())

        vertical_seller = re.finditer(r'销\s*售\s*方', text)
        for match in vertical_seller:
            positions['seller'].append(match.start())

        # Handle spaced labels like "购 名称" / "销 名称" and "买 方" / "售 方"
        for match in re.finditer(r'购\s*名\s*称', text):
            positions['buyer'].append(match.start())
        for match in re.finditer(r'销\s*名\s*称', text):
            positions['seller'].append(match.start())
        for match in re.finditer(r'买\s*方', text):
            positions['buyer'].append(match.start())
        for match in re.finditer(r'售\s*方', text):
            positions['seller'].append(match.start())

        return positions

    def _find_label_positions_from_lines(self, lines: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Find buyer/seller label lines using OCR line metadata."""
        buyer_labels = ['购买方', '购方', '购货单位']
        seller_labels = ['销售方', '销方', '销货单位', '开票人', '收款人', '复核']
        buyer_patterns = [
            r'购\s*买\s*方',
            r'购\s*名\s*称',
            r'买\s*方',
        ]
        seller_patterns = [
            r'销\s*售\s*方',
            r'销\s*名\s*称',
            r'售\s*方',
        ]

        positions: Dict[str, List[Dict[str, Any]]] = {'buyer': [], 'seller': []}

        for line in lines:
            text = line.get("text", "")
            if any(label in text for label in buyer_labels) or any(re.search(pat, text) for pat in buyer_patterns):
                positions['buyer'].append(line)
            if any(label in text for label in seller_labels) or any(re.search(pat, text) for pat in seller_patterns):
                positions['seller'].append(line)

        return positions

    def _line_distance(self, line: Dict[str, Any], label_line: Dict[str, Any]) -> float:
        """Calculate weighted distance between a line and a label line."""
        dy = abs(line["center_y"] - label_line["center_y"])
        dx = abs(line["center_x"] - label_line["center_x"])
        return dy + (dx * 0.3)

    def _classify_line_by_labels(self, line: Dict[str, Any], label_lines: Dict[str, List[Dict[str, Any]]]) -> Optional[str]:
        """Classify a line as buyer or seller based on nearest label line."""
        buyer_lines = label_lines.get("buyer", [])
        seller_lines = label_lines.get("seller", [])

        def min_distance(lines: List[Dict[str, Any]]) -> float:
            if not lines:
                return float('inf')
            return min(self._line_distance(line, label) for label in lines)

        buyer_dist = min_distance(buyer_lines)
        seller_dist = min_distance(seller_lines)

        if buyer_dist == float('inf') and seller_dist == float('inf'):
            return None

        max_dist = max(buyer_dist, seller_dist)
        if max_dist and abs(buyer_dist - seller_dist) / max_dist < 0.15:
            return None

        return 'buyer' if buyer_dist < seller_dist else 'seller'

    def _min_label_distance(
        self,
        line: Dict[str, Any],
        label_lines: Dict[str, List[Dict[str, Any]]],
        label_key: str
    ) -> float:
        lines = label_lines.get(label_key, [])
        if not lines:
            return float('inf')
        return min(self._line_distance(line, label) for label in lines)

    def _normalize_line_text(self, text: str) -> str:
        """Normalize OCR line text for matching."""
        return re.sub(r'\s+', '', text or '')

    def _parse_inline_buyer_seller_names(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Parse buyer/seller names from inline label text."""
        match = re.search(r'购\s*名\s*称[：:]\s*(.+?)\s*销\s*名\s*称[：:]\s*(.+)', text)
        if not match:
            return None, None
        buyer_name = self._clean_chinese_spaces(match.group(1))
        seller_name = self._clean_chinese_spaces(match.group(2))
        return buyer_name, seller_name

    def _find_line_for_value(self, value: str, lines: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Find the line containing the given value."""
        if not value:
            return None
        normalized_value = self._normalize_line_text(value)
        for line in lines:
            line_text = line.get("text", "")
            if value in line_text:
                return line
            if normalized_value and normalized_value in self._normalize_line_text(line_text):
                return line
        return None

    def _classify_by_proximity(self, item_pos: int, label_positions: Dict[str, list[int]]) -> str:
        """Classify an item as 'buyer' or 'seller' based on nearest label.

        Args:
            item_pos: Character position of the item (company name or tax ID)
            label_positions: Dictionary from _find_label_positions()

        Returns:
            'buyer' or 'seller' based on which label is closer
        """
        def min_distance(positions: list[int]) -> float:
            if not positions:
                return float('inf')
            return min(abs(item_pos - pos) for pos in positions)

        buyer_dist = min_distance(label_positions['buyer'])
        seller_dist = min_distance(label_positions['seller'])

        return 'buyer' if buyer_dist < seller_dist else 'seller'

    def _extract_buyer_seller(self, text: str, lines: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Optional[str]]:
        """Extract buyer and seller info using label proximity.

        Uses context-aware extraction based on proximity to label markers
        (购买方/销售方) rather than relying on match order. This handles
        PaddleOCR's interleaved column reading correctly.

        Args:
            text: OCR extracted text

        Returns:
            Dictionary with buyer_name, buyer_tax_id, seller_name, seller_tax_id
        """
        result = {
            'buyer_name': None,
            'buyer_tax_id': None,
            'seller_name': None,
            'seller_tax_id': None,
        }

        label_positions = self._find_label_positions(text)
        has_labels = bool(label_positions['buyer'] or label_positions['seller'])

        line_labels = None
        if lines:
            line_labels = self._find_label_positions_from_lines(lines)

        if line_labels and line_labels['buyer'] and line_labels['seller']:
            line_result = self._extract_buyer_seller_from_lines(lines, line_labels)
            result.update({k: v for k, v in line_result.items() if v})

        if has_labels:
            text_result = self._extract_buyer_seller_from_text(text, label_positions)
            for key, value in text_result.items():
                if not result.get(key) and value:
                    result[key] = value

        if not all(result.values()):
            fallback = self._extract_buyer_seller_fallback(text)
            for key, value in fallback.items():
                if not result.get(key) and value:
                    result[key] = value

        if line_labels and (line_labels['buyer'] and line_labels['seller']):
            result = self._swap_if_needed(result, lines, line_labels)

        if not has_labels and not (line_labels and (line_labels['buyer'] or line_labels['seller'])):
            logger.info("No buyer/seller labels found, using position-based fallback")

        return result

    def _extract_buyer_seller_from_text(
        self,
        text: str,
        label_positions: Dict[str, list[int]]
    ) -> Dict[str, Optional[str]]:
        """Extract buyer/seller using label proximity in text."""
        result = {
            'buyer_name': None,
            'buyer_tax_id': None,
            'seller_name': None,
            'seller_tax_id': None,
        }

        for line in text.splitlines():
            if '购' in line and '销' in line:
                buyer_name, seller_name = self._parse_inline_buyer_seller_names(line)
                if buyer_name:
                    result['buyer_name'] = buyer_name
                if seller_name:
                    result['seller_name'] = seller_name
                if result['buyer_name'] or result['seller_name']:
                    break

        name_pattern = r'(?:名\s*)?称[：:]\s*(.+)$'
        name_matches = [(m.start(), m.group(1)) for m in re.finditer(name_pattern, text, re.MULTILINE)]

        if name_matches and not (result['buyer_name'] and result['seller_name']):
            buyers = []
            sellers = []

            for pos, name in name_matches:
                classification = self._classify_by_proximity(pos, label_positions)
                cleaned_name = self._clean_chinese_spaces(name)
                if classification == 'buyer':
                    buyers.append(cleaned_name)
                else:
                    sellers.append(cleaned_name)

            if buyers:
                result['buyer_name'] = buyers[0]
            if sellers:
                result['seller_name'] = sellers[0]

            logger.info(f"Label-based extraction: buyer={result['buyer_name']}, seller={result['seller_name']}")

        tax_id_pattern = r'(?:纳税人识别号|统一社会信用代码|税号)[：:]\s*([A-Z0-9]{15,20})'
        if not result['buyer_tax_id'] and not result['seller_tax_id']:
            for line in text.splitlines():
                line_matches = re.findall(tax_id_pattern, line, re.IGNORECASE)
                if len(line_matches) >= 2:
                    result['buyer_tax_id'] = line_matches[0].strip()
                    result['seller_tax_id'] = line_matches[1].strip()
                    break

        tax_matches = [(m.start(), m.group(1)) for m in re.finditer(tax_id_pattern, text, re.IGNORECASE)]

        if tax_matches and not (result['buyer_tax_id'] and result['seller_tax_id']):
            buyer_tax_ids = []
            seller_tax_ids = []

            for pos, tax_id in tax_matches:
                classification = self._classify_by_proximity(pos, label_positions)
                if classification == 'buyer':
                    buyer_tax_ids.append(tax_id.strip())
                else:
                    seller_tax_ids.append(tax_id.strip())

            if buyer_tax_ids:
                result['buyer_tax_id'] = buyer_tax_ids[0]
            if seller_tax_ids:
                result['seller_tax_id'] = seller_tax_ids[0]

        return result

    def _extract_buyer_seller_from_lines(
        self,
        lines: List[Dict[str, Any]],
        label_lines: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, Optional[str]]:
        """Extract buyer/seller fields from OCR lines using label proximity."""
        result = {
            'buyer_name': None,
            'buyer_tax_id': None,
            'seller_name': None,
            'seller_tax_id': None,
        }

        buyers: List[Tuple[float, str]] = []
        sellers: List[Tuple[float, str]] = []
        buyer_tax_ids: List[Tuple[float, str]] = []
        seller_tax_ids: List[Tuple[float, str]] = []

        name_pattern = r'(?:名\s*)?称[：:]\s*(.+)$'
        tax_id_pattern = r'(?:纳税人识别号|统一社会信用代码|税号)[：:]\s*([A-Z0-9]{15,20})'

        for line in lines:
            text = line.get("text", "")
            inline_buyer, inline_seller = self._parse_inline_buyer_seller_names(text)
            if inline_buyer or inline_seller:
                if inline_buyer and not result['buyer_name']:
                    result['buyer_name'] = inline_buyer
                if inline_seller and not result['seller_name']:
                    result['seller_name'] = inline_seller
            name_match = re.search(name_pattern, text)
            tax_match = re.search(tax_id_pattern, text, re.IGNORECASE)

            classification = self._classify_line_by_labels(line, label_lines)
            if name_match and classification:
                cleaned_name = self._clean_chinese_spaces(name_match.group(1))
                if classification == 'buyer':
                    buyers.append((self._min_label_distance(line, label_lines, 'buyer'), cleaned_name))
                else:
                    sellers.append((self._min_label_distance(line, label_lines, 'seller'), cleaned_name))

            if tax_match and classification:
                tax_id = tax_match.group(1).strip()
                if classification == 'buyer':
                    buyer_tax_ids.append((self._min_label_distance(line, label_lines, 'buyer'), tax_id))
                else:
                    seller_tax_ids.append((self._min_label_distance(line, label_lines, 'seller'), tax_id))

        if buyers:
            result['buyer_name'] = sorted(buyers, key=lambda x: x[0])[0][1]
        if sellers:
            result['seller_name'] = sorted(sellers, key=lambda x: x[0])[0][1]
        if buyer_tax_ids:
            result['buyer_tax_id'] = sorted(buyer_tax_ids, key=lambda x: x[0])[0][1]
        if seller_tax_ids:
            result['seller_tax_id'] = sorted(seller_tax_ids, key=lambda x: x[0])[0][1]

        return result

    def _swap_if_needed(
        self,
        result: Dict[str, Optional[str]],
        lines: List[Dict[str, Any]],
        label_lines: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, Optional[str]]:
        """Swap buyer/seller fields when label proximity indicates reversal."""
        buyer_name = result.get("buyer_name")
        seller_name = result.get("seller_name")

        if buyer_name and seller_name:
            buyer_line = self._find_line_for_value(buyer_name, lines)
            seller_line = self._find_line_for_value(seller_name, lines)
            if buyer_line and seller_line:
                buyer_class = self._classify_line_by_labels(buyer_line, label_lines)
                seller_class = self._classify_line_by_labels(seller_line, label_lines)
                if buyer_class == 'seller' and seller_class == 'buyer':
                    result["buyer_name"], result["seller_name"] = seller_name, buyer_name
                    logger.info("Swapped buyer/seller names based on label proximity")

        buyer_tax = result.get("buyer_tax_id")
        seller_tax = result.get("seller_tax_id")
        if buyer_tax and seller_tax:
            buyer_line = self._find_line_for_value(buyer_tax, lines)
            seller_line = self._find_line_for_value(seller_tax, lines)
            if buyer_line and seller_line:
                buyer_class = self._classify_line_by_labels(buyer_line, label_lines)
                seller_class = self._classify_line_by_labels(seller_line, label_lines)
                if buyer_class == 'seller' and seller_class == 'buyer':
                    result["buyer_tax_id"], result["seller_tax_id"] = seller_tax, buyer_tax
                    logger.info("Swapped buyer/seller tax IDs based on label proximity")

        return result

    def _extract_buyer_seller_fallback(self, text: str) -> Dict[str, Optional[str]]:
        """Fallback position-based extraction when no labels found.

        Uses original logic: first company name = buyer, second = seller.
        """
        result = {
            'buyer_name': None,
            'buyer_tax_id': None,
            'seller_name': None,
            'seller_tax_id': None,
        }

        # Pattern to match company names
        name_pattern = r'(?:名\s*)?称[：:]\s*([^销购\n]*?(?:有限公司|股份公司|集团|公司))'
        name_matches = re.findall(name_pattern, text)

        if len(name_matches) >= 2:
            result['buyer_name'] = self._clean_chinese_spaces(name_matches[0])
            result['seller_name'] = self._clean_chinese_spaces(name_matches[1])
            logger.info(f"Position-based fallback: buyer={result['buyer_name']}, seller={result['seller_name']}")
        elif len(name_matches) == 1:
            cleaned_name = self._clean_chinese_spaces(name_matches[0])
            # Try to determine from context
            if '购' in text[:text.find(name_matches[0])] if name_matches[0] in text else False:
                result['buyer_name'] = cleaned_name
            else:
                result['seller_name'] = cleaned_name

        # Tax IDs - position based
        tax_id_pattern = r'(?:纳税人识别号|统一社会信用代码|税号)[：:]\s*([A-Z0-9]{15,20})'
        tax_matches = re.findall(tax_id_pattern, text, re.IGNORECASE)

        if len(tax_matches) >= 2:
            result['buyer_tax_id'] = tax_matches[0].strip()
            result['seller_tax_id'] = tax_matches[1].strip()
        elif len(tax_matches) == 1:
            if result['buyer_name'] and not result['seller_name']:
                result['buyer_tax_id'] = tax_matches[0].strip()
            elif result['seller_name'] and not result['buyer_name']:
                result['seller_tax_id'] = tax_matches[0].strip()

        # Ultimate fallback: simpler pattern
        if not result['buyer_name'] and not result['seller_name']:
            simple_pattern = r'([^\n]*(?:有限公司|股份公司))'
            simple_matches = re.findall(simple_pattern, text)
            if len(simple_matches) >= 2:
                result['buyer_name'] = self._clean_chinese_spaces(simple_matches[0])
                result['seller_name'] = self._clean_chinese_spaces(simple_matches[1])
                logger.info(f"Ultimate fallback: buyer={result['buyer_name']}, seller={result['seller_name']}")

        return result

    @staticmethod
    def _is_decoration(line: Dict[str, Any], width: int, height: int) -> bool:
        """True for vertical margin banners and the encrypted column.

        The left margin banner is printed rotated and OCR reads the whole
        thing as one box, e.g. "（0016]311号南京造币有限公司". It contains a
        company name, so any extractor that looks for "…有限公司" on the
        page will latch onto the printer's name instead of the seller's.
        """
        left = line["min_x"] / width
        right = line["max_x"] / width
        top = line["min_y"] / height
        box_height = line["max_y"] - line["min_y"]
        box_width = max(line["max_x"] - line["min_x"], 1)
        text = line.get("text", "")

        # Vertical banner: spans a large part of the page height and hugs a
        # margin. Height is measured against the page, not against the box's
        # own width -- a rotated company name is long enough to be nearly
        # square in glyph terms, yet no real field is a quarter-page tall.
        if (box_height >= height * _BANNER_MIN_HEIGHT
                and box_width <= width * _BANNER_MAX_WIDTH
                and (left < _BANNER_LEFT or right > _BANNER_RIGHT)):
            return True

        # Encrypted column: middle-right band of the upper half of the page.
        # The block is printed as rotated digits mixed with + - * / < >;
        # requiring two of those, no Chinese, and real width keeps genuine
        # values such as "17%" or "税率" in the same area out of the filter.
        if (left >= _CIPHER_LEFT and top <= _CIPHER_BOTTOM
                and box_width >= _CIPHER_MIN_WIDTH * width
                and not FieldExtractor._has_cjk(text)
                and sum(1 for char in text if char in _CIPHER_SYMBOLS) >= 2):
            return True
        return False

    @staticmethod
    def _y_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> float:
        overlap = min(a["max_y"], b["max_y"]) - max(a["min_y"], b["min_y"])
        ref = min(a["max_y"] - a["min_y"], b["max_y"] - b["min_y"])
        if ref <= 0 or overlap <= 0:
            return 0.0
        return overlap / ref

    def _find_label(self, lines: List[Dict[str, Any]], keyword: str,
                    allow_inline: bool = False) -> Optional[Dict[str, Any]]:
        """Return the line carrying `keyword` as a field label.

        Two shapes occur and both are normal: the label is its own box
        ("称："), or OCR merges label and value into one box
        ("称：中国科学院自动化研究所"). The latter is only accepted when the
        caller asks for it, so a short exact label always wins.
        """
        merged = None
        for line in lines:
            text = re.sub(r"\s+", "", line.get("text", ""))
            if keyword not in text:
                continue
            if len(text) <= len(keyword) + 4:
                return line
            if allow_inline and merged is None and text.startswith(keyword):
                merged = line
        return merged

    @classmethod
    def _cell_value(cls, label: Dict[str, Any], lines: List[Dict[str, Any]],
                    keyword: Optional[str] = None) -> Optional[str]:
        """Read the cell to the right of a label line.

        Answers the question a human answers when reading a form: which text
        sits in the same visual row, starting just after the label. Doing it
        per-label (instead of on the flattened text) is what makes it immune
        to the interleaving described above.

        When label and value share one box the value is the remainder of
        that box; the anchor is the label's *centre* rather than its right
        edge, because OCR boxes overlap -- the "纳税人识别号：" box of
        vat_0070 ends at x=574 while its value starts at x=561.
        """
        text = re.sub(r"\s+", "", label.get("text", ""))
        if keyword and text.startswith(keyword):
            inline = text[len(keyword):].strip(":：")
            if inline:
                return inline

        anchor = label["center_x"]
        candidates = [
            line for line in lines
            if line is not label
            and line["min_x"] >= anchor
            and cls._y_overlap(label, line) >= 0.5
        ]
        if not candidates:
            candidates = [
                line for line in lines
                if line is not label
                and line["min_x"] > anchor
                and cls._y_overlap(label, line) >= 0.3
            ]
        if not candidates:
            return None
        value = min(candidates, key=lambda line: line["min_x"])["text"]
        value = re.sub(r"\s+", "", value)
        return value.strip(":：") or None

    @classmethod
    def _cell_money(cls, label: Dict[str, Any], lines: List[Dict[str, Any]]) -> Optional[str]:
        """The 小写 figure printed under the 价税合计（大写） label.

        ``_cell_value`` returns whichever box starts closest to the label,
        which here is the Chinese capital amount ("叁万捌仟…") -- correct for
        a human, useless as a field value. The 小写 figure is the money box
        that starts below the label line; the two boxes of the 合计 row
        above it are the same two numbers spelled without the 小写 marker.
        """
        def is_money(line: Dict[str, Any]) -> bool:
            return "¥" in line["text"] or "￥" in line["text"]

        below = [line for line in lines
                 if line is not label and line["min_y"] >= label["min_y"]
                 and is_money(line)]
        if not below:
            below = [line for line in lines
                     if line is not label and line["min_x"] > label["max_x"]
                     and cls._y_overlap(label, line) >= 0.2 and is_money(line)]
        if not below:
            return None
        return cls._clean_money(max(below, key=lambda line: line["min_x"])["text"])

    def _materialise(self, lines: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        """Rebuild the flattened text from cleaned, position-ordered lines."""
        try:
            width = max(line["max_x"] for line in lines)
            height = max(line["max_y"] for line in lines)
        except ValueError:
            return "\n".join(line["text"] for line in lines), list(lines)
        if width <= 0 or height <= 0:
            return "\n".join(line["text"] for line in lines), list(lines)

        kept = [line for line in lines if not self._is_decoration(line, width, height)]
        ordered = sorted(kept, key=lambda line: (line["min_y"], line["min_x"]))
        return "\n".join(line["text"] for line in ordered), ordered

    @staticmethod
    def _has_cjk(value: Optional[str]) -> bool:
        return bool(value) and bool(re.search(r"[\u4e00-\u9fff]", value))

    def _extract_fields_layout(self, text: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Position-aware extraction used as a safety net for the regex pass.

        ``PATTERNS_CN`` assumes a label is immediately followed by its value
        in the flattened OCR text. On a photographed invoice that rarely
        holds: the encrypted column, the two vertical margin banners and
        single-character split labels all interleave with the real text. This
        method re-reads the same boxes by layout (rows, columns and label
        anchors) and returns whatever the regex pass could not resolve.
        """
        if not lines:
            return {}

        width = max(line["max_x"] for line in lines)
        height = max(line["max_y"] for line in lines)
        if width <= 0 or height <= 0:
            return {}

        body = [line for line in lines if not self._is_decoration(line, width, height)]
        out: Dict[str, Any] = {}

        # --- 票头：发票代码 / 发票号码 -----------------------------------
        for line in (line for line in body if line["max_y"] < height * 0.20):
            match = re.search(r"No\.?\s*(\d{8,20})", line["text"], re.IGNORECASE)
            if match:
                out.setdefault("invoice_number", match.group(1))
            for code in re.findall(r"(?<!\d)(\d{10})(?!\d)", line["text"]):
                out.setdefault("invoice_code", code)
        if not out.get("invoice_number"):
            for line in body:
                match = re.search(r"(?<!\d)(0\d{7})(?!\d)", line["text"])
                if match:
                    out["invoice_number"] = match.group(1)
                    break

        # --- 货物表名定位购销分界 ----------------------------------------
        table_top = None
        for line in body:
            if "货物" in line["text"] and "名称" in line["text"]:
                table_top = line["min_y"]
                break

        if table_top is not None:
            for zone, low, high in (("buyer", 0.0, table_top),
                                    ("seller", table_top, height)):
                zone_lines = [line for line in body if low <= line["min_y"] <= high]
                # 单字行拆分后这一格只剩「称：」，所以按「称」而不是「名称」找。
                # 找到标签但那一格是空的，也要写一个 None 进去：它表示"版面看
                # 过了、确实没有值"，与"版面没覆盖到"是两回事，后者才该退回旧
                # 的启发式（见 extract_fields）。
                for suffix, keyword in (("tax_id", "纳税人识别号"), ("name", "称")):
                    label = self._find_label(zone_lines, keyword, allow_inline=True)
                    if not label:
                        continue
                    value = self._cell_value(label, zone_lines, keyword)
                    if value == label["text"]:
                        value = None
                    out.setdefault(zone + "_" + suffix, value)

            # 货物名称：只看表头下面那一两行里靠左的中文块。竖排边栏会出现
            # 单个「名」「称」之类的孤立字，靠 y 区间和最少两字把它挡掉。
            item_rows = [line for line in body
                         if table_top < line["min_y"] < table_top + height * 0.22]
            item = self._pick_in_row(
                item_rows,
                lambda line: (line["min_x"] < width * 0.38
                              and self._has_cjk(line["text"])
                              and len(line["text"]) >= 2
                              and "货物" not in line["text"]
                              and not line["text"].strip().startswith(("规格", "单位", "数量", "单价"))),
            )
            if item:
                out.setdefault("item_name", item)

        # --- 金额 / 税额 / 税率：按表头列定位后取值 -----------------------
        # 合计行把 金额、税额、价税合计 挤在同一行，靠标签先后顺序无法区分，
        # 只能回到版面：用表头 金额/税额/税率 定出列的 x 区间，再在列里取
        # 离列心最近的一个值。表头本身常被 OCR 拆成单字行（金/额、税/额），
        # 所以定位交给 _column_span()。
        if table_top is not None:
            # table_top 是「货物或应税劳务、服务名称」那一格的 min_y，但表头
            # 那一行的各个单元格并不共享同一个 min_y：单行明细的单元格基线
            # 碰巧一致，多行明细时 OCR 给出的行高不同，整行的 min_y 会散开
            # （vat_0058 的「税率」是 465，「货物…名称」已经是 483，差 18px）。
            # 用 table_top 直接当下界就会把它前面的所有表头格（规格/单位/数
            # 量/单价/金/额/税率）全部挡在池子外，_column_span 找不到任何表头
            # 盒子，金额/税额/税率三列一起失效 —— 多行发票抽不出的真正原因。
            # 回溯 30px 与 item_layout.locate_columns 的窗口口径一致，那套在
            # 全部测试票上都能框住完整表头带。下界仍用闭区间：同一视觉行的
            # 表头格共享 min_y，严格大于会把「税率」本身排除掉。
            table_lines = [line for line in body
                           if table_top - 30 <= line["min_y"] < table_top + height * 0.35]

            for keyword, key, predicate in (
                ("金额", "amount", self._is_money_line),
                ("税额", "tax_amount", self._is_money_line),
                ("税率", "tax_rate",
                 lambda line: "%" in line["text"] or "免税" in line["text"]),
            ):
                column = self._column_span(table_lines, keyword)
                if not column:
                    continue
                value = self._pick_in_column(table_lines, column, predicate)
                if value:
                    out.setdefault(key, value)

            # 价税合计右侧那格同时印着「（大写）叁万…」和「￥38266.00」，
            # 只收带货币符号的那一个。
            total_label = (self._find_label(body, "价税合计")
                           or self._find_label(body, "小写"))
            if total_label:
                total = self._cell_money(total_label, body)
                if total:
                    out.setdefault("total_with_tax", total)

        return out

    @classmethod
    def _is_money_line(cls, line: Dict[str, Any]) -> bool:
        text = line["text"]
        return ("¥" in text or "￥" in text
                or re.fullmatch(r"\s*[\d,]+(?:\.\d{2})?\s*", text) is not None)

    @staticmethod
    def _clean_money(text: str) -> Optional[str]:
        """Pull the numeric part out of an OCR'd money box."""
        match = re.search(r"[\d,]+(?:\.\d{1,2})?", text)
        return match.group(0) if match else None

    @classmethod
    def _column_span(cls, rows: List[Dict[str, Any]], keyword: str) -> Optional[Tuple[float, float]]:
        """Locate a table column by the x-span of its header cell.

        Two failure modes are handled. First, the header is split across
        boxes ("金" / "额"), so a plain substring test on each line finds
        nothing. Second, the fragments of *this* header sit next to the
        fragments of a different one (the "额" of 税额 is four glyphs to the
        right of the "额" of 金额), so merging every matching box would give
        a column twice as wide. The rule therefore is: the narrowest
        horizontally contiguous run of boxes whose characters are exactly
        the keyword's.
        """
        def plain(line: Dict[str, Any]) -> str:
            return re.sub(r"\s+", "", line.get("text", ""))

        for line in rows:
            text = plain(line)
            if keyword in text and len(text) <= len(keyword) + 4:
                return line["min_x"] - 10.0, line["max_x"] + 10.0

        chars = set(keyword)
        wanted = sorted(keyword)
        spans = sorted(
            (line for line in rows
             if 0 < len(plain(line)) <= len(keyword)
             and set(plain(line)) <= chars),
            key=lambda line: (line["min_x"], line["max_x"]))
        if not spans:
            return None

        widest = max(line["max_x"] - line["min_x"] for line in spans)
        gap_limit = widest * 2.0 + 20.0
        best: Optional[Tuple[float, float]] = None
        for i in range(len(spans)):
            for j in range(i, len(spans)):
                if j > i and spans[j]["min_x"] - spans[j - 1]["max_x"] > gap_limit:
                    break
                group = spans[i:j + 1]
                if sorted("".join(plain(line) for line in group)) != wanted:
                    continue
                span = (min(line["min_x"] for line in group) - 10.0,
                        max(line["max_x"] for line in group) + 10.0)
                if best is None or span[1] - span[0] < best[1] - best[0]:
                    best = span
        return best

    @classmethod
    def _pick_in_row(cls, pool: List[Dict[str, Any]], predicate) -> Optional[str]:
        """First matching line of the first data row below the header."""
        for line in pool:
            if predicate(line):
                return line["text"]
        return None

    @classmethod
    def _pick_in_column(cls, pool: List[Dict[str, Any]],
                        column: Tuple[float, float], predicate) -> Optional[str]:
        """Value of the column: nearest box to the column centre matching."""
        centre = (column[0] + column[1]) / 2.0
        hits = [line for line in pool if predicate(line)]
        if not hits:
            return None
        best = min(hits, key=lambda line: abs((line["min_x"] + line["max_x"]) / 2 - centre))
        return cls._clean_money(best["text"])

    def extract_fields(self, text: str, lines: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Extract all invoice fields from OCR text.

        Args:
            text: OCR extracted text
            lines: OCR line metadata (with bounding boxes)

        Returns:
            Dictionary of extracted fields
        """
        fields = {}

        # Log OCR text for debugging (first 500 chars)
        logger.debug(f"OCR text (first 500 chars): {text[:500]}")

        is_chinese = self._is_chinese_invoice(text)

        # Run the regexes against a cleaned, position-ordered text rather than
        # the raw flattening. Without this the encrypted column and the two
        # vertical banners are still interleaved into the party blocks and
        # several patterns can never match (see _is_decoration).
        if lines:
            text, lines = self._materialise(lines)

        # Choose pattern set based on language detection
        if is_chinese:
            patterns = self.PATTERNS_CN
        else:
            patterns = self.PATTERNS_EN

        for field_name, pattern in patterns.items():
            # Skip fields with None pattern (they need special extraction)
            if pattern is None:
                fields[field_name] = None
                continue

            try:
                match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
                if match:
                    # Handle patterns with multiple capture groups (alternation)
                    # Use the first non-None group
                    value = None
                    for i in range(1, len(match.groups()) + 1):
                        if match.group(i):
                            value = match.group(i).strip()
                            break
                    fields[field_name] = self._clean_value(value, field_name) if value else None
                else:
                    fields[field_name] = None
            except Exception as e:
                logger.warning(f"Failed to extract {field_name}: {e}")
                fields[field_name] = None

        # For Chinese invoices, use position-based extraction for buyer/seller
        if is_chinese:
            layout = self._extract_fields_layout(text, lines)
            buyer_seller = self._extract_buyer_seller(text, lines)
            # 购销双方走版面读取（标签锚点 + 同行右侧单元格），旧的距离启发式
            # 只补空缺。原来的写法是让启发式无条件覆盖版面结果，而 PaddleOCR
            # 交错输出两个信息块时后者会把买方写成卖方（见 _extract_buyer_seller
            # 的 _swap_if_needed 只能纠正一半的字段）。
            #
            # 判据是 key 在不在 layout 里，而不是值空不空：版面找到了标签却读
            # 不到值，说明这一格本来就是空的（vat_0051 的购买方税号就是空白
            # 格），此时退回启发式只会把销售方的税号填进购买方。
            for key in ('buyer_name', 'buyer_tax_id', 'seller_name', 'seller_tax_id'):
                fields[key] = layout[key] if key in layout else buyer_seller.get(key)
        else:
            layout = {}

        # Position-based safety net for whatever the regex pass could not
        # resolve. It only fills gaps so a working extraction is never
        # regressed; the one exception is item_name, where the `*...*` pattern
        # can latch onto the "*" inside an encrypted-column string.
        for field, value in layout.items():
            if not value:
                continue
            current = fields.get(field)
            if current is None:
                fields[field] = value
            elif (field == 'item_name' and self._has_cjk(value)
                  and not self._has_cjk(str(current))):
                fields[field] = value

        # 合计行三个数的仲裁、以及税率兜底。必须放在上面的版面兜底之后：
        # 它要用 layout 的值去纠正正则，两者都拿到才算「矛盾」。
        self._reconcile_totals(fields, layout)

        # 税率统一带上百分号。收口放在这里而不是正则里，是因为漏「%」有两条
        # 路径（正则百分号可选、反推分支写整数），而下游要用的是同一个写法。
        # 详细缘由见 normalize_tax_rate。
        fields['tax_rate'] = normalize_tax_rate(fields.get('tax_rate'))

        # 明细行（一张发票可含多行货物）。中文票才有明细表；
        # 抽不到就保持 items 为空列表，下游按「无明细」处理，不报错。
        if is_chinese and lines:
            items, item_diag = self._extract_item_lines(lines)
            fields["items"] = strip_internal(items) if items else []
            fields["items_diag"] = item_diag

        # Log extraction results for debugging
        logger.info(f"OCR extracted: buyer={fields.get('buyer_name')}, seller={fields.get('seller_name')}")

        return fields

    @staticmethod
    def _to_number(value: Any) -> Optional[float]:
        """把 OCR 抓到的数字文本转成 float；不是数字则返回 None。"""
        if value is None:
            return None
        digits = re.sub(r"[^\d.]", "", str(value))
        if not digits:
            return None
        try:
            return float(digits)
        except ValueError:
            return None

    @staticmethod
    def _reconcile_totals(fields: Dict[str, Any],
                          layout: Dict[str, Any]) -> None:
        """合计行（金额 / 税额 / 价税合计）三者的矛盾仲裁。

        票面上这三个数挤在「合计」行的同一行里，OCR 会把它们的先后次序搞乱。
        vat_0059 的版面文本是「合 计 ¥290.45 ￥1708.55」，于是「金额」那个正则
        把 290.45 当成了金额（税额反而被版面读对了）。版面是按列心取值的，不
        受次序影响，所以当版面能凑出「金额 + 税额 = 价税合计」、而文本锚点那
        一份凑不出时，以版面为准。

        判据只有「自洽」这一条：版面不自洽时什么都不动，免得越权覆盖一个可
        能本来就对的结果。
        """
        total = FieldExtractor._to_number(
            fields.get("total_with_tax") or layout.get("total_with_tax"))
        if not total:
            return

        amount = FieldExtractor._to_number(layout.get("amount"))
        tax = FieldExtractor._to_number(layout.get("tax_amount"))
        if amount is None or tax is None:
            return
        if abs(amount + tax - total) > 0.02:
            return

        # 版面自洽。文本锚点那一份很可能把合计行里靠右的数当成了金额。
        for key, value in (("amount", amount), ("tax_amount", tax)):
            if fields.get(key) is not None:
                fields[key] = f"{value:.2f}"

        # 税率格没读出内容时，用 税额 / 金额 反推。判据卡得很紧：必须落在
        # 一个法定税率上（0.2 个百分点以内），否则宁可留空 —— 猜一个税率
        # 比留空更糟，下游的税额验算会拿它当真。
        if not fields.get("tax_rate") and amount:
            rate = tax / amount * 100.0
            hit = min(FieldExtractor.TAX_RATES, key=lambda r: abs(r - rate))
            if abs(hit - rate) < 0.2:
                fields["tax_rate"] = str(int(hit))

    def _extract_item_lines(self, lines: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """按版面把明细表逐行读出来（货物名称 / 规格 / 单位 / 数量 / 单价 …）。

        ``lines`` 是已剔除装饰性元素（印章、密码区、竖排边栏）的盒子，
        结构与 :mod:`item_layout` 的要求一致。
        """
        try:
            return extract_item_lines(list(lines))
        except Exception as exc:  # 版面抽取是增强，任何异常都不应该打断主流程
            logger.warning(f"明细行抽取失败，降级为无明细: {exc}")
            return [], {"warnings": [f"exception: {exc}"]}

    def _clean_chinese_spaces(self, text: str) -> str:
        """Remove extra spaces between Chinese characters.

        PDF text extraction often adds spaces between Chinese characters
        due to font/layout issues. This function removes those spaces
        while preserving spaces around English text.

        Examples:
            "深圳那时餐 饮管理有限公司" → "深圳那时餐饮管理有限公司"
            "餐 饮服 务" → "餐饮服务"
        """
        if not text:
            return text

        # Pattern: Chinese char + space(s) + Chinese char
        # Remove spaces between Chinese characters
        # Chinese Unicode range: \u4e00-\u9fff (CJK Unified Ideographs)
        cleaned = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', text)

        # May need multiple passes for consecutive spaces
        while re.search(r'[\u4e00-\u9fff]\s+[\u4e00-\u9fff]', cleaned):
            cleaned = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', cleaned)

        return cleaned.strip()

    def _clean_value(self, value: str, field_name: str) -> Any:
        """Clean and convert field value to appropriate type."""
        if not value:
            return None

        # Parse date fields
        if field_name == 'issue_date':
            return self._parse_date(value)

        # Parse currency fields
        if field_name in ['total_with_tax', 'amount', 'tax_amount']:
            return self._parse_currency(value)

        # Clean Chinese text fields (remove extra spaces)
        if field_name in ['buyer_name', 'seller_name', 'item_name']:
            return self._clean_chinese_spaces(value)

        return value

    def _parse_date(self, date_str: str) -> Optional[str]:
        """Parse date string to ISO format (supports Chinese and English)."""
        if not date_str:
            return None

        # Handle Chinese date format (年月日)
        date_str = date_str.replace("年", "-").replace("月", "-").replace("日", "")
        date_str = date_str.replace("/", "-")

        # Try various date formats
        formats = [
            "%Y-%m-%d",           # 2018-08-13
            "%m-%d-%Y",           # 08-13-2018
            "%d-%m-%Y",           # 13-08-2018
            "%B %d, %Y",          # August 13, 2018
            "%b %d, %Y",          # Aug 13, 2018
            "%B %d %Y",           # August 13 2018
            "%b %d %Y",           # Aug 13 2018
        ]

        for fmt in formats:
            try:
                parsed = datetime.strptime(date_str.strip(), fmt)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        return date_str

    def _parse_currency(self, currency_str: str) -> Optional[str]:
        """Parse currency string, return as string to preserve precision."""
        if not currency_str:
            return None

        # Remove currency symbols and formatting
        cleaned = re.sub(r'[$¥￥,\s]', '', currency_str)

        try:
            float(cleaned)
            return cleaned
        except ValueError:
            return currency_str


# Singleton instances
_ocr_service: Optional[OCRService] = None
_field_extractor: Optional[FieldExtractor] = None


def get_ocr_service() -> OCRService:
    """Get thread-safe OCRService singleton."""
    global _ocr_service
    if _ocr_service is None:
        with _ocr_lock:
            # Double-check after acquiring lock
            if _ocr_service is None:
                _ocr_service = OCRService()
    return _ocr_service


def get_field_extractor() -> FieldExtractor:
    """Get thread-safe FieldExtractor singleton."""
    global _field_extractor
    if _field_extractor is None:
        with _extractor_lock:
            # Double-check after acquiring lock
            if _field_extractor is None:
                _field_extractor = FieldExtractor()
    return _field_extractor
