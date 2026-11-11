"""FastAPI app: REST API plus a single-page troubleshooting UI."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .assess import Assessments
from .care import Care
from .chat import ask, chat_service
from .config import load_settings
from .correlate import InvestigationRequest, investigate
from .http import ApiError, short_error
from .logons import logon_report
from .models import utc
from .names import KINDS, NameList
from .software import (Software, before_after, compare, missing_on, pcs_with_problem, pcs_with_program,
                       pcs_without_update)
from .software import keep_reading as keep_reading_software
from .software import summary as software_summary
from .status import Status, keep_checking
from .today import NO_APP, Today, keep_reading

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


class ChatBody(BaseModel):
    question: str
    history: list[dict] = []


class ExplainBody(BaseModel):
    tab: str
    panel: str = ""
    lines: list[str] = []
    question: str = ""


class CareBody(BaseModel):
    username: str
    label: str = ""
    pin: str = ""


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
                log.info("NetIM index: reading interfaces of %d devices, %d at a time", len(devices),
                         settings.netim.parallel_reads)
                fresh.read_interfaces(devices, lambda n, total: log.info("NetIM index: %d of %d devices", n, total))
                shared["netim"] = fresh
                log.info("NetIM index ready: %d devices; investigations no longer wait for it", len(devices))
            except Exception as exc:  # NetIM down: investigations still work, only slower
                log.warning("NetIM index could not be read (%s); trying again in an hour", short_error(exc))
            time.sleep(NETIM_REFRESH_S)

    threading.Thread(target=index_netim, daemon=True, name="netim-index").start()

    # Usernames and device names for the boxes, and the "Today" overview: one background
    # reader of Aternity (started with the server, not in tests that never start it).
    names = app.state.names = NameList()
    try:
        zone = load_settings(config_path).timezone
    except Exception:  # a broken config.yaml: the reader says so
        zone = ""
    today = app.state.today = Today(zone, os.environ.get("UXCORRELATE_DATA") or None)
    care = app.state.care = Care(today, lambda: load_settings(config_path).care_list,
                                 os.environ.get("UXCORRELATE_DATA") or None, names)
    software = app.state.software = Software(zone, os.environ.get("UXCORRELATE_DATA") or None)
    status = app.state.status = Status()  # the connection lights in the top bar
    assess = app.state.assess = Assessments(today, care, status, software, os.environ.get("UXCORRELATE_DATA") or None)

    def assess_ai():
        """The AI service that words an assessment (the chats' one), and its name; (None, "") when none is set."""
        from .ai import AiClient
        service = chat_service(load_settings(config_path))
        if service is None:
            return None, ""
        return AiClient(service, os.environ.get(service.key_env, "") if service.key_env else ""), service.name

    def care_investigate(username: str, start: datetime, end: datetime) -> dict:
        """A Care list user's investigation, as the Investigate tab would run it."""
        settings, aternity, netim = sources()
        req = InvestigationRequest(start=start, end=end, username=username)
        return jsonable_encoder(investigate(req, aternity, netim, settings))

    def aternity_client():
        from .aternity import AternityClient
        settings = load_settings(config_path)
        return AternityClient(settings.aternity) if settings.aternity.base_url else None

    @asynccontextmanager
    async def lifespan(_app):
        # the HTTP and HTTPS servers share this app and both start it: read Aternity once
        if not today.started:
            today.started = True
            threading.Thread(target=keep_reading, args=(today, names, aternity_client, time.sleep),
                             kwargs={"care": care, "after": lambda now: assess.tick(now, assess_ai)},
                             daemon=True, name="today").start()
        if not software.started:  # once a day: the installed list and application versions
            software.started = True
            threading.Thread(target=keep_reading_software, args=(software, aternity_client,
                                                                 lambda: load_settings(config_path).aternity, time.sleep),
                             daemon=True, name="software").start()
        if not status.started:
            status.started = True
            threading.Thread(target=keep_checking, args=(status, lambda: load_settings(config_path), time.sleep),
                             daemon=True, name="status").start()
        if not care.started:
            care.started = True
            threading.Thread(target=care.work, args=(care_investigate, time.sleep), daemon=True,
                             name="care").start()
        yield

    app.router.lifespan_context = lifespan

    @app.get("/api/today")
    def today_view():
        return today.view(datetime.now(timezone.utc))

    @app.get("/api/care")
    def care_view():
        return care.view(datetime.now(timezone.utc))

    @app.get("/api/care/user")
    def care_user(username: str):
        """One Care list user: today's issues, 8 days of them, and the latest investigation."""
        got = care.detail(username, datetime.now(timezone.utc))
        if got is None:
            raise HTTPException(404, f"{username} is not on the Care list.")
        return got

    def care_check_now() -> None:
        """After a change on the page: the new tile's history and issues now, not at the next read."""
        if not today.ready:
            return  # the reader checks the list itself once it has read Aternity
        try:
            care.check(aternity_client(), datetime.now(timezone.utc))
        except Exception as exc:
            log.warning("Care list: the check after a change on the page failed (%s)", short_error(exc))

    def care_pin(pin: str, request: Request, what: str) -> None:
        """Every change of the Care list needs its PIN (care.pin); a wrong one is logged."""
        if (pin or "").strip() != care.pin(datetime.now(timezone.utc)):
            log.warning("Care list: a wrong PIN to %s, from %s", what, request.client.host if request.client else "?")
            raise HTTPException(403, "Wrong PIN: the Care list was not changed.")

    @app.post("/api/care/users")
    def care_add(body: CareBody, request: Request):
        """A new tile on the Care list (the page has no login: a PIN, and logged with the caller's address)."""
        care_pin(body.pin, request, f"add {body.username.strip()[:40]}")
        try:
            user = care.add(body.username, body.label, request.client.host if request.client else "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"The Care list could not be changed ({short_error(exc)}). Its file is "
                                     "care_list.json in the data folder, writable by the container (uid 1000).")
        threading.Thread(target=care_check_now, daemon=True, name="care-add").start()
        return {"username": user.username, "label": user.label}

    @app.put("/api/care/users")
    def care_relabel(body: CareBody, request: Request):
        """A new label for a user on the Care list."""
        care_pin(body.pin, request, f"change the label of {body.username.strip()[:40]}")
        try:
            user = care.relabel(body.username, body.label, request.client.host if request.client else "")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"The Care list could not be changed ({short_error(exc)}). Its file is "
                                     "care_list.json in the data folder, writable by the container (uid 1000).")
        if user is None:
            raise HTTPException(404, f"{body.username} is not on the Care list.")
        return {"username": user.username, "label": user.label}

    @app.delete("/api/care/users")
    def care_remove(username: str, request: Request, pin: str = ""):
        """Take a user off the Care list, with their saved issues and investigation."""
        care_pin(pin, request, f"remove {username.strip()[:40]}")
        try:
            gone = care.remove(username, request.client.host if request.client else "")
        except OSError as exc:
            raise HTTPException(500, f"The Care list could not be changed ({short_error(exc)}). Its file is "
                                     "care_list.json in the data folder, writable by the container (uid 1000).")
        if gone is None:
            raise HTTPException(404, f"{username} is not on the Care list.")
        return {"username": gone.username, "label": gone.label}

    @app.get("/api/software")
    def software_view(brief: bool = False):
        """The Software tab; brief: only how many red flags (the tab's label, from any tab)."""
        v = software.view()
        return {"ready": v["ready"], "flags": len(v.get("flags", []))} if brief else v

    @app.get("/api/software/program")
    def software_program(name: str):
        got = software.program(name)
        if got is None:
            raise HTTPException(404, f"{name} is not in the latest installed software list.")
        return got

    def software_read(what: str, work):
        """A read made by a click, at once: Aternity's error is said, never a blank page."""
        client = aternity_client()
        if client is None:
            raise HTTPException(503, "Aternity is not set (aternity.base_url in config.yaml).")
        started = time.monotonic()
        try:
            got = work(client)
        except ApiError as exc:
            raise HTTPException(502, f"Aternity could not be read ({exc.short()}).")
        log.info("Software: %s read in %.1f s", what, time.monotonic() - started)
        return got

    @app.get("/api/software/pcs")
    def software_pcs(name: str = "", version: str | None = None, application: str = "", problem: str = "",
                     update: str = "", windows: str = ""):
        """The PCs with a program (at a version), with a crash, hang time or a Major action on an
        application version, or without a Windows update most PCs of their Windows version have:
        read from Aternity now."""
        now = datetime.now(timezone.utc)
        if update:
            wins, instead = missing_on(software.snap, update, windows or None)
            if not wins:
                raise HTTPException(404, f"{update} is not listed as missing on a Windows version in the latest read.")
            settings = load_settings(config_path)
            return software_read(f"the PCs without {update}",
                                 lambda c: pcs_without_update(c, settings.aternity, update, wins, instead))
        if application:
            if problem not in ("crash", "hang", "slow") or not version:
                raise HTTPException(400, "problem must be crash, hang or slow, with a version")
            return software_read(f"the PCs with {problem} on {application} {version}",
                                 lambda c: pcs_with_problem(c, now, application, version, problem))
        if not name:
            raise HTTPException(400, "name (a program) or application is needed")
        return software_read(f"the PCs with {name} {version or ''}".strip(),
                             lambda c: pcs_with_program(c, name, version))

    @app.get("/api/software/compare")
    def software_compare(application: str, a: str = "", b: str = ""):
        """Two versions of an application side by side on every kind of issue."""
        now = datetime.now(timezone.utc)
        return software_read(f"{application} versions compared",
                             lambda c: compare(c, load_settings(config_path).aternity, now, application,
                                               a or None, b or None))

    @app.get("/api/software/update")
    def software_update(application: str):
        """Before and after an update of the application, on the same PCs."""
        now = datetime.now(timezone.utc)
        return software_read(f"{application} before and after its updates", lambda c: before_after(c, now, application))

    @app.post("/api/software/summary")
    def software_summary_for_management():
        """GPT's summary of the red flags, every number checked against them."""
        from .ai import AiClient
        v = software.view()
        if not v["ready"]:
            raise HTTPException(503, "The installed software is still being read: try again in a few minutes.")
        service = chat_service(load_settings(config_path))
        client = AiClient(service, os.environ.get(service.key_env, "") if service.key_env else "") if service else None
        try:
            got = software_summary(v, client)
        except ApiError as exc:
            log.warning("Software: the AI service %s did not answer (%s)", service.name, exc.short())
            got = software_summary(v, None)
            got["note"] = f"The AI service {service.name} did not answer ({exc.short()}), so these are our own sentences."
        log.info("Software: summary for management written by %s%s", "us" if got["ours"] else service.name,
                 f" ({got['note']})" if got["note"] else "")
        return got

    @app.post("/api/software/chat")
    def software_chat(body: ChatBody):
        """A question on the Software tab: the AI picks a building block, our code counts (some
        read Aternity now, as a click on the tab does)."""
        from .ai import AiClient
        from .software_chat import ask as ask_software
        settings = load_settings(config_path)
        service = chat_service(settings)
        if service is None:
            raise HTTPException(503, "The chat needs an AI service: ai.services in config.yaml "
                                     "(uxcorrelate verify --ai checks it).")
        if not body.question.strip():
            raise HTTPException(400, "Type a question.")
        if software.snap is None:
            raise HTTPException(503, "The installed software is still being read: ask again in a few minutes.")
        client = AiClient(service, os.environ.get(service.key_env, "") if service.key_env else "")
        known = {a for h in list(today.hours.values()) for a in h.apps if a != NO_APP}  # today's app names
        with today.lock:  # and those of health events (an application may be in them only: the Edge browser)
            known |= {x[5] for x in today.health if x[5]}
            known |= {k[1] for kinds in today.health_apps.values() for k in kinds if k[1]}
        try:
            answer = ask_software(body.question, body.history, client, software, aternity_client, settings.aternity,
                                  sorted(known), datetime.now(timezone.utc))
        except ApiError as exc:
            log.warning("Software chat: the AI service %s did not answer (%s)", service.name, exc.short())
            raise HTTPException(502, f"The AI service {service.name} did not answer ({exc.short()}). Try again; "
                                     "`uxcorrelate verify --ai` checks it.")
        r = answer["result"]
        log.info("Software chat: \"%s\" answered in %.1f s by %s%s%s", answer["question"][:80], answer["seconds"],
                 service.name, f" with {r['block']} ({', '.join(f'{k}={v}' for k, v in r['args'].items() if v)})"
                 if r else " with no building block", f"; {answer['note']}" if answer["note"] else "")
        return answer

    @app.get("/api/today/who")
    def today_who(kind: str, name: str = "", category: str = ""):
        """The users and PCs behind a tile or a row of the Today tab."""
        if kind not in ("slow", "app", "event"):
            raise HTTPException(400, "kind must be slow, app or event")
        return today.who(datetime.now(timezone.utc), kind, name, category)

    @app.post("/api/chat")
    def chat_ask(body: ChatBody):
        """A question on the Today tab: the AI picks a building block, our code counts."""
        from .ai import AiClient
        service = chat_service(load_settings(config_path))
        if service is None:
            raise HTTPException(503, "The chat needs an AI service: ai.services in config.yaml "
                                     "(uxcorrelate verify --ai checks it).")
        if not body.question.strip():
            raise HTTPException(400, "Type a question.")
        if not today.ready:
            raise HTTPException(503, "Still reading Aternity: ask again in a minute.")
        client = AiClient(service, os.environ.get(service.key_env, "") if service.key_env else "")
        try:
            answer = ask(body.question, body.history, client, today, names, datetime.now(timezone.utc))
        except ApiError as exc:
            log.warning("Chat: the AI service %s did not answer (%s)", service.name, exc.short())
            raise HTTPException(502, f"The AI service {service.name} did not answer ({exc.short()}). Try again; "
                                     "`uxcorrelate verify --ai` checks it.")
        r = answer["result"]
        log.info("Chat: \"%s\" answered in %.1f s by %s%s%s", answer["question"][:80], answer["seconds"],
                 service.name, f" with {r['block']} ({', '.join(f'{k}={v}' for k, v in r['args'].items() if v)})"
                 if r else " with no building block", f"; {answer['note']}" if answer["note"] else "")
        return answer

    @app.get("/api/assess")
    def assessment():
        """The assessments for executives: today's, latest first, and yesterday's summary."""
        return assess.view(datetime.now(timezone.utc))

    @app.post("/api/explain")
    def explain_panel(body: ExplainBody):
        """ "Explain ▸" on a panel: the AI words the panel's own lines (sent by the page, as
        shown) with our meaning of each term. Nothing is read from Aternity, nothing is saved."""
        from . import explain
        from .ai import AiClient
        if body.tab not in explain.TABS:
            raise HTTPException(400, "tab must be one of " + ", ".join(explain.TABS))
        if not any(x.strip() for x in body.lines):
            raise HTTPException(400, "Nothing is shown on this panel yet.")
        service = chat_service(load_settings(config_path))
        client = AiClient(service, os.environ.get(service.key_env, "") if service.key_env else "") if service else None
        a = explain.explain(client, body.tab, body.panel, body.lines, body.question, service.name if service else "")
        log.info("Explain: \"%s\" on %s, \"%s\": %s in %.1f s%s", a["question"][:80], explain.TABS[body.tab],
                 a["panel"][:60], "our own meanings" if a["ours"] else f"answered by {a['service']}", a["seconds"],
                 f"; {a['note']}" if a["note"] else "")
        return a

    @app.get("/api/names")
    def name_matches(kind: str, q: str = ""):
        if kind not in KINDS:
            raise HTTPException(400, f"kind must be one of {', '.join(KINDS)}")
        return names.match(kind, q)

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
        # always asked for again: after new files are copied, a browser must not show the old page
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/info")
    def info():
        try:
            settings, _, _ = sources()
        except ApiError as exc:
            raise HTTPException(503, str(exc))
        service = chat_service(settings)
        return {
            "applications": sorted(settings.topology.applications),
            "bucket_minutes": settings.bucket_minutes,
            "chat": service.name or service.model if service else None,
        }

    @app.get("/api/status")
    def connections():
        """The top bar's lights: Aternity, NetIM and each AI service, as last checked."""
        return status.view()

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
