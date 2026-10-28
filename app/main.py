"""FastAPI entrypoint. Run with: uvicorn app.main:app --reload"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Job Application Tracker", version="0.1.0")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
