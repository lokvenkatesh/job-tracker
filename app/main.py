"""FastAPI entrypoint. Run with: uvicorn app.main:app --reload"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    scheduler = None
    if settings.scheduler_enabled:
        from app.pipeline.scheduler import start_scheduler

        scheduler = start_scheduler(settings)
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(title="Job Application Tracker", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
