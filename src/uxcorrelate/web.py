"""FastAPI app: REST API plus a single-page troubleshooting UI."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import load_settings
from .correlate import InvestigationRequest, investigate
from .http import ApiError
from .models import utc

STATIC = Path(__file__).parent / "static"
KEEP_JOBS = 20


class JobLog(logging.Handler):
    """Copies the "uxcorrelate" progress lines of each job's thread into that job."""

    def __init__(self):
        super().__init__()
        self.by_thread: dict[int, list[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        lines = self.by_thread.get(record.thread)
        if lines is not None:
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.getMessage()}")


class InvestigateBody(BaseModel):
    username: str | None = None
    device: str | None = None
    application: str | None = None
    start: datetime
    end: datetime
    bucket_minutes: int | None = None


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="UX Correlator", version="0.1.0",
                  description="Correlate Riverbed Aternity user experience with Riverbed NetIM metrics")

    def sources():
        from .aternity import AternityClient
        from .netim import NetimClient
        settings = load_settings(config_path)
        aternity = AternityClient(settings.aternity)
        netim = NetimClient(settings.netim) if settings.netim.base_url else None
        return settings, aternity, netim

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info():
        try:
            settings, _, _ = sources()
        except ApiError as exc:
            raise HTTPException(503, str(exc))
        return {
            "applications": sorted(settings.topology.applications),
            "bucket_minutes": settings.bucket_minutes,
        }

    def run_investigation(body: InvestigateBody) -> dict:
        try:
            settings, aternity, netim = sources()
            if body.bucket_minutes:
                settings.bucket_minutes = body.bucket_minutes
            req = InvestigationRequest(
                start=utc(body.start), end=utc(body.end),
                username=body.username or None, device=body.device or None,
                application=body.application or None,
            )
            return investigate(req, aternity, netim, settings)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except ApiError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/investigate")
    def run(body: InvestigateBody):
        return run_investigation(body)

    # The UI runs investigations as jobs so it can show progress while NetIM is searched.
    job_log = JobLog()
    logging.getLogger("uxcorrelate").addHandler(job_log)
    logging.getLogger("uxcorrelate").setLevel(logging.INFO)
    jobs: dict[str, dict] = {}

    def run_job(job: dict, body: InvestigateBody) -> None:
        job_log.by_thread[threading.get_ident()] = job["lines"]
        try:
            job["result"] = run_investigation(body)
        except HTTPException as exc:
            job["error"] = exc.detail
        except Exception as exc:  # shown in the UI instead of a silent hang
            logging.getLogger("uxcorrelate").exception("Investigation failed")
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job_log.by_thread.pop(threading.get_ident(), None)
            job["done"] = True

    @app.post("/api/jobs")
    def start_job(body: InvestigateBody):
        for old in list(jobs)[:-KEEP_JOBS]:
            jobs.pop(old, None)
        job_id = uuid.uuid4().hex
        jobs[job_id] = job = {"lines": [], "done": False, "result": None, "error": None}
        threading.Thread(target=run_job, args=(job, body), daemon=True).start()
        return {"id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Unknown job (the app may have restarted); run it again.")
        return jobs[job_id]

    return app
