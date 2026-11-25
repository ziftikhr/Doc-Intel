"""Processing pipeline: plan -> work units -> assemble.

Splitting a document into independent *work units* (page ranges of a PDF/TIFF, or a
whole spreadsheet) is what makes the system scale:

* a 1,000-page scanned PDF becomes 40 units of 25 pages that run on all cores
  (or, from Task 4 on, on many queue workers) instead of one 40-minute job;
* a crash in one unit costs 25 pages, not the document;
* the same three functions back both the local ``BatchProcessor`` and the
  background job queue - ``plan_document`` / ``run_unit`` / ``assemble`` map
  directly onto a Celery chord (header = units, callback = assemble).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import ProcessingConfig
from .detection import detect_file_type, sha256_file
from .errors import ProcessingError
from .models import (
    REVIEW_CODES,
    DocumentMetadata,
    DocumentStatus,
    ExtractionMethod,
    FileType,
    Issue,
    IssueCode,
    PageResult,
    ProcessedDocument,
    ProcessingStats,
)
from .processors import image as image_proc
from .processors import pdf as pdf_proc
from .processors import spreadsheet as sheet_proc

PAGED_KINDS = {FileType.PDF, FileType.TIFF, FileType.JPEG, FileType.PNG}


@dataclass(frozen=True)
class WorkUnit:
    doc_id: str
    path: str
    file_type: FileType
    start: int = 0  # 0-based, inclusive (paged types only)
    end: int = 0  # exclusive


@dataclass
class UnitResult:
    unit: WorkUnit
    pages: list[PageResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass
class Plan:
    doc_id: str
    metadata: DocumentMetadata
    started_at: datetime
    t0: float
    units: list[WorkUnit] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    fatal: bool = False


def _metadata(path: Path, size: int = 0, sha: str = "", ftype: FileType = FileType.UNKNOWN,
              mime: str = "application/octet-stream") -> DocumentMetadata:
    return DocumentMetadata(
        filename=path.name, path=str(path), size_bytes=size, sha256=sha,
        declared_extension=path.suffix.lower(), detected_type=ftype, mime_type=mime,
    )


def plan_document(path: str | os.PathLike, config: ProcessingConfig) -> Plan:
    """Validate the file and split it into work units. Never raises."""
    path = Path(path)
    started_at, t0 = datetime.now(timezone.utc), time.time()

    def fail(code: IssueCode, msg: str, meta: DocumentMetadata | None = None) -> Plan:
        meta = meta or _metadata(path)
        return Plan(doc_id=meta.sha256 or f"path:{path}", metadata=meta, started_at=started_at,
                    t0=t0, issues=[Issue.error(code, msg)], fatal=True)

    try:
        if not path.is_file():
            return fail(IssueCode.FILE_NOT_FOUND, f"File not found: {path}")
        size = path.stat().st_size
        if size == 0:
            return fail(IssueCode.EMPTY_FILE, "File is empty (0 bytes)", _metadata(path))
        if size > config.max_file_size_mb * 1024 * 1024:
            return fail(IssueCode.FILE_TOO_LARGE,
                        f"File is {size / 2**20:.0f} MB (limit {config.max_file_size_mb} MB)",
                        _metadata(path, size))

        sha = sha256_file(path)
        meta = _metadata(path, size, sha)
        try:
            det = detect_file_type(path)
        except ProcessingError as exc:
            return fail(exc.code, exc.message, meta)
        meta.detected_type, meta.mime_type = det.file_type, det.mime_type

        if det.file_type is FileType.UNKNOWN:
            note = det.note or "Unrecognised file content"
            return fail(IssueCode.UNSUPPORTED_TYPE,
                        f"{note}. Supported: PDF, JPG, PNG, TIFF, XLSX, XLS, CSV", meta)

        issues: list[Issue] = []
        if det.mismatch:
            issues.append(Issue.warning(
                IssueCode.TYPE_MISMATCH,
                f"Extension {path.suffix!r} but content is {det.file_type.value.upper()}; "
                f"processed as {det.file_type.value.upper()}",
            ))

        ft = det.file_type
        if ft is FileType.PDF:
            count, props, extra_issues = pdf_proc.inspect_pdf(path, config)
        elif ft.is_image:
            count, props, extra_issues = image_proc.inspect_image(path, config)
        elif ft is FileType.XLSX:
            count, props, extra_issues = sheet_proc.inspect_xlsx(path, config)
        elif ft is FileType.XLS:
            count, props, extra_issues = sheet_proc.inspect_xls(path, config)
        else:
            count, props, extra_issues = 1, {}, []
        issues.extend(extra_issues)
        meta.page_count, meta.properties = count, props

        if ft in PAGED_KINDS:
            ocr_heavy = ft.is_image or props.get("estimated_ocr_share", 0) > 0
            step = max(1, config.ocr_chunk_pages if ocr_heavy else config.chunk_pages)
            units = [WorkUnit(sha, str(path), ft, s, min(s + step, count))
                     for s in range(0, count, step)]
        else:
            units = [WorkUnit(sha, str(path), ft)]
        return Plan(doc_id=sha, metadata=meta, started_at=started_at, t0=t0,
                    units=units, issues=issues)
    except ProcessingError as exc:
        return fail(exc.code, exc.message, locals().get("meta"))
    except Exception as exc:  # last line of defence: bad input must never crash a worker
        return fail(IssueCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}", locals().get("meta"))


def run_unit(unit: WorkUnit, config: ProcessingConfig) -> UnitResult:
    """Process one work unit. Never raises."""
    path = Path(unit.path)
    try:
        ft = unit.file_type
        if ft is FileType.PDF:
            pages = pdf_proc.process_pdf_pages(path, unit.start, unit.end, config)
        elif ft.is_image:
            pages = image_proc.process_image_frames(path, unit.start, unit.end, config)
        elif ft is FileType.XLSX:
            pages = sheet_proc.process_xlsx(path, config)
        elif ft is FileType.XLS:
            pages = sheet_proc.process_xls(path, config)
        elif ft is FileType.CSV:
            pages = sheet_proc.process_csv(path, config)
        else:
            raise ProcessingError(IssueCode.UNSUPPORTED_TYPE, f"No processor for {ft}")
        return UnitResult(unit, pages)
    except ProcessingError as exc:
        return UnitResult(unit, issues=[Issue.error(exc.code, exc.message)])
    except Exception as exc:
        return UnitResult(unit, issues=[Issue.error(IssueCode.INTERNAL_ERROR,
                                                    f"{type(exc).__name__}: {exc}")])


def assemble(plan: Plan, results: list[UnitResult]) -> ProcessedDocument:
    """Merge unit results (in any order) into the final document."""
    issues = list(plan.issues)
    pages: list[PageResult] = []
    for r in results:
        pages.extend(r.pages)
        issues.extend(r.issues)
    pages.sort(key=lambda p: p.number)

    # Any page a crashed unit never returned is reported, never silently dropped.
    if not plan.fatal and plan.metadata.detected_type in PAGED_KINDS:
        have = {p.number for p in pages}
        unit_errors = [i.message for r in results for i in r.issues]
        reason = unit_errors[0] if unit_errors else "worker did not return this page"
        for n in range(1, plan.metadata.page_count + 1):
            if n not in have:
                pages.append(PageResult(number=n, label=f"Page {n}", method=ExtractionMethod.NONE,
                                        issues=[Issue.error(IssueCode.PAGE_FAILED, reason, n)]))
        pages.sort(key=lambda p: p.number)

    stats = _stats(pages)
    failed_pages = stats.pages_failed
    ok_pages = len(pages) - failed_pages
    if plan.fatal or (ok_pages == 0 and (failed_pages or any(i.severity == "error" for i in issues))):
        status = DocumentStatus.FAILED
    elif failed_pages or any(i.severity == "error" for i in issues):
        status = DocumentStatus.PARTIAL
    else:
        status = DocumentStatus.SUCCESS

    if status is not DocumentStatus.FAILED and stats.total_chars == 0:
        issues.append(Issue.warning(IssueCode.NO_TEXT_FOUND, "No text could be extracted"))

    doc = ProcessedDocument(
        document_id=plan.doc_id, status=status, metadata=plan.metadata, pages=pages,
        issues=issues, stats=stats, started_at=plan.started_at,
        duration_ms=round((time.time() - plan.t0) * 1000, 2),
    )
    doc.review_reasons = _review_reasons(doc)
    doc.needs_review = bool(doc.review_reasons)
    return doc


def _stats(pages: list[PageResult]) -> ProcessingStats:
    s = ProcessingStats(pages_total=len(pages))
    confs = []
    for p in pages:
        s.total_chars += p.char_count
        if p.failed:
            s.pages_failed += 1
        elif p.method is ExtractionMethod.DIGITAL:
            s.pages_digital += 1
        elif p.method is ExtractionMethod.OCR:
            s.pages_ocr += 1
        elif p.method is ExtractionMethod.HYBRID:
            s.pages_hybrid += 1
        elif p.method is ExtractionMethod.SPREADSHEET:
            s.pages_spreadsheet += 1
        if p.ocr and p.char_count:
            confs.append(p.ocr.confidence)
    if confs:
        s.mean_ocr_confidence = round(sum(confs) / len(confs), 4)
        s.min_ocr_confidence = round(min(confs), 4)
    return s


def _review_reasons(doc: ProcessedDocument) -> list[str]:
    reasons: dict[str, None] = {}
    if doc.status is not DocumentStatus.SUCCESS:
        reasons[f"processing {doc.status.value}"] = None
    for issue in doc.all_issues():
        if issue.severity == "error" or issue.code in REVIEW_CODES:
            reasons[issue.code.value] = None
    return list(reasons)


def process_file(path: str | os.PathLike, config: ProcessingConfig | None = None) -> ProcessedDocument:
    """Process a single file synchronously in the current process. Never raises."""
    config = config or ProcessingConfig.from_env()
    plan = plan_document(path, config)
    results = [run_unit(u, config) for u in plan.units]
    return assemble(plan, results)
