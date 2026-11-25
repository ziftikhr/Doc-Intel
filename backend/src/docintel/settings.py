"""Application settings for the API, worker, database and search (Tasks 4-5).

Read from environment variables (and ``.env``):

    DATABASE_URL                  postgresql+psycopg://user:pass@localhost:5433/docintel
    DOCINTEL_STORAGE_DIR          where uploaded originals are kept (default ./storage)
    DOCINTEL_MAX_UPLOAD_MB        per-file upload limit (default 50)
    DOCINTEL_API_KEY              if set, every /api request needs header X-API-Key
    DOCINTEL_CORS_ORIGINS         comma-separated, default http://localhost:3000
    DOCINTEL_INLINE_WORKER        run background workers inside the API process (default true)
    DOCINTEL_WORKER_THREADS       inline worker threads (default 1)
    DOCINTEL_JOB_MAX_ATTEMPTS     retries for crashed jobs (default 3)
    DOCINTEL_STALE_JOB_MINUTES    a running job without heartbeat this long is re-queued (default 10)
    DOCINTEL_EMBEDDING_PROVIDER   auto | gemini | hashing | none (auto: gemini if its key is set)
    DOCINTEL_EMBEDDING_MODEL      default gemini-embedding-001
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

# The database column is vector(768); changing it needs a migration + re-index.
EMBEDDING_DIM = 768

ALLOWED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff",
                      ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt")


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://docintel:docintel@127.0.0.1:5433/docintel"
    storage_dir: str = "storage"
    max_upload_mb: int = 50
    api_key: str = ""
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    inline_worker: bool = True
    worker_threads: int = 1
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3
    stale_job_minutes: int = 10
    embedding_provider: str = "auto"
    embedding_model: str = "gemini-embedding-001"
    allowed_extensions: tuple[str, ...] = field(default=ALLOWED_EXTENSIONS)

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_dir).resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        values: dict = {}
        if os.environ.get("DATABASE_URL"):
            values["database_url"] = os.environ["DATABASE_URL"]
        for f in fields(cls):
            raw = os.environ.get(f"DOCINTEL_{f.name.upper()}")
            if raw is None or f.name == "database_url":
                continue
            default = getattr(cls, f.name, None)
            if isinstance(default, bool):
                values[f.name] = _bool(raw)
            elif isinstance(default, int):
                values[f.name] = int(raw)
            elif isinstance(default, float):
                values[f.name] = float(raw)
            elif isinstance(default, tuple) or f.name in ("cors_origins", "allowed_extensions"):
                values[f.name] = tuple(x.strip() for x in raw.split(",") if x.strip())
            else:
                values[f.name] = raw.strip()
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def with_overrides(self, **overrides) -> "Settings":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})


def load_env() -> None:
    """Load .env from the working directory (or a parent) without overriding real env vars."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)
