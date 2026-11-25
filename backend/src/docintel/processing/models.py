"""Output schema of the processing stage (consumed by extraction, validation, storage)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

PROCESSOR_VERSION = "1.0.0"


class FileType(str, Enum):
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    XLSX = "xlsx"
    XLS = "xls"
    CSV = "csv"
    UNKNOWN = "unknown"

    @property
    def is_image(self) -> bool:
        return self in (FileType.JPEG, FileType.PNG, FileType.TIFF)

    @property
    def is_spreadsheet(self) -> bool:
        return self in (FileType.XLSX, FileType.XLS, FileType.CSV)


class ExtractionMethod(str, Enum):
    DIGITAL = "digital_text"  # text layer of a born-digital PDF
    OCR = "ocr"  # scanned page / image
    HYBRID = "hybrid"  # OCR merged with a sparse digital text layer
    SPREADSHEET = "spreadsheet"  # parsed cell grid
    NONE = "none"  # nothing could be extracted (see issues)


class DocumentStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"  # some pages failed, the rest is usable
    FAILED = "failed"


class IssueCode(str, Enum):
    # Fatal for the whole file
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    EMPTY_FILE = "EMPTY_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    CORRUPTED = "CORRUPTED"
    ENCRYPTED = "ENCRYPTED"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    DECOMPRESSION_BOMB = "DECOMPRESSION_BOMB"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Page level
    PAGE_FAILED = "PAGE_FAILED"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    OCR_FAILED = "OCR_FAILED"
    OCR_TIMEOUT = "OCR_TIMEOUT"
    # Warnings (processing succeeded but the result deserves a look)
    TYPE_MISMATCH = "TYPE_MISMATCH"
    REPAIRED = "REPAIRED"
    TRUNCATED = "TRUNCATED"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    LOW_IMAGE_QUALITY = "LOW_IMAGE_QUALITY"
    NO_TEXT_FOUND = "NO_TEXT_FOUND"
    EMPTY_SHEET = "EMPTY_SHEET"
    ROWS_TRUNCATED = "ROWS_TRUNCATED"
    TEXT_TRUNCATED = "TEXT_TRUNCATED"
    CELL_ERRORS = "CELL_ERRORS"
    ENCODING_GUESSED = "ENCODING_GUESSED"
    MALFORMED_ROWS = "MALFORMED_ROWS"


Severity = Literal["error", "warning"]

# Warnings that should route a document to a human reviewer.
REVIEW_CODES = {
    IssueCode.TYPE_MISMATCH,
    IssueCode.REPAIRED,
    IssueCode.TRUNCATED,
    IssueCode.LOW_OCR_CONFIDENCE,
    IssueCode.LOW_IMAGE_QUALITY,
    IssueCode.NO_TEXT_FOUND,
    IssueCode.ROWS_TRUNCATED,
    IssueCode.CELL_ERRORS,
    IssueCode.MALFORMED_ROWS,
}


class Issue(BaseModel):
    code: IssueCode
    severity: Severity
    message: str
    page: Optional[int] = None

    @classmethod
    def error(cls, code: IssueCode, message: str, page: int | None = None) -> "Issue":
        return cls(code=code, severity="error", message=message, page=page)

    @classmethod
    def warning(cls, code: IssueCode, message: str, page: int | None = None) -> "Issue":
        return cls(code=code, severity="warning", message=message, page=page)


class ImageQuality(BaseModel):
    width: int
    height: int
    dpi: Optional[float] = None
    sharpness: float = Field(description="Edge sharpness around text, ~0.7 clean, < 0.3 blurry")
    contrast: float = Field(description="Paper minus ink brightness (0-255)")
    noise: float = Field(0.0, description="Background std-dev away from text")
    ink_ratio: float = Field(0.0, description="Share of pixels classified as ink")
    skew_angle: float = 0.0
    is_low_quality: bool = False
    reasons: list[str] = Field(default_factory=list)


class OCRInfo(BaseModel):
    engine: str
    confidence: float = Field(ge=0.0, le=1.0, description="Character-weighted mean confidence")
    preprocessing: str
    rotation: int = 0
    attempts: int = 1
    dpi: Optional[int] = None


Cell = Any  # str | int | float | bool | None (dates are ISO-8601 strings)


class Table(BaseModel):
    name: str
    header: list[str] = Field(default_factory=list)
    rows: list[list[Cell]] = Field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0
    header_row_index: Optional[int] = None
    truncated: bool = False


class PageResult(BaseModel):
    """One unit of content: a PDF page, an image/TIFF frame or a spreadsheet sheet."""

    number: int = Field(description="1-based position in the document")
    label: str
    method: ExtractionMethod
    text: str = ""
    char_count: int = 0
    ocr: Optional[OCRInfo] = None
    quality: Optional[ImageQuality] = None
    tables: list[Table] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    duration_ms: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return any(i.severity == "error" for i in self.issues) and not self.text.strip()


class DocumentMetadata(BaseModel):
    filename: str
    path: str
    size_bytes: int
    sha256: str
    declared_extension: str
    detected_type: FileType
    mime_type: str
    page_count: int = 0
    properties: dict[str, Any] = Field(default_factory=dict)


class ProcessingStats(BaseModel):
    pages_total: int = 0
    pages_digital: int = 0
    pages_ocr: int = 0
    pages_hybrid: int = 0
    pages_spreadsheet: int = 0
    pages_failed: int = 0
    total_chars: int = 0
    mean_ocr_confidence: Optional[float] = None
    min_ocr_confidence: Optional[float] = None


class ProcessedDocument(BaseModel):
    document_id: str = Field(description="SHA-256 of the file content (dedup key)")
    status: DocumentStatus
    metadata: DocumentMetadata
    pages: list[PageResult] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list, description="Document-level issues")
    stats: ProcessingStats = Field(default_factory=ProcessingStats)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    started_at: datetime
    duration_ms: float = 0.0
    processor_version: str = PROCESSOR_VERSION

    @property
    def full_text(self) -> str:
        return "\n\n".join(
            f"--- {p.label} ---\n{p.text}" for p in self.pages if p.text.strip()
        )

    def all_issues(self) -> list[Issue]:
        return [*self.issues, *(i for p in self.pages for i in p.issues)]
