from __future__ import annotations

from .models import IssueCode


class ProcessingError(Exception):
    """A classified, expected failure (bad input) - becomes an error ``Issue``."""

    def __init__(self, code: IssueCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
