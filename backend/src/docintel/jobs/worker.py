"""Background worker: runs Task 1 -> 2 -> 3, stores the results, indexes them for search.

Run it as its own process (``docintel worker``) or inside the API process
(``DOCINTEL_INLINE_WORKER=true``, the default for local use). Both share the same
Postgres queue, so any number of workers can run side by side.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from docintel.db.models import Document, Job
from docintel.db.repository import known_invoice_refs, save_processing, save_results, utcnow
from docintel.db.session import session_scope
from docintel.extraction import ExtractionConfig, extract_invoices
from docintel.processing import ProcessingConfig, process_file
from docintel.search.embeddings import Embedder, make_embedder
from docintel.search.indexer import index_document
from docintel.settings import Settings
from docintel.validation import DuplicateIndex, ValidationConfig, apply_validation, validate_document

from .queue import claim_next, complete, fail, heartbeat, requeue_stale

log = logging.getLogger(__name__)

# Serialises "check duplicates + save" across workers, so two copies of the same
# invoice processed at the same moment cannot both miss each other.
DUPLICATE_LOCK = 727002
HEARTBEAT_SECONDS = 20


@dataclass
class Pipeline:
    settings: Settings
    processing: ProcessingConfig = field(default_factory=ProcessingConfig.from_env)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig.from_env)
    validation: ValidationConfig = field(default_factory=ValidationConfig.from_env)
    embedder: Optional[Embedder] = None
    embedder_info: str = ""

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "Pipeline":
        p = cls(settings=settings, **overrides)
        if "embedder" not in overrides:
            p.embedder, p.embedder_info = make_embedder(settings)
        return p


def _stage(factory: sessionmaker[Session], job_id: uuid.UUID, stage: str) -> None:
    with session_scope(factory) as s:
        heartbeat(s, job_id, stage)


def run_job(factory: sessionmaker[Session], job_id: uuid.UUID, pipeline: Pipeline) -> None:
    """Process the document of a claimed job. Raises on unexpected errors (-> retry)."""
    with session_scope(factory) as s:
        job = s.get(Job, job_id)
        doc = s.get(Document, job.document_id)
        doc.status, doc.error = "processing", None
        path, doc_id = doc.storage_path, doc.id
        filename = doc.filename

    # Task 1 - read the file (never raises on bad input; returns status=failed instead)
    processed = process_file(path, pipeline.processing)
    processed.metadata.filename = filename  # stored under its hash; report the uploaded name
    with session_scope(factory) as s:
        save_processing(s, s.get(Document, doc_id), processed)
        heartbeat(s, job_id, "extracting")

    # Task 2 - extract invoice fields
    result = extract_invoices(processed, pipeline.extraction)
    _stage(factory, job_id, "validating")

    # Task 3 - validate against everything already stored, then save atomically
    with session_scope(factory) as s:
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DUPLICATE_LOCK})
        doc = s.get(Document, doc_id)
        known = known_invoice_refs(s, result.invoices, exclude_document=doc_id)
        report = validate_document(result, pipeline.validation, duplicates=DuplicateIndex(known),
                                   source=f"document:{doc_id}")
        save_results(s, doc, apply_validation(result, report))
        heartbeat(s, job_id, "indexing")

    # Task 5 - chunks + embeddings for search (an embedding outage only degrades search)
    with session_scope(factory) as s:
        index_document(s, doc_id, pipeline.embedder)

    with session_scope(factory) as s:
        doc = s.get(Document, doc_id)
        if processed.status.value == "failed":
            doc.status = "failed"
            doc.error = "; ".join(i.message for i in processed.issues) or "the file could not be read"
        else:
            doc.status = "processed"
        doc.processed_at = utcnow()
        complete(s, s.get(Job, job_id))


class Worker:
    def __init__(self, factory: sessionmaker[Session], pipeline: Pipeline, worker_id: Optional[str] = None):
        self.factory = factory
        self.pipeline = pipeline
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stale_after = timedelta(minutes=pipeline.settings.stale_job_minutes)

    def run_once(self) -> Optional[uuid.UUID]:
        """Claim and run one job. Returns its id, or None when the queue is empty."""
        with session_scope(self.factory) as s:
            requeue_stale(s, self.stale_after)
            job = claim_next(s, self.worker_id)
            job_id = job.id if job else None
        if job_id is None:
            return None
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(job_id, stop), daemon=True)
        beat.start()
        try:
            run_job(self.factory, job_id, self.pipeline)
            log.info("job %s done", job_id)
        except Exception as e:  # noqa: BLE001 - any crash becomes a retry / failure
            log.exception("job %s failed", job_id)
            with session_scope(self.factory) as s:
                job = s.get(Job, job_id)
                retry = fail(s, job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
                if retry:
                    s.get(Document, job.document_id).status = "queued"
        finally:
            stop.set()
            beat.join(timeout=5)
        return job_id

    def drain(self, max_jobs: int = 1000) -> int:
        """Run jobs until the queue is empty (tests, ``docintel worker --once``)."""
        n = 0
        while n < max_jobs and self.run_once() is not None:
            n += 1
        return n

    def run_forever(self, stop: threading.Event) -> None:
        log.info("worker %s started", self.worker_id)
        while not stop.is_set():
            try:
                if self.run_once() is None:
                    stop.wait(self.pipeline.settings.worker_poll_seconds)
            except Exception:  # noqa: BLE001 - e.g. database restarting; keep polling
                log.exception("worker loop error")
                stop.wait(5)
        log.info("worker %s stopped", self.worker_id)

    def _beat(self, job_id: uuid.UUID, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with session_scope(self.factory) as s:
                    heartbeat(s, job_id)
            except Exception:  # noqa: BLE001
                log.warning("heartbeat failed for job %s", job_id)


def start_workers(factory: sessionmaker[Session], pipeline: Pipeline, n: int) -> tuple[list[threading.Thread], threading.Event]:
    stop = threading.Event()
    threads = []
    for i in range(max(1, n)):
        w = Worker(factory, pipeline)
        t = threading.Thread(target=w.run_forever, args=(stop,), name=f"docintel-worker-{i}", daemon=True)
        t.start()
        threads.append(t)
    return threads, stop


