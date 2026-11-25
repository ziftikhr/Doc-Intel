"""FastAPI application.

    docintel serve                 # http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from docintel.db.session import check_connection, get_engine, init_db, session_factory
from docintel.extraction.llm import make_llm
from docintel.jobs.worker import Pipeline, start_workers
from docintel.settings import Settings, load_env

from .routes import queue_counts, router
from .schemas import HealthOut
from .services import ServiceError

log = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None, *, pipeline: Optional[Pipeline] = None,
               inline_worker: Optional[bool] = None, init_schema: bool = True) -> FastAPI:
    if settings is None:
        load_env()
        settings = Settings.from_env()
    run_workers = settings.inline_worker if inline_worker is None else inline_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_schema:
            init_db(get_engine(settings.database_url))
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        threads, stop = ([], None)
        if run_workers:
            threads, stop = start_workers(app.state.session_factory, app.state.pipeline, settings.worker_threads)
        app.state.worker_count = len(threads)
        yield
        if stop is not None:
            stop.set()
            for t in threads:
                t.join(timeout=10)

    app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan,
                  description="Upload invoices (PDF, images, spreadsheets); they are read (OCR), extracted "
                              "(AI + rules), validated and indexed for semantic search in the background.")
    app.state.settings = settings
    app.state.session_factory = session_factory(settings.database_url)
    app.state.pipeline = pipeline or Pipeline.from_settings(settings)
    app.state.worker_count = 0

    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        body = {"detail": exc.message}
        if exc.details:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.status, content=body)

    @app.get("/health", response_model=HealthOut, tags=["health"])
    def health(request: Request):
        ok, db_info = check_connection(get_engine(settings.database_url))
        llm, reason = make_llm(app.state.pipeline.extraction)
        llm_info = f"{llm.provider}:{llm.model}" if llm else f"rules only ({reason})"
        queue = {}
        if ok:
            with app.state.session_factory() as s:
                queue = queue_counts(s)
        return HealthOut(status="ok" if ok else "degraded", database=db_info, llm=llm_info,
                         embeddings=app.state.pipeline.embedder_info or "none",
                         workers=request.app.state.worker_count, queue=queue)

    app.include_router(router)
    return app
