"""Parallel, resumable batch processing for thousands of files / pages.

* One shared process pool executes both planning and page-range units, so a single
  huge scan and thousands of small receipts are load-balanced the same way.
* In-flight work is bounded (back-pressure): memory stays flat no matter how many
  files are queued. Units of already-started documents are scheduled before new
  documents so finished results are written and released early.
* Output is content-addressed (``<sha256>.json``): re-running the batch skips files
  already processed, and byte-identical duplicates are processed only once.
* If a worker process dies (e.g. a native-library crash on a malicious file) the pool
  is rebuilt and in-flight units are retried once; repeat offenders are marked failed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, Field

from .config import ProcessingConfig
from .models import DocumentStatus, Issue, IssueCode, ProcessedDocument
from .pipeline import Plan, UnitResult, WorkUnit, _metadata, assemble, plan_document, run_unit

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff",
                      ".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
MAX_RETRIES = 1

# ---------------------------------------------------------------- worker side ----

_WORKER_CONFIG: ProcessingConfig | None = None


def _init_worker(config: ProcessingConfig, threads: int) -> None:
    global _WORKER_CONFIG
    _WORKER_CONFIG = config
    # Parallelism comes from processes; cap each OCR engine's own threading so that
    # workers x threads ~= cores (N workers x N threads thrashes the CPU).
    os.environ["DOCINTEL_OCR_THREADS"] = str(threads)
    os.environ["OMP_THREAD_LIMIT"] = str(threads)
    logging.basicConfig(level=logging.WARNING)


def _fault_injection(path: str) -> None:
    # Test hook: hard-kill the worker (like a segfault in a native parser) when the
    # path contains the marker. Inert unless the env var is set.
    marker = os.environ.get("DOCINTEL_FAULT_INJECT_CRASH")
    if marker and marker in path:
        os._exit(70)


def _plan_task(path: str) -> Plan:
    _fault_injection(path)
    return plan_document(path, _WORKER_CONFIG)


def _unit_task(unit: WorkUnit) -> UnitResult:
    _fault_injection(unit.path)
    return run_unit(unit, _WORKER_CONFIG)


class _InlineExecutor:
    """Same interface as the pool, runs tasks immediately (workers=1, debugging)."""

    def __init__(self, config: ProcessingConfig):
        _init_worker(config, threads=min(8, os.cpu_count() or 1))

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # pragma: no cover - tasks never raise
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


# ---------------------------------------------------------------- results --------


class FailureRecord(BaseModel):
    path: str
    status: DocumentStatus
    codes: list[str]
    message: str


class BatchSummary(BaseModel):
    files_total: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    skipped_existing: int = 0
    duplicates: int = 0
    needs_review: int = 0
    pages_total: int = 0
    pages_failed: int = 0
    pages_by_method: dict[str, int] = Field(default_factory=dict)
    wall_time_s: float = 0.0
    pages_per_second: float = 0.0
    documents_per_minute: float = 0.0
    success_rate: float = 0.0
    failure_rate: float = 0.0
    worker_restarts: int = 0
    failures: list[FailureRecord] = Field(default_factory=list)


@dataclass
class _DocState:
    plan: Plan
    remaining: int
    results: list[UnitResult] = field(default_factory=list)


# ---------------------------------------------------------------- orchestrator ---


def discover_files(inputs: Iterable[str | os.PathLike]) -> list[Path]:
    """Expand directories recursively; keep explicitly named files as-is."""
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*")
                                if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES))
        else:
            files.append(p)
    return files


class BatchProcessor:
    def __init__(self, config: ProcessingConfig | None = None, output_dir: str | os.PathLike | None = None,
                 workers: int | None = None, resume: bool = True,
                 on_document: Callable[[ProcessedDocument, int, int], None] | None = None):
        self.config = config or ProcessingConfig.from_env()
        self.workers = workers if workers is not None else self.config.workers
        self.output_dir = Path(output_dir) if output_dir else None
        self.resume = resume
        self.on_document = on_document
        self.max_inflight = max(2, self.workers * 3)

    # -- public ---------------------------------------------------------------------

    def run(self, paths: Iterable[str | os.PathLike]) -> BatchSummary:
        paths = [str(p) for p in paths]
        self._summary = BatchSummary(files_total=len(paths))
        self._methods: Counter[str] = Counter()
        self._done = 0
        self._seen: dict[str, str] = {}  # doc_id -> first path
        if self.output_dir:
            (self.output_dir / "documents").mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        self._pending_paths = deque((p, 0) for p in paths)
        self._ready_units: deque[tuple[WorkUnit, int]] = deque()
        self._docs: dict[str, _DocState] = {}
        self._inflight: dict[Future, tuple] = {}
        self._quarantine: deque[tuple] = deque()
        self._quarantine_exec = None
        self._quarantine_busy = False
        self._executor = self._new_executor()
        try:
            self._loop()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            if self._quarantine_exec:
                self._quarantine_exec.shutdown(wait=True, cancel_futures=True)

        s = self._summary
        s.wall_time_s = round(time.time() - t0, 3)
        s.pages_by_method = dict(self._methods)
        processed = s.succeeded + s.partial + s.failed
        if s.wall_time_s > 0:
            s.pages_per_second = round(s.pages_total / s.wall_time_s, 2)
            s.documents_per_minute = round(processed * 60 / s.wall_time_s, 1)
        if processed:
            s.success_rate = round((s.succeeded + s.partial) / processed, 4)
            s.failure_rate = round(s.failed / processed, 4)
        if self.output_dir:
            self._write_json(self.output_dir / "summary.json", s.model_dump(mode="json"))
        return s

    # -- scheduling -----------------------------------------------------------------

    def _new_executor(self, workers: int | None = None):
        workers = workers or self.workers
        if self.workers <= 1:
            return _InlineExecutor(self.config)
        threads = max(1, (os.cpu_count() or 1) // workers)
        return ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                   initargs=(self.config, threads))

    @staticmethod
    def _task_fn(kind: str):
        return _unit_task if kind == "unit" else _plan_task

    def _fill(self) -> None:
        # Quarantine: tasks that were in flight when a worker died are retried one at
        # a time in a separate single-worker pool, so a file that really crashes the
        # parser takes down only itself, not the innocent tasks that shared the pool.
        if self._quarantine and not self._quarantine_busy:
            kind, item, attempt = self._quarantine.popleft()
            if self._quarantine_exec is None:
                self._quarantine_exec = self._new_executor(workers=1)
            fut = self._quarantine_exec.submit(self._task_fn(kind), item)
            self._inflight[fut] = (kind, item, attempt, "quarantine")
            self._quarantine_busy = True
        while sum(1 for t in self._inflight.values() if t[3] == "main") < self.max_inflight:
            if self._ready_units:  # finish started documents first
                unit, attempt = self._ready_units.popleft()
                fut = self._executor.submit(_unit_task, unit)
                self._inflight[fut] = ("unit", unit, attempt, "main")
            elif self._pending_paths:
                path, attempt = self._pending_paths.popleft()
                fut = self._executor.submit(_plan_task, path)
                self._inflight[fut] = ("plan", path, attempt, "main")
            else:
                return

    def _loop(self) -> None:
        while True:
            self._fill()
            if not self._inflight:
                return
            done, _ = wait(list(self._inflight), return_when=FIRST_COMPLETED)
            crashed: list[tuple] = []
            for fut in done:
                task = self._inflight.pop(fut)
                if task[3] == "quarantine":
                    self._quarantine_busy = False
                try:
                    result = fut.result()
                except BrokenProcessPool:
                    crashed.append(task)
                    continue
                self._handle(task, result)
            for task in crashed:
                if task[3] == "quarantine":
                    self._quarantine_crashed(task)
            if any(t[3] == "main" for t in crashed):
                self._recover_main_pool([t for t in crashed if t[3] == "main"])

    def _recover_main_pool(self, crashed: list[tuple]) -> None:
        log.error("Worker process died; rebuilding pool, retrying in-flight work in quarantine")
        self._summary.worker_restarts += 1
        # Every task still running in the main pool is lost with it.
        lost = [t for t in self._inflight.values() if t[3] == "main"]
        self._inflight = {f: t for f, t in self._inflight.items() if t[3] != "main"}
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = self._new_executor()
        for kind, item, attempt, _ in [*crashed, *lost]:
            if attempt < MAX_RETRIES:
                self._quarantine.append((kind, item, attempt + 1))
            else:
                self._give_up(kind, item)

    def _quarantine_crashed(self, task: tuple) -> None:
        # Running alone, so this task is the culprit: no further retries.
        kind, item, _, _ = task
        log.error("Task crashed its worker again in isolation: %s", item)
        self._summary.worker_restarts += 1
        self._quarantine_exec.shutdown(wait=False, cancel_futures=True)
        self._quarantine_exec = None
        self._give_up(kind, item)

    def _give_up(self, kind: str, item) -> None:
        if kind == "unit":
            self._handle(("unit", item), UnitResult(item, issues=[Issue.error(
                IssueCode.INTERNAL_ERROR, "Worker process crashed while processing these pages")]))
        else:
            self._finish_fatal(item)

    def _handle(self, task: tuple, result) -> None:
        kind = task[0]
        if kind == "plan":
            plan: Plan = result
            if plan.fatal:
                self._finish(assemble(plan, []))
                return
            if plan.doc_id in self._seen:
                self._summary.duplicates += 1
                self._progress_skip(plan, f"duplicate of {self._seen[plan.doc_id]}")
                return
            self._seen[plan.doc_id] = plan.metadata.path
            if self.resume and self.output_dir and self._output_path(plan.doc_id).exists():
                self._summary.skipped_existing += 1
                self._progress_skip(plan, "already processed")
                return
            self._docs[plan.doc_id] = _DocState(plan, remaining=len(plan.units))
            self._ready_units.extend((u, 0) for u in plan.units)
        else:
            unit_result: UnitResult = result
            state = self._docs[unit_result.unit.doc_id]
            state.results.append(unit_result)
            state.remaining -= 1
            if state.remaining == 0:
                del self._docs[unit_result.unit.doc_id]
                self._finish(assemble(state.plan, state.results))

    def _finish_fatal(self, path: str) -> None:
        p = Path(path)
        plan = Plan(doc_id=f"path:{p}", metadata=_metadata(p), started_at=datetime.now(timezone.utc),
                    t0=time.time(), fatal=True,
                    issues=[Issue.error(IssueCode.INTERNAL_ERROR, "Worker process crashed while reading file")])
        self._finish(assemble(plan, []))

    # -- bookkeeping ----------------------------------------------------------------

    def _finish(self, doc: ProcessedDocument) -> None:
        s = self._summary
        if doc.status is DocumentStatus.SUCCESS:
            s.succeeded += 1
        elif doc.status is DocumentStatus.PARTIAL:
            s.partial += 1
        else:
            s.failed += 1
        if doc.status is not DocumentStatus.SUCCESS:
            errors = [i for i in doc.all_issues() if i.severity == "error"]
            s.failures.append(FailureRecord(
                path=doc.metadata.path, status=doc.status,
                codes=sorted({i.code.value for i in errors}),
                message=errors[0].message if errors else "",
            ))
        s.needs_review += int(doc.needs_review)
        s.pages_total += doc.stats.pages_total
        s.pages_failed += doc.stats.pages_failed
        for p in doc.pages:
            self._methods[p.method.value] += 1

        if self.output_dir:
            if not doc.document_id.startswith("path:"):
                self._write_json(self._output_path(doc.document_id), doc.model_dump(mode="json"))
            with open(self.output_dir / "index.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "path": doc.metadata.path, "document_id": doc.document_id,
                    "status": doc.status.value, "type": doc.metadata.detected_type.value,
                    "pages": doc.stats.pages_total, "needs_review": doc.needs_review,
                    "duration_ms": doc.duration_ms,
                }) + "\n")
        self._done += 1
        if self.on_document:
            self.on_document(doc, self._done, self._summary.files_total)

    def _progress_skip(self, plan: Plan, reason: str) -> None:
        self._done += 1
        log.info("skip %s (%s)", plan.metadata.path, reason)

    def _output_path(self, doc_id: str) -> Path:
        return self.output_dir / "documents" / f"{doc_id}.json"

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)  # atomic: readers never see a half-written file
