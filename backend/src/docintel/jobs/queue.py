"""A durable job queue in PostgreSQL - no Redis/Celery to install.

* ``claim_next`` uses ``SELECT ... FOR UPDATE SKIP LOCKED``: any number of workers can
  poll at once and each job is handed to exactly one of them.
* A running job's worker updates ``heartbeat_at``; if a worker dies, ``requeue_stale``
  hands its job to another worker (counting it as a failed attempt).
* A job that raises is retried with exponential back-off up to ``max_attempts``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from docintel.db.models import Document, Job
from docintel.db.repository import utcnow

RETRY_BASE_SECONDS = 5


def enqueue(session: Session, document_id: uuid.UUID, max_attempts: int = 3, kind: str = "process") -> Job:
    job = Job(document_id=document_id, kind=kind, status="queued", max_attempts=max_attempts)
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, worker_id: str) -> Optional[Job]:
    """Atomically take the oldest runnable job (caller commits)."""
    job = session.scalars(
        select(Job).where(Job.status == "queued", Job.run_after <= func.now())
        .order_by(Job.created_at).limit(1).with_for_update(skip_locked=True)).first()
    if job is None:
        return None
    now = utcnow()
    job.status, job.stage, job.worker_id = "running", "processing", worker_id
    job.attempts += 1
    job.started_at, job.heartbeat_at, job.error, job.finished_at = now, now, None, None
    session.flush()
    return job


def heartbeat(session: Session, job_id: uuid.UUID, stage: Optional[str] = None) -> None:
    values = {"heartbeat_at": utcnow()}
    if stage:
        values["stage"] = stage
    session.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(**values))


def complete(session: Session, job: Job) -> None:
    job.status, job.stage, job.finished_at, job.error = "succeeded", "done", utcnow(), None


def fail(session: Session, job: Job, error: str) -> bool:
    """Record a crash. Returns True if the job will be retried."""
    job.error = error[:4000]
    if job.attempts < job.max_attempts:
        job.status, job.worker_id = "queued", None
        job.run_after = utcnow() + timedelta(seconds=RETRY_BASE_SECONDS * 2 ** (job.attempts - 1))
        return True
    job.status, job.finished_at = "failed", utcnow()
    doc = session.get(Document, job.document_id)
    if doc is not None:
        doc.status, doc.error = "failed", f"processing failed after {job.attempts} attempt(s): {error[:1000]}"
    return False


def requeue_stale(session: Session, stale_after: timedelta) -> int:
    """Give jobs of crashed workers back to the queue (or fail them when out of attempts)."""
    cutoff = utcnow() - stale_after
    stale = session.scalars(select(Job).where(Job.status == "running", Job.heartbeat_at < cutoff)
                            .with_for_update(skip_locked=True)).all()
    for job in stale:
        fail(session, job, f"worker {job.worker_id} stopped responding during stage '{job.stage}'")
    return len(stale)
