"""Extraction configuration. Every value can be overridden with a DOCINTEL_* env var
(e.g. ``DOCINTEL_LLM_MODEL=gpt-5.4``). API keys are read from ``OPENAI_API_KEY`` and
``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class ExtractionConfig:
    # --- LLM ----------------------------------------------------------------------
    # auto = Gemini if GEMINI_API_KEY is set, else OpenAI | openai | gemini | none (rules only)
    llm_provider: str = "auto"
    llm_model: str = ""  # empty = provider default (gpt-5.4-mini / gemini-3.5-flash)
    # Reasoning/thinking effort; set to "" for models without it (gpt-4.1, gpt-4o).
    llm_reasoning_effort: str = "low"
    llm_timeout_s: int = 120
    llm_max_retries: int = 2
    llm_concurrency: int = 4  # parallel LLM calls per document
    # A per-minute rate limit (HTTP 429) is waited out this many times per segment;
    # a daily quota / no credit stops LLM calls for the rest of the document instead.
    llm_rate_limit_retries: int = 4
    llm_rate_limit_max_wait_s: int = 65

    # --- Segmentation ----------------------------------------------------------------
    max_segment_pages: int = 8
    max_segment_chars: int = 40_000

    # --- Confidence --------------------------------------------------------------------
    # Fields below this confidence are flagged for review.
    review_confidence: float = 0.6
    # OCR pages below this mean confidence get LOW_OCR_CONFIDENCE on their fields.
    low_ocr_confidence: float = 0.6

    @classmethod
    def from_env(cls, **overrides) -> "ExtractionConfig":
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"DOCINTEL_{f.name.upper()}")
            if raw is None:
                continue
            if f.type in ("int", int):
                values[f.name] = int(raw)
            elif f.type in ("float", float):
                values[f.name] = float(raw)
            else:
                values[f.name] = raw.strip()
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def resolved_provider(self) -> str:
        p = (self.llm_provider or "").lower()
        if p != "auto":
            return p
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return "gemini"
        return "openai"

    def with_overrides(self, **overrides) -> "ExtractionConfig":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})
