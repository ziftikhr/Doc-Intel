"""REST endpoints (mounted under /api/v1)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from docintel.db.models import Document, Invoice, Job, Page, ValidationIssueRow
from docintel.jobs.queue import enqueue
from docintel.search.query import SearchFilters, SearchResponse, search

from .deps import get_session, require_api_key
from .preview import media_type, render_page
from .schemas import (
    DocumentDetail,
    DocumentSummary,
    InvoiceDetail,
    InvoiceSummary,
    IssueOut,
    JobOut,
    PageOut,
    ReviewRequest,
    StatsOut,
    UploadResult,
)
from .schemas import (
    Page as PageOf,
)
from .services import ServiceError, get_or_404, register_upload, review_invoice, safe_filename, store_upload

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def _issue_counts(session: Session, invoice_ids: list[uuid.UUID]) -> dict:
    if not invoice_ids:
        return {}
    rows = session.execute(
        select(ValidationIssueRow.invoice_id,
               func.count().filter(ValidationIssueRow.severity == "error"),
               func.count().filter(ValidationIssueRow.severity == "warning"))
        .where(ValidationIssueRow.invoice_id.in_(invoice_ids)).group_by(ValidationIssueRow.invoice_id)).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _summaries(session: Session, rows: list[tuple[Invoice, str]]) -> list[InvoiceSummary]:
    counts = _issue_counts(session, [inv.id for inv, _ in rows])
    out = []
    for inv, filename in rows:
        s = InvoiceSummary.model_validate(inv)
        s.filename = filename
        s.errors, s.warnings = counts.get(inv.id, (0, 0))
        out.append(s)
    return out


# --------------------------------------------------------------- documents ----------


@router.post("/documents", response_model=list[UploadResult], status_code=status.HTTP_202_ACCEPTED,
             tags=["documents"], summary="Upload one or more documents for background processing")
def upload_documents(request: Request, files: list[UploadFile] = File(...),
                     reprocess: bool = Query(False, description="Process again if this exact file exists"),
                     session: Session = Depends(get_session)):
    settings = request.app.state.settings
    results = []
    for f in files:
        name = safe_filename(f.filename)
        stored = store_upload(f.file, name, settings)
        doc, job, duplicate = register_upload(session, stored, name, f.content_type, settings, reprocess)
        session.commit()
        if duplicate and not reprocess:
            msg = "this exact file was already uploaded; returning the existing document"
        elif duplicate:
            msg = "queued for reprocessing"
        else:
            msg = "queued for processing"
        results.append(UploadResult(document=DocumentSummary.model_validate(doc),
                                    job=JobOut.of(job) if job else None, duplicate_upload=duplicate, message=msg))
    return results


@router.get("/documents", response_model=PageOf[DocumentSummary], tags=["documents"])
def list_documents(session: Session = Depends(get_session),
                   status_: Optional[Literal["queued", "processing", "processed", "failed"]] = Query(None, alias="status"),
                   validation_status: Optional[str] = None, needs_review: Optional[bool] = None,
                   q: Optional[str] = Query(None, description="Filename contains"),
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    conds = []
    if status_:
        conds.append(Document.status == status_)
    if validation_status:
        conds.append(Document.validation_status == validation_status)
    if needs_review is not None:
        conds.append(Document.needs_review == needs_review)
    if q:
        conds.append(Document.filename.ilike(f"%{q}%"))
    total = session.scalar(select(func.count()).select_from(Document).where(*conds))
    docs = session.scalars(select(Document).where(*conds).order_by(Document.created_at.desc())
                           .limit(limit).offset(offset)).all()
    return PageOf(items=[DocumentSummary.model_validate(d) for d in docs], total=total, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentDetail, tags=["documents"])
def get_document(document_id: uuid.UUID, session: Session = Depends(get_session)):
    doc = get_or_404(session, Document, document_id, "document")
    out = DocumentDetail.model_validate(doc, from_attributes=True)
    out.invoices = _summaries(session, [(inv, doc.filename) for inv in doc.invoices])
    out.issues = [IssueOut.model_validate(i) for i in session.scalars(
        select(ValidationIssueRow).where(ValidationIssueRow.document_id == doc.id,
                                         ValidationIssueRow.invoice_id.is_(None)))]
    out.jobs = [JobOut.of(j) for j in doc.jobs]
    return out


@router.get("/documents/{document_id}/pages", response_model=list[PageOut], tags=["documents"],
            summary="Extracted text of every page (Task 1 output)")
def get_pages(document_id: uuid.UUID, session: Session = Depends(get_session)):
    get_or_404(session, Document, document_id, "document")
    return session.scalars(select(Page).where(Page.document_id == document_id).order_by(Page.number)).all()


@router.get("/documents/{document_id}/result", tags=["documents"],
            summary="The complete Task 2 + Task 3 output, exactly as the pipeline produced it")
def get_result(document_id: uuid.UUID, session: Session = Depends(get_session)):
    doc = get_or_404(session, Document, document_id, "document")
    if doc.result is None:
        raise ServiceError(409, f"the document is not processed yet (status: {doc.status})")
    return doc.result


def _stored_path(doc: Document, request: Request) -> Path:
    path = Path(doc.storage_path).resolve()
    root = request.app.state.settings.storage_path
    if root not in path.parents or not path.is_file():
        raise ServiceError(404, "the stored file is missing")
    return path


@router.get("/documents/{document_id}/file", tags=["documents"], summary="The original file")
def download_file(document_id: uuid.UUID, request: Request, session: Session = Depends(get_session),
                  inline: bool = Query(False, description="Show in the browser instead of downloading")):
    doc = get_or_404(session, Document, document_id, "document")
    return FileResponse(_stored_path(doc, request), filename=doc.filename,
                        media_type=media_type(doc.filename, doc.detected_type),
                        content_disposition_type="inline" if inline else "attachment")


@router.get("/documents/{document_id}/pages/{number}/image", tags=["documents"],
            summary="A page rendered as PNG (PDF pages and image frames, including TIFF)",
            response_class=Response, responses={200: {"content": {"image/png": {}}}})
def page_image(document_id: uuid.UUID, number: int, request: Request, session: Session = Depends(get_session),
               width: int = Query(1200, ge=200, le=2000)):
    doc = get_or_404(session, Document, document_id, "document")
    png = render_page(_stored_path(doc, request), doc.detected_type, number, width)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/documents/{document_id}/reprocess", response_model=JobOut, status_code=202, tags=["documents"])
def reprocess(document_id: uuid.UUID, request: Request, session: Session = Depends(get_session)):
    doc = get_or_404(session, Document, document_id, "document")
    if any(j.status in ("queued", "running") for j in doc.jobs):
        raise ServiceError(409, "this document is already being processed")
    doc.status, doc.error = "queued", None
    job = enqueue(session, doc.id, request.app.state.settings.job_max_attempts)
    session.commit()
    return JobOut.of(job)


@router.delete("/documents/{document_id}", status_code=204, tags=["documents"])
def delete_document(document_id: uuid.UUID, session: Session = Depends(get_session)):
    doc = get_or_404(session, Document, document_id, "document")
    if any(j.status == "running" for j in doc.jobs):
        raise ServiceError(409, "the document is being processed; try again when the job has finished")
    path = Path(doc.storage_path)
    session.delete(doc)
    session.commit()
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()  # the per-hash folder, if now empty
    except OSError:
        pass


# -------------------------------------------------------------------- jobs ----------


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"], summary="Background job status")
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)):
    return JobOut.of(get_or_404(session, Job, job_id, "job"))


@router.get("/jobs", response_model=PageOf[JobOut], tags=["jobs"])
def list_jobs(session: Session = Depends(get_session),
              status_: Optional[Literal["queued", "running", "succeeded", "failed"]] = Query(None, alias="status"),
              limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    conds = [Job.status == status_] if status_ else []
    total = session.scalar(select(func.count()).select_from(Job).where(*conds))
    jobs = session.scalars(select(Job).where(*conds).order_by(Job.created_at.desc()).limit(limit).offset(offset))
    return PageOf(items=[JobOut.of(j) for j in jobs], total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------- invoices ----------

SORTS = {"date": Invoice.invoice_date, "total": Invoice.total, "created": Invoice.created_at,
         "supplier": Invoice.supplier_name, "confidence": Invoice.confidence}


@router.get("/invoices", response_model=PageOf[InvoiceSummary], tags=["invoices"])
def list_invoices(session: Session = Depends(get_session),
                  supplier: Optional[str] = None, customer: Optional[str] = None, currency: Optional[str] = None,
                  date_from: Optional[date] = None, date_to: Optional[date] = None,
                  min_total: Optional[Decimal] = None, max_total: Optional[Decimal] = None,
                  validation_status: Optional[Literal["valid", "needs_review", "invalid"]] = None,
                  review_status: Optional[Literal["pending", "approved", "rejected"]] = None,
                  needs_review: Optional[bool] = None, document_id: Optional[uuid.UUID] = None,
                  issue_code: Optional[str] = Query(None, description="e.g. TOTAL_MISMATCH"),
                  sort: Literal["date", "total", "created", "supplier", "confidence"] = "created",
                  order: Literal["asc", "desc"] = "desc",
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    filters = SearchFilters(supplier=supplier, customer=customer, currency=currency, date_from=date_from,
                            date_to=date_to, min_total=min_total, max_total=max_total,
                            validation_status=validation_status, review_status=review_status,
                            needs_review=needs_review, document_id=document_id)
    conds = filters.invoice_conditions()
    if issue_code:
        conds.append(Invoice.id.in_(select(ValidationIssueRow.invoice_id)
                                    .where(ValidationIssueRow.code == issue_code.upper())))
    total = session.scalar(select(func.count()).select_from(Invoice).where(*conds))
    col = SORTS[sort]
    ordering = (col.asc() if order == "asc" else col.desc()).nulls_last()
    rows = session.execute(select(Invoice, Document.filename).join(Document).where(*conds)
                           .order_by(ordering, Invoice.id).limit(limit).offset(offset)).all()
    return PageOf(items=_summaries(session, [tuple(r) for r in rows]), total=total, limit=limit, offset=offset)


def _detail(session: Session, inv: Invoice) -> InvoiceDetail:
    out = InvoiceDetail.model_validate(inv)
    out.filename = inv.document.filename
    out.errors = sum(i.severity == "error" for i in inv.issues)
    out.warnings = sum(i.severity == "warning" for i in inv.issues)
    return out


def _load_invoice(session: Session, invoice_id: uuid.UUID) -> Invoice:
    inv = session.scalar(select(Invoice).where(Invoice.id == invoice_id)
                         .options(selectinload(Invoice.line_items), selectinload(Invoice.issues),
                                  selectinload(Invoice.document)))
    if inv is None:
        raise ServiceError(404, "invoice not found")
    return inv


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetail, tags=["invoices"])
def get_invoice(invoice_id: uuid.UUID, session: Session = Depends(get_session)):
    return _detail(session, _load_invoice(session, invoice_id))


@router.post("/invoices/{invoice_id}/review", response_model=InvoiceDetail, tags=["invoices"],
             summary="Approve / reject an invoice, or correct fields (validation re-runs)")
def review(invoice_id: uuid.UUID, body: ReviewRequest, request: Request, session: Session = Depends(get_session)):
    inv = _load_invoice(session, invoice_id)
    if body.action is None and not body.corrections and body.note is None:
        raise ServiceError(422, "nothing to do: give an action, corrections or a note")
    review_invoice(session, inv, action=body.action, corrections=body.corrections, note=body.note,
                   force=body.force, config=request.app.state.pipeline.validation,
                   embedder=request.app.state.pipeline.embedder)
    session.commit()
    return _detail(session, _load_invoice(session, invoice_id))


# ------------------------------------------------------------------ search ----------


@router.get("/search", response_model=SearchResponse, tags=["search"],
            summary="Semantic + keyword search over invoices and document text")
def search_endpoint(request: Request, session: Session = Depends(get_session),
                    q: str = Query(..., min_length=1, max_length=500),
                    scope: Literal["all", "invoices", "passages"] = "all",
                    limit: int = Query(10, ge=1, le=50),
                    min_similarity: Optional[float] = Query(None, ge=-1, le=1,
                                                            description="Override the model's relevance cut-off"),
                    supplier: Optional[str] = None, currency: Optional[str] = None,
                    date_from: Optional[date] = None, date_to: Optional[date] = None,
                    min_total: Optional[Decimal] = None, max_total: Optional[Decimal] = None,
                    validation_status: Optional[Literal["valid", "needs_review", "invalid"]] = None,
                    document_id: Optional[uuid.UUID] = None):
    filters = SearchFilters(supplier=supplier, currency=currency, date_from=date_from, date_to=date_to,
                            min_total=min_total, max_total=max_total, validation_status=validation_status,
                            document_id=document_id)
    return search(session, q, request.app.state.pipeline.embedder, filters, scope=scope, limit=limit,
                  min_similarity=min_similarity)


# ------------------------------------------------------------------- stats ----------


def queue_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, **{s: n for s, n in rows}}


@router.get("/stats", response_model=StatsOut, tags=["stats"], summary="Counts for a dashboard")
def stats(session: Session = Depends(get_session)):
    def grouped(col, model):
        return {k or "none": n for k, n in session.execute(select(col, func.count()).select_from(model)
                                                            .group_by(col)).all()}

    totals = session.execute(select(Invoice.currency, func.sum(Invoice.total))
                             .where(Invoice.total.is_not(None), Invoice.review_status != "rejected")
                             .group_by(Invoice.currency)).all()
    top = session.execute(select(ValidationIssueRow.code, ValidationIssueRow.severity, func.count().label("n"))
                          .where(ValidationIssueRow.severity != "info")
                          .group_by(ValidationIssueRow.code, ValidationIssueRow.severity)
                          .order_by(func.count().desc()).limit(10)).all()
    return StatsOut(
        documents=grouped(Document.status, Document),
        invoices=grouped(Invoice.validation_status, Invoice),
        review=grouped(Invoice.review_status, Invoice),
        needs_review=session.scalar(select(func.count()).select_from(Invoice).where(Invoice.needs_review)) or 0,
        totals_by_currency={(c or "unknown"): t for c, t in totals},
        top_issues=[{"code": c, "severity": s, "count": n} for c, s, n in top],
        queue=queue_counts(session))

