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
from .logons import logon_report
from .models import utc

STATIC = Path(__file__).parent / "static"
KEEP_JOBS = 20
NETIM_REFRESH_S = 3600  # rebuild the NetIM device/interface index this often
log = logging.getLogger("uxcorrelate")


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


class LogonBody(BaseModel):
    username: str | None = None
    device: str | None = None
    location: str | None = None
    start: datetime
    end: datetime


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="UX Correlator", version="0.1.0",
                  description="Correlate Riverbed Aternity user experience with Riverbed NetIM metrics")

    # Finding a user's gateway needs every NetIM device's interfaces (one call per
    # device). A background thread keeps that index in one shared NetIM client, so
    # investigations don't pay for it; a fresh index replaces it every hour.
    shared: dict = {"netim": None}

    def index_netim() -> None:
        from .netim import NetimClient
        while True:
            try:
                settings = load_settings(config_path)
                if not settings.netim.base_url:
                    return
                fresh = NetimClient(settings.netim)
                if shared["netim"] is None:
                    shared["netim"] = fresh  # usable (cold) while it fills
                devices = fresh.devices()
                log.info("NetIM index: reading interfaces of %d devices", len(devices))
                for n, dev in enumerate(devices, 1):
                    fresh.interfaces(dev.id)
                    if n % 50 == 0:
                        log.info("NetIM index: %d of %d devices", n, len(devices))
                shared["netim"] = fresh
                log.info("NetIM index ready: %d devices; investigations no longer wait for it", len(devices))
            except Exception as exc:  # NetIM down: investigations still work, only slower
                log.warning("NetIM index failed, retrying in an hour: %s", exc)
            time.sleep(NETIM_REFRESH_S)

    threading.Thread(target=index_netim, daemon=True, name="netim-index").start()

    def sources():
        from .aternity import AternityClient
        from .netim import NetimClient
        settings = load_settings(config_path)
        aternity = AternityClient(settings.aternity)
        netim = None
        if settings.netim.base_url:
            netim = shared["netim"] or NetimClient(settings.netim)
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
    log.addHandler(job_log)
    log.setLevel(logging.INFO)
    jobs: dict[str, dict] = {}

    def run_logons(body: LogonBody) -> dict:
        if utc(body.end) <= utc(body.start):
            raise HTTPException(400, "end must be after start")
        try:
            settings, aternity, _ = sources()
            return logon_report(aternity, utc(body.start), utc(body.end), username=body.username or None,
                                device=body.device or None, location=body.location or None,
                                unit=settings.boot_unit)
        except ApiError as exc:
            raise HTTPException(502, str(exc))

    def run_job(job: dict, work, body) -> None:
        job_log.by_thread[threading.get_ident()] = job["lines"]
        try:
            job["result"] = work(body)
        except HTTPException as exc:
            job["error"] = exc.detail
        except Exception as exc:  # shown in the UI instead of a silent hang
            log.exception("Investigation failed")
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job_log.by_thread.pop(threading.get_ident(), None)
            job["done"] = True

    def start(work, body) -> dict:
        for old in list(jobs)[:-KEEP_JOBS]:
            jobs.pop(old, None)
        job_id = uuid.uuid4().hex
        jobs[job_id] = job = {"lines": [], "done": False, "result": None, "error": None}
        threading.Thread(target=run_job, args=(job, work, body), daemon=True).start()
        return {"id": job_id}

    @app.post("/api/jobs")
    def start_job(body: InvestigateBody):
        return start(run_investigation, body)

    @app.post("/api/logon-jobs")
    def start_logon_job(body: LogonBody):
        return start(run_logons, body)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Unknown job (the app may have restarted); run it again.")
        return jobs[job_id]

    return app
