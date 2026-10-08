"""Invoice processing service."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, delete

from app.models.invoice import (
    Invoice, InvoiceItem, OcrResult, LlmResult, ParsingDiff, InvoiceStatus,
)
from app.services.ocr_service import (
    get_ocr_service, get_field_extractor, normalize_tax_rate,
)
from app.services.llm_service import get_llm_service
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Separate thread pools for OCR and LLM to maximize throughput
# OCR is CPU-bound (image processing), LLM is I/O-bound (API calls)
# Using separate pools prevents one from blocking the other
_ocr_executor = ThreadPoolExecutor(max_workers=settings.ocr_max_workers)
_llm_executor = ThreadPoolExecutor(max_workers=settings.llm_max_workers)
logger.info(f"Initialized thread pools: OCR={settings.ocr_max_workers}, LLM={settings.llm_max_workers}")

# Fields to compare between OCR and LLM
COMPARABLE_FIELDS = [
    'invoice_number',
    'issue_date',
    'buyer_name',
    'buyer_tax_id',
    'seller_name',
    'seller_tax_id',
    'item_name',
    'total_with_tax',
    'amount',
    'tax_amount',
    'tax_rate',
]
# 明细行不走 COMPARABLE_FIELDS：那份列表会被 _reset_extracted_fields 拿来
# 对 Invoice 实例做 setattr，而 items 是关系属性，赋 None 只会把集合清空、
# 旧明细行还留在库里（delete-orphan 不触发）。明细单独走下面的解析流程。

# 明细行里允许入库的字段（按票面列顺序）。数组里的额外键一律丢弃。
ITEM_FIELDS = ('item_name', 'specification', 'unit', 'quantity',
               'unit_price', 'amount', 'tax_rate', 'tax_amount')

#: 确认一张发票必须齐备的字段 —— 「必填」的定义在这里，不在前端。
#:
#: 缺任何一项都无法确认（routers/invoices.py 的 confirm 接口据此返回 400）。
#: 前端 InvoiceDetailPage.tsx 里的 REQUIRED_FIELDS 是这份清单的另一个副本，
#: 负责画星号和提前拦截；两边改一处就要同步另一处，否则会出现「页面上没星号、
#: 点确认却报错」。
CRITICAL_FIELDS = (
    'invoice_number',
    'issue_date',
    'total_with_tax',
    'buyer_name',
    'buyer_tax_id',
    'seller_name',
    'seller_tax_id',
    'item_name',
)


def _reset_extracted_fields(invoice: Invoice) -> None:
    """Reset extracted fields to avoid stale values on reprocess."""
    for field_name in COMPARABLE_FIELDS:
        setattr(invoice, field_name, None)


def _run_ocr(file_data: bytes, file_type: str) -> Tuple[str, float, Dict[str, Any]]:
    """Run OCR processing in a separate thread.

    Args:
        file_data: Raw file bytes
        file_type: File type (pdf, jpg, png, etc.)

    Returns:
        Tuple of (raw_text, confidence, extracted_fields)
    """
    ocr_service = get_ocr_service()
    extractor = get_field_extractor()

    if file_type == 'pdf':
        raw_text, confidence, ocr_lines = ocr_service.process_pdf(file_data)
    else:
        raw_text, confidence, ocr_lines = ocr_service.process_image(file_data)

    ocr_fields = extractor.extract_fields(raw_text, ocr_lines)
    return raw_text, confidence, ocr_fields


def _run_llm_vision(file_data: bytes, file_type: str) -> Dict[str, Any]:
    """Run LLM vision parsing in a separate thread.

    Args:
        file_data: Raw file bytes
        file_type: File type (pdf, jpg, png, etc.)

    Returns:
        Dictionary of extracted fields
    """
    llm_service = get_llm_service()

    if not llm_service.is_available or not llm_service.supports_vision():
        return {}

    # Determine MIME type
    mime_map = {
        'pdf': 'application/pdf',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
    }
    mime_type = mime_map.get(file_type, 'image/png')

    # For PDF, we need to convert first page to image
    if file_type == 'pdf':
        try:
            from pdf2image import convert_from_bytes
            from io import BytesIO

            # Use higher DPI (300) for better text recognition in PDFs
            # Some PDFs have text rendered as graphics which need higher resolution
            images = convert_from_bytes(file_data, dpi=300, first_page=1, last_page=1)
            if images:
                # Convert PIL Image to bytes with high quality
                buffer = BytesIO()
                images[0].save(buffer, format='PNG', optimize=False)
                file_data = buffer.getvalue()
                mime_type = 'image/png'
                logger.info(f"PDF converted to image: {images[0].size[0]}x{images[0].size[1]} pixels, {len(file_data)} bytes")
            else:
                logger.warning("Failed to convert PDF to image for LLM vision")
                return {}
        except Exception as e:
            logger.error(f"PDF to image conversion failed: {e}")
            return {}

    return llm_service.parse_invoice_from_image(file_data, mime_type)


def _has_meaningful_fields(fields: Dict[str, Any]) -> bool:
    """Check if parsed fields contain any meaningful values."""
    if not fields:
        return False
    for value in fields.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


async def process_invoice(invoice_id: int, db: AsyncSession) -> bool:
    """Process an invoice: run OCR and LLM vision in parallel, then compare results.

    This function runs OCR and LLM vision parsing in parallel for better performance
    and more accurate results. The LLM analyzes the image directly instead of
    relying on OCR text.

    Args:
        invoice_id: ID of the invoice to process
        db: Database session

    Returns:
        True if processing succeeded, False otherwise
    """
    try:
        # Get invoice
        # selectinload 不是可选项：invoice.items 上的 delete-orphan 让
        #「给集合赋值」这一步必须先读到原有明细才能算出差异，而异步 session
        # 里的隐式懒加载会抛 MissingGreenlet —— 也就是只要这张票之前解析出过
        # 明细，重新解析就会崩。预加载之后赋值就不再触发懒加载。
        query = (select(Invoice)
                 .options(selectinload(Invoice.items))
                 .where(Invoice.id == invoice_id))
        result = await db.execute(query)
        invoice = result.scalar_one_or_none()

        if not invoice:
            logger.error(f"Invoice {invoice_id} not found")
            return False

        # Delete existing OCR, LLM, and diff results for reprocessing
        await db.execute(delete(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id))
        await db.execute(delete(LlmResult).where(LlmResult.invoice_id == invoice_id))
        await db.execute(delete(OcrResult).where(OcrResult.invoice_id == invoice_id))
        logger.info(f"Cleared existing processing results for invoice {invoice_id}")

        # Run OCR and LLM vision in PARALLEL using separate thread pools
        loop = asyncio.get_running_loop()

        # Create tasks for parallel execution
        # OCR uses CPU-bound pool, LLM uses I/O-bound pool
        ocr_task = loop.run_in_executor(
            _ocr_executor, _run_ocr, invoice.file_data, invoice.file_type
        )
        llm_task = loop.run_in_executor(
            _llm_executor, _run_llm_vision, invoice.file_data, invoice.file_type
        )

        # Wait for both tasks to complete
        logger.info(f"Running OCR and LLM vision in parallel for invoice {invoice_id}")
        ocr_result_data, llm_fields = await asyncio.gather(ocr_task, llm_task)

        # Unpack OCR results
        raw_text, confidence, ocr_fields = ocr_result_data
        has_llm = _has_meaningful_fields(llm_fields)

        logger.info(f"OCR completed: {len(ocr_fields)} fields extracted")
        logger.info(f"LLM vision completed: {len(llm_fields)} fields extracted (has_llm={has_llm})")

        # Save OCR result
        ocr_result = OcrResult(
            invoice_id=invoice_id,
            raw_text=raw_text,
            invoice_number=ocr_fields.get('invoice_number'),
            issue_date=ocr_fields.get('issue_date'),
            buyer_name=ocr_fields.get('buyer_name'),
            buyer_tax_id=ocr_fields.get('buyer_tax_id'),
            seller_name=ocr_fields.get('seller_name'),
            seller_tax_id=ocr_fields.get('seller_tax_id'),
            item_name=ocr_fields.get('item_name'),
            total_with_tax=ocr_fields.get('total_with_tax'),
            amount=ocr_fields.get('amount'),
            tax_amount=ocr_fields.get('tax_amount'),
            tax_rate=ocr_fields.get('tax_rate'),
        )
        db.add(ocr_result)

        # Save LLM result if available
        if has_llm:
            llm_result = LlmResult(
                invoice_id=invoice_id,
                invoice_number=llm_fields.get('invoice_number'),
                issue_date=llm_fields.get('issue_date'),
                buyer_name=llm_fields.get('buyer_name'),
                buyer_tax_id=llm_fields.get('buyer_tax_id'),
                seller_name=llm_fields.get('seller_name'),
                seller_tax_id=llm_fields.get('seller_tax_id'),
                item_name=llm_fields.get('item_name'),
                total_with_tax=llm_fields.get('total_with_tax'),
                amount=llm_fields.get('amount'),
                tax_amount=llm_fields.get('tax_amount'),
                tax_rate=llm_fields.get('tax_rate'),
            )
            db.add(llm_result)
        else:
            logger.info(f"LLM vision not available - invoice {invoice_id} using OCR-only flow")

        # Compare OCR and LLM results, create diffs
        final_fields, diffs = _compare_and_resolve(ocr_fields, llm_fields, has_llm)

        # Clear extracted fields so missing values don't keep stale data
        _reset_extracted_fields(invoice)

        # Save parsing diffs
        for diff in diffs:
            parsing_diff = ParsingDiff(
                invoice_id=invoice_id,
                field_name=diff['field_name'],
                ocr_value=diff['ocr_value'],
                llm_value=diff['llm_value'],
                final_value=diff['final_value'],
                source=diff['source'],
                resolved=0 if diff['needs_review'] else 1,
            )
            db.add(parsing_diff)

        # Update invoice with final data
        _update_invoice_from_fields(invoice, final_fields)
        _sync_invoice_items(invoice, ocr_fields, llm_fields, has_llm, final_fields)

        # 冲突与缺失只用于日志：状态不再按它们分叉（见下面 status 的赋值）。
        has_conflicts = any(d['needs_review'] for d in diffs)

        missing_fields = [f for f in CRITICAL_FIELDS if not final_fields.get(f)]
        if missing_fields:
            logger.warning(f"Invoice {invoice_id} missing critical fields: {missing_fields}")
        if has_conflicts:
            logger.warning(f"Invoice {invoice_id} has unresolved conflicts")

        # 解析完成一律「待审核」。
        #
        # 确认这张票是人的动作，不是解析的副产品。原来这里是「无冲突且必填齐全
        # 就置 已确认」，于是刚上传的票一解析完就显示成已确认 —— 用户一眼没看
        # 就被算作审核通过。重新解析同样回到待审核：机器刚重写过票面字段，
        # 上一次人工确认已经失效。
        invoice.status = InvoiceStatus.REVIEWING

        await db.commit()
        logger.info(
            f"Invoice {invoice_id} processed successfully "
            f"(conflicts={has_conflicts}, missing_critical={len(missing_fields)})"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to process invoice {invoice_id}: {e}")
        await db.rollback()
        return False


def _compare_and_resolve(
    ocr_fields: Dict[str, Any],
    llm_fields: Dict[str, Any],
    has_llm: bool
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Compare OCR and LLM results, resolve differences.

    ``source`` records *why* a value was chosen, and each value is distinct
    on purpose -- the UI and the audit trail both key off it:

    ``ocr``       LLM never ran; the whole invoice is OCR-only.
    ``matched``   both engines produced the same value.
    ``conflict``  both produced values and they differ -> needs review.
    ``llm_only``  LLM produced a value, OCR did not.
    ``ocr_only``  OCR produced a value, LLM did not. This is the normal
                  state for a clean invoice, not a disagreement: it must
                  not be presented as a conflict.

    Args:
        ocr_fields: Fields extracted by OCR
        llm_fields: Fields extracted by LLM
        has_llm: Whether LLM results are available

    Returns:
        Tuple of (final_fields, diffs_list)
    """
    final_fields = {}
    diffs = []

    for field_name in COMPARABLE_FIELDS:
        # 归一化按字段走：税率的「11」和「11%」是同一个值，其余字段仍是
        # 朴素的字符串比对（金额另有 _values_are_equal 做数值等价）。
        ocr_value = _normalize_field_value(field_name, ocr_fields.get(field_name))
        llm_value = (_normalize_field_value(field_name, llm_fields.get(field_name))
                     if has_llm else None)

        # Determine final value and whether review is needed
        if not has_llm:
            # No LLM available, use OCR value directly
            final_value = ocr_value
            source = 'ocr'
            needs_review = False
        elif _values_are_equal(field_name, ocr_value, llm_value):
            # Values match (including numeric equivalence like 330.00 == 330)
            final_value = ocr_value or llm_value
            source = 'matched'
            needs_review = False
        elif ocr_value and llm_value:
            # Both have values but they differ - needs manual review
            final_value = None  # Leave blank for manual review
            source = 'conflict'
            needs_review = True
        elif llm_value and not ocr_value:
            # LLM found something OCR missed - prefer LLM. "only" is what
            # separates this from 'llm' (which means the LLM never ran at
            # all, see the `not has_llm` branch above): the two cases used to
            # share one value, so nothing downstream could tell an
            # unverified single-source value from an OCR-only batch.
            final_value = llm_value
            source = 'llm_only'
            needs_review = False
        else:
            # OCR found something LLM missed - prefer OCR
            final_value = ocr_value
            source = 'ocr_only'
            needs_review = False

        final_fields[field_name] = final_value

        # Record diff if there's a discrepancy or both have values
        if has_llm and (ocr_value or llm_value):
            diffs.append({
                'field_name': field_name,
                'ocr_value': ocr_value,
                'llm_value': llm_value,
                'final_value': final_value,
                'source': source,
                'needs_review': needs_review,
            })

    return final_fields, diffs


def _normalize_value(value: Any) -> Optional[str]:
    """Normalize a value to string for comparison."""
    if value is None:
        return None
    value_str = str(value).strip()
    if not value_str:
        return None
    return value_str


def _normalize_field_value(field_name: str, value: Any) -> Optional[str]:
    """按字段语义归一化取值，供「双源比对」和「写库」共用。

    目前只有税率需要额外处理：带不带百分号是同一个税率，而 OCR 有两条路径
    会把「%」丢掉（见 :func:`normalize_tax_rate`）。不归一化的话，OCR 的
    「11」和 LLM 的「11%」会被 ``_values_are_equal`` 判成不等 -> 记成
    conflict -> 用户每张票都得手动裁决一次这一项。
    """
    normalized = _normalize_value(value)
    if field_name == 'tax_rate':
        return normalize_tax_rate(normalized)
    return normalized


# Fields that should be compared as numbers
NUMERIC_FIELDS = ['total_with_tax', 'amount', 'tax_amount']


def _values_are_equal(field_name: str, value1: Optional[str], value2: Optional[str]) -> bool:
    """Compare two values, using numeric comparison for numeric fields.

    Args:
        field_name: Name of the field being compared
        value1: First value (normalized string)
        value2: Second value (normalized string)

    Returns:
        True if values are considered equal
    """
    # If both are None or empty, they're equal
    if not value1 and not value2:
        return True

    # If only one is None/empty, they're not equal
    if not value1 or not value2:
        return False

    # For numeric fields, compare as numbers
    if field_name in NUMERIC_FIELDS:
        try:
            from decimal import Decimal, InvalidOperation
            # Remove any currency symbols and whitespace
            clean1 = value1.replace('¥', '').replace('￥', '').replace(',', '').strip()
            clean2 = value2.replace('¥', '').replace('￥', '').replace(',', '').strip()
            num1 = Decimal(clean1)
            num2 = Decimal(clean2)
            return num1 == num2
        except (InvalidOperation, ValueError):
            # Fall back to string comparison if not valid numbers
            pass

    # Default to string comparison
    return value1 == value2


def _update_invoice_from_fields(invoice: Invoice, fields: dict) -> None:
    """Update invoice fields from extracted data."""
    from datetime import datetime
    from decimal import Decimal

    if fields.get('invoice_number'):
        invoice.invoice_number = fields['invoice_number']

    if fields.get('issue_date'):
        try:
            invoice.issue_date = datetime.strptime(fields['issue_date'], '%Y-%m-%d').date()
        except ValueError:
            pass

    if fields.get('buyer_name'):
        invoice.buyer_name = fields['buyer_name']

    if fields.get('buyer_tax_id'):
        invoice.buyer_tax_id = fields['buyer_tax_id']

    if fields.get('seller_name'):
        invoice.seller_name = fields['seller_name']

    if fields.get('seller_tax_id'):
        invoice.seller_tax_id = fields['seller_tax_id']

    if fields.get('item_name'):
        invoice.item_name = fields['item_name']

    if fields.get('total_with_tax'):
        try:
            invoice.total_with_tax = Decimal(fields['total_with_tax'])
        except (ValueError, TypeError):
            pass

    if fields.get('amount'):
        try:
            invoice.amount = Decimal(fields['amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_amount'):
        try:
            invoice.tax_amount = Decimal(fields['tax_amount'])
        except (ValueError, TypeError):
            pass

    if fields.get('tax_rate'):
        invoice.tax_rate = fields['tax_rate']


# 明细行里需要转成 Decimal 的字段。
# 不做转换会有真问题：asyncpg 不接受把 numeric 列的绑定参数传成 str，
# 插入时直接抛 InvalidParameterTypeError，而这条错误只在真正 flush 时才冒出来，
# 单元测试里挂个 Invoice 实例是看不到的。
_ITEM_NUMERIC = ('quantity', 'unit_price', 'amount', 'tax_amount')


def _clean_item(raw: Any) -> Optional[Dict[str, Any]]:
    """把一路来源的明细行整理成可入库的字典；不可用的返回 None。"""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in ITEM_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() in ('none', 'null', 'nan'):
            continue
        if key in _ITEM_NUMERIC:
            try:
                out[key] = Decimal(text.replace(',', ''))
            except (ValueError, TypeError, InvalidOperation):
                continue
        elif key == 'tax_rate':
            # 逐行税率同样要带百分号，理由见 normalize_tax_rate：
            # 明细行的值既来自 OCR，也来自用户在明细表里的手输。
            out[key] = normalize_tax_rate(text) or text
        else:
            out[key] = text
    return out or None


def _items_amount_sum(items: List[Dict[str, Any]]) -> float:
    total = 0.0
    for it in items:
        try:
            total += float(str(it.get('amount', '')).replace(',', ''))
        except ValueError:
            continue
    return round(total, 2)


def build_item_rows(raw_items: List[Any]) -> List[Dict[str, Any]]:
    """把一路来源的明细行整理成可入库的字典列表。

    与 :func:`_clean_item` 的分工：后者是逐行清洗，这里是整体的入口 ——
    调用方（明细人工修正接口）只需要拿这个函数的结果去构造 InvoiceItem，
    不必知道数值要转 Decimal、空字符串要当没填这些细节。
    """
    return [c for c in (_clean_item(i) for i in raw_items) if c]


def _pick_items(ocr_items: List[Any], llm_items: List[Any],
                final_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    """在 OCR 明细与 LLM 明细之间挑一份更可信的。

    判据沿用对账时的三角校验：明细金额合计应当等于票面「金额」列
    （= 价税合计 - 税额）。谁更贴合就信谁；两边都贴合或都不贴合时
    取 OCR —— 版面抽取是照着盒子坐标读的，比视觉模型的自由发挥稳。
    """
    ocr_clean = [c for c in (_clean_item(i) for i in ocr_items) if c]
    llm_clean = [c for c in (_clean_item(i) for i in llm_items) if c]

    if not ocr_clean and not llm_clean:
        return []
    if not llm_clean:
        return ocr_clean
    if not ocr_clean:
        return llm_clean

    # 票面上真正的「不含税金额合计」
    try:
        target = float(str(final_fields.get('amount') or '').replace(',', ''))
    except ValueError:
        target = None
    if target is None:
        try:
            grand = float(str(final_fields.get('total_with_tax') or '').replace(',', ''))
            tax = float(str(final_fields.get('tax_amount') or '').replace(',', ''))
            target = round(grand - tax, 2)
        except ValueError:
            target = None

    if target is None:
        return ocr_clean
    d_ocr = abs(_items_amount_sum(ocr_clean) - target)
    d_llm = abs(_items_amount_sum(llm_clean) - target)
    return llm_clean if d_llm < d_ocr else ocr_clean


def _sync_invoice_items(invoice: Invoice, ocr_fields: Dict[str, Any],
                        llm_fields: Dict[str, Any], has_llm: bool,
                        final_fields: Dict[str, Any]) -> int:
    """把明细行同步到 invoice_items。返回写入行数。

    只有本次处理真的产出了明细时才覆盖旧数据；两边都没抽到就保持原样，
    免得重复处理一张票时把之前人工确认过的明细抹掉。
    """
    ocr_items = ocr_fields.get('items') or []
    llm_items = (llm_fields or {}).get('items') or [] if has_llm else []
    picked = _pick_items(ocr_items, llm_items, final_fields)
    # 两边都没产出明细 -> 本次这次处理与明细无关，别动已有数据
    # （重复处理一张人工确认过的票时，不能把明细清掉）
    if not picked:
        return 0
    invoice.items = [
        InvoiceItem(row_no=idx, **it) for idx, it in enumerate(picked, start=1)
    ]
    return len(picked)


def check_llm_available() -> bool:
    """Check if LLM service is available."""
    return get_llm_service().is_available
