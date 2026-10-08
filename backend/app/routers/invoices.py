from typing import Optional, List, Dict, Any
from io import BytesIO
from datetime import date
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from decimal import Decimal

from app.database import get_db
from app.models.invoice import (
    Invoice, InvoiceItem, OcrResult, LlmResult, ParsingDiff, InvoiceStatus,
)
from app.schemas.invoice import (
    InvoiceResponse, InvoiceListResponse, InvoiceDetailResponse,
    InvoiceUpdate, BatchUpdateRequest, BatchDeleteRequest, StatisticsResponse, UploadResponse,
    ResolveDiffRequest, RejectInvoiceRequest
)
from app.config import get_settings
from app.services.audit_service import log_audit_no_commit, get_client_info
from app.services.invoice_service import build_item_rows, CRITICAL_FIELDS
from app.services.ocr_service import normalize_tax_rate
from app.rate_limit import limiter

settings = get_settings()
router = APIRouter()


def _parse_invoice_ids(invoice_ids: Optional[str]) -> Optional[List[int]]:
    if not invoice_ids:
        return None
    ids: List[int] = []
    for raw_id in invoice_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            ids.append(int(raw_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="发票ID必须为整数") from exc
    if not ids:
        raise HTTPException(status_code=400, detail="发票ID不能为空")
    return ids


def _parse_date_param(value: Optional[str], field_name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} 日期格式无效，应为 YYYY-MM-DD") from exc


@router.post("/upload", response_model=List[UploadResponse])
@limiter.limit("10/minute")
async def upload_invoices(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db)
):
    """上传发票文件 (支持多文件)，上传后异步触发OCR解析"""
    results = []
    invoice_ids_to_process = []
    client_info = get_client_info(request)

    for file in files:
        # Validate file type
        ext = file.filename.split(".")[-1].lower() if file.filename else ""
        if ext not in settings.allowed_extensions:
            results.append(UploadResponse(
                id=0,
                file_name=file.filename or "unknown",
                status="error",
                message=f"不支持的文件类型: {ext}"
            ))
            continue

        # Read file content
        content = await file.read()

        # Validate file size
        if len(content) > settings.max_file_size:
            results.append(UploadResponse(
                id=0,
                file_name=file.filename or "unknown",
                status="error",
                message=f"文件过大，最大支持 {settings.max_file_size // 1024 // 1024}MB"
            ))
            continue

        # Create invoice record with UPLOADED status (not yet processed)
        invoice = Invoice(
            file_name=file.filename or "unknown",
            file_type=ext,
            file_data=content,
            status=InvoiceStatus.UPLOADED
        )
        db.add(invoice)
        await db.flush()

        invoice_ids_to_process.append(invoice.id)

        # Audit log for upload
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="upload",
            new_value={"file_name": invoice.file_name, "file_type": ext, "file_size": len(content)},
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )

        results.append(UploadResponse(
            id=invoice.id,
            file_name=invoice.file_name,
            status="success",
            message="上传成功，等待解析"
        ))

    await db.commit()

    # Schedule background processing for each uploaded invoice
    for invoice_id in invoice_ids_to_process:
        background_tasks.add_task(process_invoice_background, invoice_id)

    return results


async def process_invoice_background(invoice_id: int, max_retries: int = 3):
    """Background task to process an invoice with OCR/LLM.

    Implements exponential backoff retry logic for transient failures.
    Total attempts = 1 (initial) + max_retries, with delays of 2, 4, 8 seconds
    between retries.

    Args:
        invoice_id: ID of the invoice to process
        max_retries: Maximum number of retries after the initial attempt (default: 3,
            resulting in up to 4 total attempts)
    """
    from app.services.invoice_service import process_invoice as do_process
    from app.database import async_session_maker
    from app.services.audit_service import log_audit
    import logging
    import asyncio
    logger = logging.getLogger(__name__)

    retry_count = 0
    last_error = None

    while retry_count <= max_retries:
        async with async_session_maker() as db:
            try:
                # Update status to PROCESSING
                query = select(Invoice).where(Invoice.id == invoice_id)
                result = await db.execute(query)
                invoice = result.scalar_one_or_none()

                if not invoice:
                    logger.error(f"Invoice {invoice_id} not found")
                    return

                invoice.status = InvoiceStatus.PROCESSING
                await db.commit()

                logger.info(f"Background processing invoice {invoice_id} (attempt {retry_count + 1}/{max_retries + 1})")
                success = await do_process(invoice_id, db)

                if success:
                    logger.info(f"Invoice {invoice_id} processing completed successfully")
                    # Log successful processing
                    await log_audit(
                        db=db,
                        entity_type="invoice",
                        entity_id=invoice_id,
                        action="process_complete",
                        new_value={"status": "success", "attempts": retry_count + 1}
                    )
                    return
                else:
                    last_error = "Processing returned false"
                    logger.warning(f"Invoice {invoice_id} processing returned false")

            except Exception as e:
                last_error = str(e)
                logger.error(f"Failed to process invoice {invoice_id} (attempt {retry_count + 1}): {e}")
                await db.rollback()

        retry_count += 1

        if retry_count <= max_retries:
            # Exponential backoff: 2^retry_count seconds (2, 4, 8 seconds)
            delay = 2 ** retry_count
            logger.info(f"Retrying invoice {invoice_id} in {delay} seconds...")
            await asyncio.sleep(delay)

    # All retries exhausted - mark as failed
    async with async_session_maker() as db:
        try:
            query = select(Invoice).where(Invoice.id == invoice_id)
            result = await db.execute(query)
            invoice = result.scalar_one_or_none()

            if invoice:
                # Set status back to UPLOADED so user can retry manually
                invoice.status = InvoiceStatus.UPLOADED
                await db.commit()

                # Log failed processing
                await log_audit(
                    db=db,
                    entity_type="invoice",
                    entity_id=invoice_id,
                    action="process_failed",
                    new_value={"error": last_error, "attempts": max_retries + 1}
                )

            logger.error(f"Invoice {invoice_id} processing failed after {max_retries + 1} attempts: {last_error}")
        except Exception as e:
            logger.error(f"Failed to update invoice {invoice_id} status after retry exhaustion: {e}")


@router.get("", response_model=InvoiceListResponse)
async def list_invoices(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    invoice_number: Optional[str] = Query(None, description="发票号码模糊搜索"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    db: AsyncSession = Depends(get_db)
):
    """获取发票列表"""
    query = select(Invoice)

    # Apply filters
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    # 之前这里没有 invoice_number 过滤，而前端搜索框一直在传这个参数 ——
    # FastAPI 会静默丢掉未声明的查询参数，于是「搜不到」时页面照样列出全部发票。
    # 用模糊匹配而不是精确匹配：搜索框里打「9000001」应该能命中「90000031」。
    if invoice_number and invoice_number.strip():
        query = query.where(Invoice.invoice_number.ilike(f"%{invoice_number.strip()}%"))
    if start_date:
        try:
            start_date_obj = date.fromisoformat(start_date)
            query = query.where(Invoice.issue_date >= start_date_obj)
        except ValueError:
            pass  # Invalid date format, skip filter
    if end_date:
        try:
            end_date_obj = date.fromisoformat(end_date)
            query = query.where(Invoice.issue_date <= end_date_obj)
        except ValueError:
            pass  # Invalid date format, skip filter

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0

    # Apply pagination
    query = query.order_by(Invoice.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    invoices = result.scalars().all()

    return InvoiceListResponse(
        items=[InvoiceResponse.model_validate(inv) for inv in invoices],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics(
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    db: AsyncSession = Depends(get_db)
):
    """获取发票统计数据"""
    query = select(Invoice)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)

    result = await db.execute(query)
    invoices = result.scalars().all()

    count = len(invoices)
    total_amount = sum((inv.amount or Decimal(0)) for inv in invoices)
    total_tax = sum((inv.tax_amount or Decimal(0)) for inv in invoices)
    total_with_tax = sum((inv.total_with_tax or Decimal(0)) for inv in invoices)

    return StatisticsResponse(
        count=count,
        total_amount=total_amount,
        total_tax=total_tax,
        total_with_tax=total_with_tax
    )


@router.get("/{invoice_id}", response_model=InvoiceDetailResponse)
async def get_invoice(
    invoice_id: int,
    db: AsyncSession = Depends(get_db)
):
    """获取发票详情"""
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Load related data
    ocr_query = select(OcrResult).where(OcrResult.invoice_id == invoice_id)
    ocr_result = await db.execute(ocr_query)
    ocr = ocr_result.scalar_one_or_none()

    llm_query = select(LlmResult).where(LlmResult.invoice_id == invoice_id)
    llm_result = await db.execute(llm_query)
    llm = llm_result.scalar_one_or_none()

    diff_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    diff_result = await db.execute(diff_query)
    diffs = diff_result.scalars().all()

    # 明细行。invoices.items 上的 order_by="InvoiceItem.row_no" 已经在
    # 模型层排好序，这里不用再排一遍。
    item_query = select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
    item_result = await db.execute(item_query)
    invoice_items = item_result.scalars().all()

    # Build response dict from invoice to avoid lazy loading issues
    invoice_dict = {
        "id": invoice.id,
        "file_name": invoice.file_name,
        "file_type": invoice.file_type,
        "status": invoice.status,
        "owner": invoice.owner,
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date,
        "buyer_name": invoice.buyer_name,
        "buyer_tax_id": invoice.buyer_tax_id,
        "seller_name": invoice.seller_name,
        "seller_tax_id": invoice.seller_tax_id,
        "item_name": invoice.item_name,
        "total_with_tax": invoice.total_with_tax,
        "specification": invoice.specification,
        "unit": invoice.unit,
        "quantity": invoice.quantity,
        "unit_price": invoice.unit_price,
        "amount": invoice.amount,
        "tax_rate": invoice.tax_rate,
        "tax_amount": invoice.tax_amount,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
        "ocr_result": ocr,
        "llm_result": llm,
        "parsing_diffs": list(diffs),
        "items": list(invoice_items),
    }

    return InvoiceDetailResponse.model_validate(invoice_dict)


@router.get("/{invoice_id}/file")
async def get_invoice_file(
    invoice_id: int,
    db: AsyncSession = Depends(get_db)
):
    """获取发票原始文件"""
    from fastapi.responses import Response
    from urllib.parse import quote

    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    content_type_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png"
    }

    # URL-encode filename for Content-Disposition header (RFC 5987)
    encoded_filename = quote(invoice.file_name)

    return Response(
        content=invoice.file_data,
        media_type=content_type_map.get(invoice.file_type, "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )


@router.put("/{invoice_id}", response_model=InvoiceResponse)
async def update_invoice(
    invoice_id: int,
    update_data: InvoiceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """更新发票信息（含明细行的人工修正）"""
    # selectinload 不是可选项：下面给 invoice.items 整体赋值时，delete-orphan
    # 级联要求 SQLAlchemy 先读到原集合才能算出差异，异步 session 里的隐式懒
    # 加载会抛 MissingGreenlet —— 也就是「第二次修改同一张票的明细」必崩。
    query = (select(Invoice)
             .options(selectinload(Invoice.items))
             .where(Invoice.id == invoice_id))
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Capture old values for audit
    update_dict = update_data.model_dump(exclude_unset=True)
    # 明细单独处理，不能走下面那圈 setattr：它是对关系集合的全量替换，而且
    # 它的值是嵌套 dict，JSON 序列化不进审计日志。pop 出来顺带把两个语义
    # 分开 —— 键不存在 = 本次不动明细，[] = 用户把明细清空了。
    items_payload = update_dict.pop("items", None)

    old_values = {key: getattr(invoice, key) for key in update_dict.keys()}
    # Convert non-serializable types
    for key, value in old_values.items():
        if hasattr(value, 'value'):  # Enum
            old_values[key] = value.value
        elif hasattr(value, 'isoformat'):  # Date/DateTime
            old_values[key] = value.isoformat()
        elif isinstance(value, Decimal):
            old_values[key] = str(value)

    # 手输的税率也要落到与 OCR 相同的写法上（带百分号），否则「表单里填 11」
    # 存进去就是「11」，下次解析又比对不上。见 normalize_tax_rate。
    if 'tax_rate' in update_dict:
        update_dict['tax_rate'] = normalize_tax_rate(update_dict['tax_rate'])

    for key, value in update_dict.items():
        setattr(invoice, key, value)

    if items_payload is not None:
        rows = build_item_rows(items_payload)
        old_values["items"] = f"{len(invoice.items)} 行"
        invoice.items = [
            InvoiceItem(row_no=idx, **row) for idx, row in enumerate(rows, start=1)
        ]

    # Audit log
    client_info = get_client_info(request)
    new_values = update_dict.copy()
    if items_payload is not None:
        # 只记行数：明细逐格的新旧值留在 invoice_items 里，审计日志记一份
        # 「什么时候被人工改过几行」就够定位问题了。
        new_values["items"] = f"{len(rows)} 行（人工修正）"
    for key, value in new_values.items():
        if hasattr(value, 'value'):  # Enum
            new_values[key] = value.value
        elif hasattr(value, 'isoformat'):  # Date/DateTime
            new_values[key] = value.isoformat()
        elif isinstance(value, Decimal):
            new_values[key] = str(value)

    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="update",
        old_value=old_values,
        new_value=new_values,
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()
    await db.refresh(invoice)

    return InvoiceResponse.model_validate(invoice)


@router.post("/batch-update")
@limiter.limit("30/minute")
async def batch_update_invoices(
    request: Request,
    batch_request: BatchUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """批量更新发票状态/归属人"""
    query = select(Invoice).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()

    client_info = get_client_info(request)
    updated_count = 0
    for invoice in invoices:
        old_values = {}
        new_values = {}
        if batch_request.status is not None:
            old_values["status"] = invoice.status.value if invoice.status else None
            invoice.status = batch_request.status
            new_values["status"] = batch_request.status.value
        if batch_request.owner is not None:
            old_values["owner"] = invoice.owner
            invoice.owner = batch_request.owner
            new_values["owner"] = batch_request.owner
        updated_count += 1

        # Audit log for each invoice
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="batch_update",
            old_value=old_values,
            new_value=new_values,
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )

    await db.commit()

    return {
        "message": f"成功更新 {updated_count} 张发票",
        "updated_count": updated_count
    }


@router.post("/batch-delete")
@limiter.limit("20/minute")
async def batch_delete_invoices(
    request: Request,
    batch_request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db)
):
    """批量删除发票及其关联数据"""
    if not batch_request.invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要删除的发票")

    # Query invoices to delete
    query = select(Invoice).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()

    if not invoices:
        raise HTTPException(status_code=404, detail="未找到要删除的发票")

    client_info = get_client_info(request)
    deleted_count = 0
    for invoice in invoices:
        # Audit log for each deletion
        await log_audit_no_commit(
            db=db,
            entity_type="invoice",
            entity_id=invoice.id,
            action="delete",
            old_value={"file_name": invoice.file_name, "invoice_number": invoice.invoice_number},
            ip_address=client_info.get("ip_address"),
            user_agent=client_info.get("user_agent"),
        )
        await db.delete(invoice)
        deleted_count += 1

    await db.commit()

    return {
        "message": f"成功删除 {deleted_count} 张发票",
        "deleted_count": deleted_count
    }


@router.post("/batch-reprocess")
@limiter.limit("5/minute")
async def batch_reprocess_invoices(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_request: BatchDeleteRequest,  # Reuse for invoice_ids
    db: AsyncSession = Depends(get_db)
):
    """批量重新解析发票（清除旧的OCR/LLM结果，重新处理）"""
    import logging
    logger = logging.getLogger(__name__)

    if not batch_request.invoice_ids:
        raise HTTPException(status_code=400, detail="请选择要重新解析的发票")

    # Query invoices to reprocess
    query = select(Invoice).where(Invoice.id.in_(batch_request.invoice_ids))
    result = await db.execute(query)
    invoices = result.scalars().all()

    if not invoices:
        raise HTTPException(status_code=404, detail="未找到要重新解析的发票")

    # Clear old parsing results and reset invoice fields
    for invoice in invoices:
        # Delete old OCR results
        ocr_query = select(OcrResult).where(OcrResult.invoice_id == invoice.id)
        ocr_result = await db.execute(ocr_query)
        for ocr in ocr_result.scalars().all():
            await db.delete(ocr)

        # Delete old LLM results
        llm_query = select(LlmResult).where(LlmResult.invoice_id == invoice.id)
        llm_result = await db.execute(llm_query)
        for llm in llm_result.scalars().all():
            await db.delete(llm)

        # Delete old parsing diffs
        diff_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice.id)
        diff_result = await db.execute(diff_query)
        for diff in diff_result.scalars().all():
            await db.delete(diff)

        # Reset invoice fields
        invoice.invoice_number = None
        invoice.issue_date = None
        invoice.buyer_name = None
        invoice.buyer_tax_id = None
        invoice.seller_name = None
        invoice.seller_tax_id = None
        invoice.item_name = None
        invoice.total_with_tax = None
        invoice.amount = None
        invoice.tax_amount = None
        invoice.tax_rate = None
        invoice.status = InvoiceStatus.UPLOADED

    await db.commit()
    logger.info(f"Cleared old parsing results for {len(invoices)} invoices, scheduling reprocess")

    # Schedule background processing
    for invoice in invoices:
        background_tasks.add_task(process_invoice_background, invoice.id)

    return {
        "message": f"已清除 {len(invoices)} 张发票的旧解析结果，正在重新解析",
        "count": len(invoices)
    }


@router.post("/{invoice_id}/process")
async def process_invoice(
    invoice_id: int,
    db: AsyncSession = Depends(get_db)
):
    """处理发票：运行OCR解析"""
    from app.services.invoice_service import process_invoice as do_process

    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    success = await do_process(invoice_id, db)

    if success:
        return {"message": "解析成功", "invoice_id": invoice_id}
    else:
        raise HTTPException(status_code=500, detail="解析失败")


@router.delete("/{invoice_id}")
async def delete_invoice(
    invoice_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """删除发票"""
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Audit log
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="delete",
        old_value={"file_name": invoice.file_name, "invoice_number": invoice.invoice_number},
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.delete(invoice)
    await db.commit()

    return {"message": "删除成功"}


@router.post("/{invoice_id}/diffs/{diff_id}/resolve")
async def resolve_diff(
    invoice_id: int,
    diff_id: int,
    resolve_request: ResolveDiffRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """解决解析差异，选择OCR、LLM或自定义值"""
    from datetime import datetime
    from decimal import Decimal as Dec

    # Get the diff
    diff_query = select(ParsingDiff).where(
        ParsingDiff.id == diff_id,
        ParsingDiff.invoice_id == invoice_id
    )
    diff_result = await db.execute(diff_query)
    diff = diff_result.scalar_one_or_none()

    if not diff:
        raise HTTPException(status_code=404, detail="差异记录不存在")

    # Capture old value for audit
    old_diff_value = {
        "field_name": diff.field_name,
        "final_value": diff.final_value,
        "source": diff.source,
        "resolved": diff.resolved,
    }

    # Determine the final value
    if resolve_request.source == 'ocr':
        final_value = diff.ocr_value
    elif resolve_request.source == 'llm':
        final_value = diff.llm_value
    elif resolve_request.source == 'custom':
        final_value = resolve_request.custom_value
    else:
        raise HTTPException(status_code=400, detail="无效的来源类型")

    # Update the diff
    diff.final_value = final_value
    diff.source = resolve_request.source
    diff.resolved = 1

    # Get the invoice and update the corresponding field
    invoice_query = select(Invoice).where(Invoice.id == invoice_id)
    invoice_result = await db.execute(invoice_query)
    invoice = invoice_result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # Update the invoice field based on field_name
    field_name = diff.field_name
    if final_value is not None:
        if field_name == 'issue_date':
            try:
                invoice.issue_date = datetime.strptime(final_value, '%Y-%m-%d').date()
            except ValueError:
                pass
        elif field_name in ['total_with_tax', 'amount', 'tax_amount', 'quantity', 'unit_price']:
            try:
                setattr(invoice, field_name, Dec(final_value))
            except (ValueError, TypeError):
                pass
        elif field_name == 'tax_rate':
            # 「自定义」里手输的税率同样要带百分号，见 normalize_tax_rate
            invoice.tax_rate = normalize_tax_rate(final_value)
        else:
            setattr(invoice, field_name, final_value)

    # 裁决完所有差异 ≠ 审核通过。
    #
    # 这里原来会在 all_resolved 时把状态直接置成「已确认」，于是「把每个冲突点
    # 各点了一次」就等于「这张票我认了」—— 用户根本没看过整张票的对账结果，
    # 状态就已经是已确认。现在只回报是否裁完，是否确认只由「确认发票」按钮决定。
    all_diffs_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    all_diffs_result = await db.execute(all_diffs_query)
    all_diffs = all_diffs_result.scalars().all()

    all_resolved = all(d.resolved == 1 for d in all_diffs)

    # Audit log for diff resolution
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="parsing_diff",
        entity_id=diff_id,
        action="resolve",
        old_value=old_diff_value,
        new_value={
            "field_name": field_name,
            "final_value": final_value,
            "source": resolve_request.source,
            "resolved": 1,
            "invoice_id": invoice_id,
        },
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()

    return {
        "message": "差异已解决",
        "field_name": field_name,
        "final_value": final_value,
        "all_resolved": all_resolved
    }


@router.post("/{invoice_id}/confirm")
async def confirm_invoice(
    invoice_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """确认发票，标记所有差异为已解决。"""
    # Get invoice
    invoice_query = select(Invoice).where(Invoice.id == invoice_id)
    invoice_result = await db.execute(invoice_query)
    invoice = invoice_result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    # 「必填」的定义只有一份：invoice_service.CRITICAL_FIELDS。这里再抄一遍
    # 清单的话，改一处就会漏一处，出现「页面能提交、后端却 400」。
    missing_fields = [field for field in CRITICAL_FIELDS if not getattr(invoice, field)]
    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail="无法确认：必填字段缺失，请补全后再确认。"
        )

    # Check if LLM result exists
    llm_query = select(LlmResult).where(LlmResult.invoice_id == invoice_id)
    llm_result = await db.execute(llm_query)
    llm = llm_result.scalar_one_or_none()
    has_llm = llm is not None

    # Get parsing diffs to verify comparison exists
    diffs_query = select(ParsingDiff).where(ParsingDiff.invoice_id == invoice_id)
    diffs_result = await db.execute(diffs_query)
    diffs = diffs_result.scalars().all()

    if has_llm and not diffs:
        raise HTTPException(
            status_code=400,
            detail="无法确认：缺少OCR和LLM的比对结果。请重新解析发票。"
        )

    # Mark all diffs as resolved
    for diff in diffs:
        if diff.resolved == 0:
            # Use existing final_value or ocr_value as default
            if not diff.final_value:
                diff.final_value = diff.ocr_value or diff.llm_value
            diff.resolved = 1

    # Update invoice status
    old_status = invoice.status.value if invoice.status else None
    invoice.status = InvoiceStatus.CONFIRMED

    # Audit log for confirmation
    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="confirm",
        old_value={"status": old_status},
        new_value={
            "status": InvoiceStatus.CONFIRMED.value,
            "resolved_diffs": len(diffs),
            "source": "llm" if has_llm else "ocr",
        },
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()

    return {"message": "发票已确认", "resolved_count": len(diffs)}


@router.post("/{invoice_id}/reject")
async def reject_invoice(
    invoice_id: int,
    request: Request,
    reject_request: Optional[RejectInvoiceRequest] = None,
    db: AsyncSession = Depends(get_db)
):
    """拒绝这张发票 —— 人工审核不通过，票据不予采纳。

    刻意不做必填字段校验：资料不全、跟业务对不上，正是拒绝它的理由。拿必填
    校验把这条路堵上，用户就只能先去编一份假数据才能把票拒掉。

    与「确认」是一对反向动作，互相可覆盖：拒错了再点确认即可恢复。
    """
    query = select(Invoice).where(Invoice.id == invoice_id)
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(status_code=404, detail="发票不存在")

    reason = (reject_request.reason if reject_request else None) or ""
    reason = reason.strip() or None

    if invoice.status == InvoiceStatus.REJECTED:
        # 幂等：连点两次不应该在审计日志里留下两条拒绝记录
        return {
            "message": "该发票已是拒绝状态",
            "status": InvoiceStatus.REJECTED.value,
            "reason": reason,
        }

    old_status = invoice.status.value if invoice.status else None
    invoice.status = InvoiceStatus.REJECTED

    client_info = get_client_info(request)
    await log_audit_no_commit(
        db=db,
        entity_type="invoice",
        entity_id=invoice_id,
        action="reject",
        old_value={"status": old_status},
        new_value={"status": InvoiceStatus.REJECTED.value, "reason": reason},
        ip_address=client_info.get("ip_address"),
        user_agent=client_info.get("user_agent"),
    )

    await db.commit()

    return {
        "message": "发票已拒绝",
        "status": InvoiceStatus.REJECTED.value,
        "reason": reason,
    }


def _trim_trailing_zeros(value) -> str:
    """数量/单价里浮点尾零只影响观感不影响数值：1.0000 -> 1，非零小数原样保留。

    数据库里是 Numeric，取出来是 Decimal，str() 会把建表时的精度原样带出来
    （比如 quantity 存成 1.0000）。直接把尾零剥掉即可，不用 round —— 圆整
    会改数值，对账场景不能接受。
    """
    if value is None:
        return ''
    text = str(value).strip()
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


@router.get("/export/csv")
@limiter.limit("10/minute")
async def export_invoices_csv(
    request: Request,
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    with_items: bool = Query(False, description="是否附带明细行（默认不带）"),
    db: AsyncSession = Depends(get_db)
):
    """导出发票为CSV格式"""
    import csv
    from urllib.parse import quote

    query = select(Invoice)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    start_date_obj = _parse_date_param(start_date, "start_date")
    if start_date_obj:
        query = query.where(Invoice.issue_date >= start_date_obj)
    end_date_obj = _parse_date_param(end_date, "end_date")
    if end_date_obj:
        query = query.where(Invoice.issue_date <= end_date_obj)

    query = query.order_by(Invoice.created_at.desc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    # Create CSV content
    output = BytesIO()
    # Write BOM for Excel compatibility with Chinese characters
    output.write(b'\xef\xbb\xbf')

    import codecs
    writer = csv.writer(codecs.getwriter('utf-8')(output))

    # Header row
    writer.writerow([
        '发票号码', '开票日期', '购买方名称', '购买方纳税人识别号',
        '销售方名称', '销售方纳税人识别号', '项目名称',
        '金额', '税额', '价税合计', '税率',
        '状态', '归属人', '文件名', '创建时间'
    ])

    # 明细行的取数放在写表头之后，避免默认（不带明细）路径白查一次库
    items_by_invoice: Dict[int, List[Any]] = {}
    if with_items:
        item_query = (
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id.in_([inv.id for inv in invoices]))
            .order_by(InvoiceItem.invoice_id, InvoiceItem.row_no)
        )
        for it in (await db.execute(item_query)).scalars().all():
            items_by_invoice.setdefault(it.invoice_id, []).append(it)

    # Data rows
    for inv in invoices:
        writer.writerow([
            inv.invoice_number or '',
            str(inv.issue_date) if inv.issue_date else '',
            inv.buyer_name or '',
            inv.buyer_tax_id or '',
            inv.seller_name or '',
            inv.seller_tax_id or '',
            inv.item_name or '',
            str(inv.amount) if inv.amount else '',
            str(inv.tax_amount) if inv.tax_amount else '',
            str(inv.total_with_tax) if inv.total_with_tax else '',
            inv.tax_rate or '',
            inv.status.value if inv.status else '',
            inv.owner or '',
            inv.file_name or '',
            inv.created_at.strftime('%Y-%m-%d %H:%M:%S') if inv.created_at else ''
        ])

    # 明细放第二段，不再插在发票行中间。旧格式的问题：
    #   · 「发票表头 + 明细表头」两行表头混在一个文件里，读到第 2 行才知道
    #     后面 9 列是另一套含义；
    #   · 同一列在不同行含义不同（第 8 列发票行是价税合计、明细行是税率），
    #     Excel 排序/筛选/求和全会算错；
    #   · 明细行自己不带发票号码，脱离上下文就没法对回是哪张票。
    # 现在改成上下两张独立的表：上半张是发票列表，空一行后是明细段，
    # 每条明细行首列带发票号码，每一行都能自己说明自己。
    if with_items:
        writer.writerow([])
        writer.writerow(['发票明细（发票号码与上表关联）'])
        writer.writerow([
            '发票号码', '明细行号', '货物或应税劳务名称', '规格型号', '单位',
            '数量', '单价', '金额', '税率', '税额',
        ])
        for inv in invoices:
            for it in items_by_invoice.get(inv.id, []):
                writer.writerow([
                    inv.invoice_number or '',
                    it.row_no or '', it.item_name or '', it.specification or '',
                    it.unit or '',
                    _trim_trailing_zeros(it.quantity),
                    _trim_trailing_zeros(it.unit_price),
                    str(it.amount) if it.amount is not None else '',
                    it.tax_rate or '',
                    str(it.tax_amount) if it.tax_amount is not None else '',
                ])

    output.seek(0)

    filename = quote('发票导出.csv')
    return StreamingResponse(
        output,
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"
        }
    )


@router.get("/export/excel")
@limiter.limit("10/minute")
async def export_invoices_excel(
    request: Request,
    invoice_ids: Optional[str] = Query(None, description="发票ID列表，逗号分隔"),
    status: Optional[InvoiceStatus] = Query(None, description="状态筛选"),
    owner: Optional[str] = Query(None, description="归属人筛选"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    with_items: bool = Query(True, description="是否附带发票明细工作表（默认带）"),
    db: AsyncSession = Depends(get_db)
):
    """导出发票为Excel格式"""
    from urllib.parse import quote

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    except ImportError:
        raise HTTPException(status_code=500, detail="Excel导出需要安装openpyxl库")

    query = select(Invoice)

    ids = _parse_invoice_ids(invoice_ids)
    if ids:
        query = query.where(Invoice.id.in_(ids))
    if status:
        query = query.where(Invoice.status == status)
    if owner:
        query = query.where(Invoice.owner == owner)
    start_date_obj = _parse_date_param(start_date, "start_date")
    if start_date_obj:
        query = query.where(Invoice.issue_date >= start_date_obj)
    end_date_obj = _parse_date_param(end_date, "end_date")
    if end_date_obj:
        query = query.where(Invoice.issue_date <= end_date_obj)

    query = query.order_by(Invoice.created_at.desc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "发票列表"

    # Header row
    headers = [
        '发票号码', '开票日期', '购买方名称', '购买方纳税人识别号',
        '销售方名称', '销售方纳税人识别号', '项目名称',
        '金额', '税额', '价税合计', '税率',
        '状态', '归属人', '文件名', '创建时间'
    ]

    header_font = Font(bold=True)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = Alignment(horizontal='center')

    # Data rows
    for row, inv in enumerate(invoices, 2):
        data = [
            inv.invoice_number or '',
            str(inv.issue_date) if inv.issue_date else '',
            inv.buyer_name or '',
            inv.buyer_tax_id or '',
            inv.seller_name or '',
            inv.seller_tax_id or '',
            inv.item_name or '',
            float(inv.amount) if inv.amount else '',
            float(inv.tax_amount) if inv.tax_amount else '',
            float(inv.total_with_tax) if inv.total_with_tax else '',
            inv.tax_rate or '',
            inv.status.value if inv.status else '',
            inv.owner or '',
            inv.file_name or '',
            inv.created_at.strftime('%Y-%m-%d %H:%M:%S') if inv.created_at else ''
        ]

        for col, value in enumerate(data, 1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = thin_border

    # Auto-adjust column widths
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column].width = adjusted_width

    # 第二张表：发票明细视图。一张发票可能有多行货物，摊平成一行一张发票会丢
    # 信息；这里不再用平铺表，而是每张发票一个「卡片」块（标题行 → 信息行 →
    # 明细小表），块间空一行。版式（配色/合并/边框/列宽）与确认过的样例
    # 《发票方案二_示例.xlsx》保持一致。
    if with_items and invoices:
        items_by_invoice: Dict[int, List[Any]] = {}
        ids = [inv.id for inv in invoices]
        item_query = (
            select(InvoiceItem)
            .where(InvoiceItem.invoice_id.in_(ids))
            .order_by(InvoiceItem.invoice_id, InvoiceItem.row_no)
        )
        for it in (await db.execute(item_query)).scalars().all():
            items_by_invoice.setdefault(it.invoice_id, []).append(it)

        ws2 = wb.create_sheet("发票明细视图")

        title_fill = PatternFill(fill_type='solid', start_color='FFDCEAF7',
                                 end_color='FFDCEAF7')
        info_fill = PatternFill(fill_type='solid', start_color='FFF7F9FB',
                                end_color='FFF7F9FB')
        head_fill = PatternFill(fill_type='solid', start_color='FFEAF2F8',
                                end_color='FFEAF2F8')
        label_font = Font(bold=True, color='FF666666')
        medium_tb = Border(top=Side(style='medium'), bottom=Side(style='medium'))
        thin_bottom = Border(bottom=Side(style='thin'))

        # 列宽是卡片版式的一部分，照样例写死，不自适应
        for letter, width in {'A': 18, 'B': 24, 'C': 10, 'D': 10, 'E': 15,
                              'F': 15, 'G': 10, 'H': 18, 'I': 8, 'J': 8}.items():
            ws2.column_dimensions[letter].width = width
        ws2.freeze_panes = 'A2'

        def money(value):
            """金额统一成 float + 千分位格式；None 留空，不写 0。"""
            return float(value) if value is not None else None

        row = 1
        for index, inv in enumerate(invoices):
            if index > 0:
                row += 1  # 块间空一行

            # ---- 标题行：A:H 合并 = 「发票 号码」，I:J 合并 = 状态
            ws2.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
            ws2.merge_cells(start_row=row, start_column=9, end_row=row, end_column=10)
            for col in range(1, 11):
                cell = ws2.cell(row=row, column=col)
                cell.fill = title_fill
                cell.border = medium_tb
            title = ws2.cell(row=row, column=1, value=f'发票 {inv.invoice_number or ""}')
            title.font = Font(bold=True, size=14)
            title.alignment = Alignment(vertical='center')
            status = ws2.cell(row=row, column=9,
                              value=inv.status.value if inv.status else '')
            status.font = Font(bold=True)
            status.alignment = Alignment(horizontal='center', vertical='center')
            row += 1

            # ---- 信息行：左列灰底标签，B:D 合并放值
            info_rows = [
                ('开票日期', str(inv.issue_date) if inv.issue_date else None),
                ('购买方', inv.buyer_name or None),
                ('销售方', inv.seller_name or None),
                ('购买方税号', inv.buyer_tax_id or None),
                ('销售方税号', inv.seller_tax_id or None),
                ('归属人', inv.owner or None),
            ]
            for label, value in info_rows:
                ws2.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
                for col in range(1, 5):
                    cell = ws2.cell(row=row, column=col)
                    cell.fill = info_fill
                    cell.border = thin_bottom
                ws2.cell(row=row, column=1, value=label).font = label_font
                ws2.cell(row=row, column=2, value=value)
                row += 1

            # ---- 金额行：四组「标签 + 值」并排（A/B、C/D、E/F、G/H）
            for col in range(1, 9):
                cell = ws2.cell(row=row, column=col)
                cell.fill = info_fill
                cell.border = thin_bottom
            amount_pairs = [
                ('发票金额', money(inv.amount), None),
                ('税额', money(inv.tax_amount), None),
                ('价税合计', money(inv.total_with_tax), None),
                ('文件', inv.file_name or None, None),
            ]
            for offset, (label, value, _fmt) in enumerate(amount_pairs):
                label_cell = ws2.cell(row=row, column=1 + offset * 2, value=label)
                label_cell.font = label_font
                value_cell = ws2.cell(row=row, column=2 + offset * 2, value=value)
                if isinstance(value, float):
                    value_cell.number_format = '#,##0.00'
            row += 1

            # ---- 明细小表头
            for col, header in enumerate(
                    ['项目', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额'], 1):
                cell = ws2.cell(row=row, column=col, value=header)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal='center')
                cell.fill = head_fill
                cell.border = thin_bottom
            row += 1

            # ---- 明细行：文本左对齐、数值右对齐，一律垂直居中，行尾细底边
            for it in items_by_invoice.get(inv.id, []):
                cells = [
                    (it.item_name or None, None, False),
                    (it.specification or None, None, False),
                    (it.unit or None, None, False),
                    (float(it.quantity) if it.quantity is not None else None,
                     'General', True),
                    (float(it.unit_price) if it.unit_price is not None else None,
                     '#,##0.0000', True),
                    (float(it.amount) if it.amount is not None else None,
                     '#,##0.00', True),
                    (it.tax_rate or None, None, True),
                    (float(it.tax_amount) if it.tax_amount is not None else None,
                     '#,##0.00', True),
                ]
                for col, (value, fmt, is_right) in enumerate(cells, 1):
                    cell = ws2.cell(row=row, column=col, value=value)
                    cell.border = thin_bottom
                    cell.alignment = Alignment(
                        horizontal='right' if is_right else None,
                        vertical='center',
                    )
                    if fmt and value is not None:
                        cell.number_format = fmt
                row += 1

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = quote('发票导出.xlsx')
    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"
        }
    )
