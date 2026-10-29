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

    @app.post("/api/investigate")
    def run(body: InvestigateBody):
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

    return app
