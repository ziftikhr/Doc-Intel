"""Task 1 - Document processing.

Turns PDFs, images (JPG/PNG/TIFF) and spreadsheets (XLSX/XLS/CSV) into a uniform
``ProcessedDocument``: one ``PageResult`` per PDF page / image frame / sheet, with the
extracted text, tables, OCR confidence, image-quality metrics and any issues found.

Public entry points:

* ``process_file(path)``            - process one file (never raises on bad input)
* ``BatchProcessor(...).run(paths)`` - process thousands of files in parallel, resumable
"""

from .config import ProcessingConfig
from .models import (
    DocumentStatus,
    ExtractionMethod,
    FileType,
    Issue,
    IssueCode,
    PageResult,
    ProcessedDocument,
)
from .pipeline import process_file

__all__ = [
    "DocumentStatus",
    "ExtractionMethod",
    "FileType",
    "Issue",
    "IssueCode",
    "PageResult",
    "ProcessedDocument",
    "ProcessingConfig",
    "process_file",
]
