from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import factory  # noqa: E402

from docintel.processing import ProcessingConfig  # noqa: E402
from docintel.processing.ocr import available_engines  # noqa: E402

ENGINES = available_engines()
requires_ocr = pytest.mark.skipif(not ENGINES, reason="no OCR engine installed")


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("samples")


@pytest.fixture
def config() -> ProcessingConfig:
    return ProcessingConfig(workers=1)


def norm(text: str) -> str:
    """Whitespace/case-insensitive form for tolerant OCR assertions."""
    return re.sub(r"\s+", "", text).lower()


def found_tokens(text: str, tokens=factory.KEY_TOKENS) -> list[str]:
    t = norm(text)
    return [tok for tok in tokens if norm(tok) in t]


def codes(doc) -> set[str]:
    return {i.code.value for i in doc.all_issues()}


# ------------------------------------------------------ database (Tasks 4-5) ----------
# Tests use a separate database "docintel_test" on the same server as DATABASE_URL
# (docker compose up -d). They are skipped when no PostgreSQL is reachable.


def _test_db_url() -> str | None:
    import os

    from docintel.settings import load_env

    load_env()
    url = os.environ.get("DOCINTEL_TEST_DATABASE_URL")
    if url:
        return url
    base = os.environ.get("DATABASE_URL")
    return base.rsplit("/", 1)[0] + "/docintel_test" if base else None


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = _test_db_url()
    if not url:
        pytest.skip("no DATABASE_URL configured")
    from sqlalchemy import create_engine, text

    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
        with admin.connect() as c:
            name = url.rsplit("/", 1)[1]
            if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar():
                c.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable ({type(e).__name__}); run `docker compose up -d`")
    from docintel.db import drop_all, get_engine, init_db

    engine = get_engine(url)
    drop_all(engine)  # always start from the current schema
    init_db(engine)
    return url


@pytest.fixture
def db(pg_url):
    """Empty tables before each test; returns a session factory."""
    from sqlalchemy import text

    from docintel.db import get_engine, session_factory

    with get_engine(pg_url).begin() as c:
        c.execute(text("TRUNCATE documents, jobs, invoices, line_items, validation_issues, pages, chunks CASCADE"))
    return session_factory(pg_url)


@pytest.fixture
def app_settings(pg_url, tmp_path):
    from docintel.settings import Settings

    return Settings(database_url=pg_url, storage_dir=str(tmp_path / "storage"), inline_worker=False,
                    embedding_provider="hashing", max_upload_mb=2)


@pytest.fixture
def pipeline(app_settings):
    from docintel.extraction import ExtractionConfig
    from docintel.jobs import Pipeline
    from docintel.search import HashingEmbedder

    return Pipeline(settings=app_settings, processing=ProcessingConfig(workers=1),
                    extraction=ExtractionConfig(llm_provider="none"),
                    embedder=HashingEmbedder(), embedder_info="hashing-v1")


@pytest.fixture
def client(db, app_settings, pipeline):
    from fastapi.testclient import TestClient

    from docintel.api import create_app

    app = create_app(app_settings, pipeline=pipeline, inline_worker=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def run_jobs(client, pipeline):
    """Process everything queued, synchronously (instead of a background thread)."""
    from docintel.jobs import Worker

    def run() -> int:
        return Worker(client.app.state.session_factory, pipeline, worker_id="test").drain()

    return run
