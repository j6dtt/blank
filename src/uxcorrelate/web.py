"""FastAPI app: REST API plus a single-page troubleshooting UI."""

from __future__ import annotations

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


class InvestigateBody(BaseModel):
    username: str | None = None
    device: str | None = None
    application: str | None = None
    start: datetime
    end: datetime
    bucket_minutes: int | None = None


def create_app(config_path: str | None = None, demo: bool = False) -> FastAPI:
    app = FastAPI(title="UX Correlator", version="0.1.0",
                  description="Correlate Riverbed Aternity user experience with Riverbed NetIM metrics")

    def sources():
        settings = load_settings(config_path)
        if demo:
            from .demo import demo_sources
            aternity, netim, start, end = demo_sources(settings)
        else:
            from .aternity import AternityClient
            from .netim import NetimClient
            aternity = AternityClient(settings.aternity)
            netim = NetimClient(settings.netim) if settings.netim.base_url else None
            start = end = None
        return settings, aternity, netim, start, end

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info():
        try:
            settings, _, _, start, end = sources()
        except ApiError as exc:
            raise HTTPException(503, str(exc))
        return {
            "demo": demo,
            "applications": sorted(settings.topology.applications),
            "bucket_minutes": settings.bucket_minutes,
            "default_start": start.isoformat() if start else None,
            "default_end": end.isoformat() if end else None,
            "default_user": "jdoe" if demo else None,
        }

    @app.post("/api/investigate")
    def run(body: InvestigateBody):
        try:
            settings, aternity, netim, _, _ = sources()
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

    return app
