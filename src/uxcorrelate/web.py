"""FastAPI app: REST API plus a single-page troubleshooting UI."""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import http as _http

from .assess import Assessments
from .care import Care
from .chat import ask, chat_service
from .config import Settings, config_notes, load_settings
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
from .usage import Usage, addr_of

STATIC = Path(__file__).parent / "static"
BRAND = "J6 Preservation Society"  # index.html's <title>; config.yaml's title goes after it
KEEP_DONE_MIN = 10  # a finished job is kept this long for its tab (it asks every second); a running one always
_clock = time.monotonic  # the jobs' clock (a test moves it)
NETIM_REFRESH_S = 3600  # rebuild the NetIM device/interface index this often
# the admin page's login (docs/admin-roadmap.md): one password, ADMIN_PASSWORD in .env
ADMIN_ENV = "ADMIN_PASSWORD"
ADMIN_COOKIE = "uxadmin"
ADMIN_HOURS = 8  # a login lasts this long (Log out ends it; a restart too)
ADMIN_TRIES = 5  # wrong passwords from one address before it is locked ...
ADMIN_LOCK_MIN = 15  # ... for this long
# what the calls to Riverbed are for, by the page's request (the admin page's "Load on Riverbed")
PURPOSES = (("/api/software", "Software tab"), ("/api/explain", "Explain"), ("/api/care", "Care list (page)"),
            ("/api/today", "Today tab"), ("/api/investigate", "investigations"), ("/api/names", "name boxes"),
            ("/admin/api", "admin page"))
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
    source: str | None = None  # where it was started: the Investigate tab, or a tab's Investigate ▸ (admin page)


class ChatBody(BaseModel):
    question: str
    history: list[dict] = []
    page: dict | None = None  # Ask anything: the tab the page has open, as shown ({"tab", "lines"})


class ExplainBody(BaseModel):
    tab: str
    panel: str = ""
    lines: list[str] = []
    question: str = ""


class CareBody(BaseModel):
    username: str
    label: str = ""
    pin: str = ""


class CareOrder(BaseModel):
    usernames: list[str]  # every user on the Care list, in the new order
    pin: str = ""


class SeenBody(BaseModel):
    tab: str


class LoginBody(BaseModel):
    password: str = ""


class AdBody(BaseModel):
    """The usernames and desktops on the admin page, to look up in Active Directory."""
    users: list[str] = []
    pcs: list[str] = []


class LogonBody(BaseModel):
    username: str | None = None
    device: str | None = None
    location: str | None = None
    start: datetime
    end: datetime


def _did_not_answer(exc: ApiError) -> str:
    """ "Aternity did not answer (HTTP 504). Try again." — the source's name and its short words,
    never the URL or the reply's body (the page shows this)."""
    if exc.status is None and exc.__cause__ is None:  # our own words (base_url not set): as they are
        return str(exc)
    who = str(exc).split(":", 1)[0].strip() or "Aternity"
    return f"{who} did not answer ({exc.short()}). Try again; `uxcorrelate verify` checks the connection."


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

    threading.Thread(target=_http.as_doing("NetIM index", index_netim), daemon=True, name="netim-index").start()

    # Usernames and device names for the boxes, and the "Today" overview: one background
    # reader of Aternity (started with the server, not in tests that never start it).
    names = app.state.names = NameList()
    try:
        first = load_settings(config_path)
        zone, refresh, sv_match = first.timezone, first.refresh_minutes, first.secureview.match
    except Exception:  # a broken config.yaml: the reader says so
        zone, refresh, sv_match = "", Settings().refresh_minutes, ""
    today = app.state.today = Today(zone, os.environ.get("UXCORRELATE_DATA") or None)
    from .secureview import SecureView, keep_backfilling
    # SecureView on the admin page: the Today reader's hours also go to it (secureview.match, read at start)
    sv = app.state.secureview = today.secureview = SecureView(sv_match, zone, os.environ.get("UXCORRELATE_DATA") or None)
    today.refresh_min = refresh  # how often Aternity is read (config.yaml refresh_minutes)
    from . import explain as _explain
    _explain.set_refresh(refresh)
    care = app.state.care = Care(today, lambda: load_settings(config_path).care_list,
                                 os.environ.get("UXCORRELATE_DATA") or None, names)
    software = app.state.software = Software(zone, os.environ.get("UXCORRELATE_DATA") or None)
    status = app.state.status = Status()  # the connection lights in the top bar
    # the admin page's counts: each use, and every call to Riverbed and the AI services (http.METER)
    usage = app.state.usage = Usage(today, os.environ.get("UXCORRELATE_DATA") or None)
    _http.METER, _http.CUT = usage.call, usage.cut
    from .ad import Lookup
    ad_lookup = app.state.ad = Lookup(lambda: load_settings(config_path).ad)  # the admin page's names from AD
    care.counts = usage.care_counts
    assess = app.state.assess = Assessments(today, care, status, os.environ.get("UXCORRELATE_DATA") or None,
                                            aternity_of=lambda: aternity_client(),
                                            unit_of=lambda: load_settings(config_path).boot_unit)

    def assess_ai():
        """The AI service that words an assessment (the chats' one), and its name; (None, "") when none is set."""
        from .ai import AiClient
        service = chat_service(load_settings(config_path))
        if service is None:
            return None, ""
        return AiClient(service, os.environ.get(service.key_env, "") if service.key_env else ""), service.name

    def care_investigate(username: str, start: datetime, end: datetime) -> dict:
        """A Care list user's investigation, as the Investigate tab would run it."""
        began = time.monotonic()
        try:
            settings, aternity, netim = sources()
            req = InvestigationRequest(start=start, end=end, username=username)
            result = jsonable_encoder(investigate(req, aternity, netim, settings))
        except Exception as exc:
            usage.investigation(None, "care-auto", time.monotonic() - began, error=short_error(exc))
            usage.care_counts(ran=1)
            raise
        usage.investigation(None, "care-auto", time.monotonic() - began, result)
        usage.care_counts(ran=1)
        return result

    def aternity_client():
        from .aternity import AternityClient
        settings = load_settings(config_path)
        return AternityClient(settings.aternity) if settings.aternity.base_url else None

    @asynccontextmanager
    async def lifespan(_app):
        # the HTTP and HTTPS servers share this app and both start it: read Aternity once
        if not today.started:
            today.started = True
            def after(now):
                assess.tick(now, assess_ai)
                usage.care_counts(now, listed=len(care.users))
                usage.read_done(now)  # the read's time, who is placed now, the day's file
            threading.Thread(target=_http.as_doing("Today reader, Care list checks, assessments", keep_reading),
                             args=(today, names, aternity_client, time.sleep), kwargs={"care": care, "after": after},
                             daemon=True, name="today").start()
        if not software.started:  # once a day: the installed list and application versions
            software.started = True
            threading.Thread(target=_http.as_doing("Software reader", keep_reading_software),
                             args=(software, aternity_client,
                                                                 lambda: load_settings(config_path).aternity, time.sleep),
                             daemon=True, name="software").start()
        if not status.started:
            status.started = True
            threading.Thread(target=_http.as_doing("connection lights", keep_checking),
                             args=(status, lambda: load_settings(config_path), time.sleep),
                             kwargs={"notes_of": lambda: config_notes(config_path, zone=False)}, daemon=True,
                             name="status").start()
        if sv.on() and not sv.started:  # Aternity's daily data for the days before SecureView's records
            sv.started = True
            threading.Thread(target=_http.as_doing("SecureView daily read", keep_backfilling),
                             args=(sv, today, aternity_client, time.sleep), daemon=True, name="secureview").start()
        if not care.started:
            care.started = True
            threading.Thread(target=_http.as_doing("Care list investigations", care.work),
                             args=(care_investigate, time.sleep), daemon=True,
                             name="care").start()
        yield
        usage.save(force=True)  # a restart: the admin page's counts since the last write (at most a minute's)
        sv.save(datetime.now(timezone.utc))

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def purpose(request: Request, call_next):
        """What the calls a request makes to Riverbed are for (http.PURPOSE: the admin page)."""
        path = request.url.path
        with _http.doing(next((p for prefix, p in PURPOSES if path.startswith(prefix)), "page")):
            response = await call_next(request)
        if path.startswith("/api/software/"):  # a click on the Software tab (or its old chat)
            usage.run("software", addr_of(request), by=path.split("/")[3], ok=response.status_code < 400)
        return response

    @app.post("/api/seen")
    def tab_seen(body: SeenBody, request: Request):
        """The page's beacon: a tab opened (at load and at each switch), for the admin page."""
        usage.seen(addr_of(request), body.tab)
        return {"ok": True}

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

    @app.get("/api/MSS/care/status")
    def care_status(request: Request):
        """Every Care list tile for another platform that asks every few minutes: username, label,
        alert / ok / unknown, level, since and a short summary, all in UTC (care.status)."""
        usage.poll(addr_of(request))
        return care.status(datetime.now(timezone.utc))

    def care_check_now() -> None:
        """After a change on the page: the new tile's history and issues now, not at the next read."""
        if not today.ready:
            return  # the reader checks the list itself once it has read Aternity
        try:
            care.check(aternity_client(), datetime.now(timezone.utc), read=False)  # not a read: no alert read counted
        except Exception as exc:
            log.warning("Care list: the check after a change on the page failed (%s)", short_error(exc))

    def care_pin(pin: str, request: Request, what: str) -> None:
        """Every change of the Care list needs its PIN (care.pin); a wrong one is logged."""
        if (pin or "").strip() != care.pin(datetime.now(timezone.utc)):
            log.warning("Care list: a wrong PIN to %s, from %s", what, request.client.host if request.client else "?")
            usage.care_change(addr_of(request), what, False)
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
        threading.Thread(target=_http.as_doing("Care list (page)", care_check_now), daemon=True,
                         name="care-add").start()
        usage.care_change(addr_of(request), f"add {user.username}", True)
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
        usage.care_change(addr_of(request), f"change the label of {user.username}", True)
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
        usage.care_change(addr_of(request), f"remove {gone.username}", True)
        return {"username": gone.username, "label": gone.label}

    @app.put("/api/care/order")
    def care_order(body: CareOrder, request: Request):
        """The order of the tiles (the page's Arrange tiles): the list file in that order. 409: the
        list was changed while it was arranged."""
        care_pin(body.pin, request, "change the order of the tiles")
        try:
            changed = care.reorder(body.usernames, request.client.host if request.client else "")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"The Care list could not be changed ({short_error(exc)}). Its file is "
                                     "care_list.json in the data folder, writable by the container (uid 1000).")
        if changed:
            usage.care_change(addr_of(request), "change the order of the tiles", True)
        return {"changed": changed}

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
            answer = ask(body.question, body.history, client, today, names, datetime.now(timezone.utc),
                         aternity_client)
        except ApiError as exc:
            log.warning("Chat: the AI service %s did not answer (%s)", service.name, exc.short())
            raise HTTPException(502, f"The AI service {service.name} did not answer ({exc.short()}). Try again; "
                                     "`uxcorrelate verify --ai` checks it.")
        r = answer["result"]
        log.info("Chat: \"%s\" answered in %.1f s by %s%s%s", answer["question"][:80], answer["seconds"],
                 service.name, f" with {r['block']} ({', '.join(f'{k}={v}' for k, v in r['args'].items() if v)})"
                 if r else " with no building block", f"; {answer['note']}" if answer["note"] else "")
        return answer

    def ask_investigate(addr, username, device, application, start: datetime, end: datetime) -> dict:
        """An investigation asked for in Ask anything, as the Investigate tab runs it."""
        began = time.monotonic()
        try:
            settings, aternity, netim = sources()
            req = InvestigationRequest(start=start, end=end, username=username, device=device,
                                       application=application)
            result = investigate(req, aternity, netim, settings)
        except Exception as exc:
            usage.investigation(addr, "ask", time.monotonic() - began, error=short_error(exc))
            raise
        usage.investigation(addr, "ask", time.monotonic() - began, result)
        return result

    @app.post("/api/ask")
    def ask_anything(body: ChatBody, request: Request):
        """Ask anything (agent.py): the AI looks things up, as many steps as it needs, and answers.
        Run as a job (some look-ups read Aternity now, an investigation takes minutes): the page
        asks /api/jobs/{id} every second and shows each look-up as it starts."""
        from . import agent
        from .ai import AiClient
        settings = load_settings(config_path)
        service = chat_service(settings)
        if service is None:
            raise HTTPException(503, "The chat needs an AI service: ai.services in config.yaml "
                                     "(uxcorrelate verify --ai checks it).")
        if not body.question.strip():
            raise HTTPException(400, "Type a question.")
        if not today.ready:
            raise HTTPException(503, "Still reading Aternity: ask again in a minute.")
        client = AiClient(service, os.environ.get(service.key_env, "") if service.key_env else "")
        known = {a for h in list(today.hours.values()) for a in h.apps if a != NO_APP}  # as the Software chat
        with today.lock:
            known |= {x[5] for x in today.health if x[5]}
            known |= {k[1] for kinds in today.health_apps.values() for k in kinds if k[1]}
        addr = addr_of(request)
        reach = agent.Reach(aternity_of=aternity_client, cfg=settings.aternity, software=software,
                            known=sorted(known), investigate=lambda *a: ask_investigate(addr, *a),
                            boot_unit=settings.boot_unit)

        def work(body: ChatBody) -> dict:
            tab = _explain.TABS.get(str((body.page or {}).get("tab")))
            log.info("Ask anything: \"%s\", asked of %s%s", " ".join(body.question.split())[:80], service.name,
                     f" on the {tab} tab" if tab else "")
            began = time.monotonic()
            page_tab = str((body.page or {}).get("tab") or "") or None
            try:
                answer = agent.ask(body.question, body.history, client, today, names, datetime.now(timezone.utc),
                                   reach, body.page)
            except ApiError as exc:
                log.warning("Ask anything: the AI service %s did not answer (%s)", service.name, exc.short())
                usage.asked(addr, body.question, time.monotonic() - began, error=exc.short(), tab=page_tab)
                raise HTTPException(502, f"The AI service {service.name} did not answer ({exc.short()}). Try "
                                         "again; `uxcorrelate verify --ai` checks it.")
            except Exception as exc:
                usage.asked(addr, body.question, time.monotonic() - began, error=short_error(exc), tab=page_tab)
                raise
            usage.asked(addr, body.question, time.monotonic() - began, answer, tab=page_tab)
            log.info("Ask anything: \"%s\" answered in %.1f s by %s after %d look-up(s)%s%s",
                     answer["question"][:80], answer["seconds"], service.name, len(answer["steps"]),
                     f" ({answer['looked'][:200]})" if answer["steps"] else "",
                     f"; {answer['note']}" if answer["note"] else "")
            return answer
        return start(work, body, "A question to Ask anything", "Ask anything")

    @app.get("/api/assess")
    def assessment():
        """The assessments for executives: today's, latest first, and yesterday's summary."""
        return assess.view(datetime.now(timezone.utc))

    @app.post("/api/explain")
    def explain_panel(body: ExplainBody, request: Request):
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
        usage.run("explain", addr_of(request), tab=body.tab, s=a.get("seconds"))
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
        return HTMLResponse(page(), headers={"Cache-Control": "no-cache"})

    def page() -> str:
        """index.html with config.yaml's title after the brand, in the top bar and the browser tab."""
        text = (STATIC / "index.html").read_text(encoding="utf-8")
        try:
            title = load_settings(config_path).title
        except Exception:  # a broken config.yaml: the page shows the brand alone, /api/info says what is wrong
            title = ""
        if not title:
            return text
        t = html.escape(title)
        return (text.replace(f"<title>{BRAND}</title>", f"<title>{BRAND} | {t}</title>", 1)
                .replace("<!--title-->", f"<i></i><h1>{t}</h1>", 1))

    @app.get("/api/health")
    def health():
        """Kubernetes' checks that the app answers (k8s/uxcorrelate.yaml): no config read, no Riverbed call,
        left out of the log (cli.QUIET). Docker's own check still asks for the page."""
        return {"ok": True}

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
        req = InvestigationRequest(
            start=utc(body.start), end=utc(body.end),
            username=body.username or None, device=body.device or None,
            application=body.application or None,
        )
        try:  # only what was asked, or config.yaml, is the caller's to fix: a fault of ours is logged
            req.validate()  # (any ValueError inside an investigation was a 400 with Python's words)
            settings, aternity, netim = sources()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if body.bucket_minutes:
            settings.bucket_minutes = body.bucket_minutes
        try:
            return investigate(req, aternity, netim, settings)
        except ApiError as exc:  # its own words, no URL or reply body on the page
            log.warning("Investigation: %s", exc)
            raise HTTPException(502, _did_not_answer(exc))

    def counted(body: InvestigateBody, addr: str | None, by: str) -> dict:
        """An investigation, counted for the admin page: who, from where, how long, what it found first."""
        began = time.monotonic()
        try:
            result = run_investigation(body)
        except HTTPException as exc:
            usage.investigation(addr, by, time.monotonic() - began, error=str(exc.detail))
            raise
        except Exception as exc:
            usage.investigation(addr, by, time.monotonic() - began, error=short_error(exc))
            raise
        usage.investigation(addr, by, time.monotonic() - began, result)
        return result

    @app.post("/api/investigate")
    def run(body: InvestigateBody, request: Request):
        return counted(body, addr_of(request), body.source or "API")

    # The UI runs investigations as jobs so it can show progress while NetIM is searched.
    job_log = JobLog()
    log.addHandler(job_log)
    log.setLevel(logging.INFO)
    jobs: dict[str, dict] = {}
    done_at: dict[str, float] = {}  # job id -> when it finished
    jobs_lock = threading.Lock()

    def run_logons(body: LogonBody) -> dict:
        if utc(body.end) <= utc(body.start):
            raise HTTPException(400, "end must be after start")
        try:
            settings, aternity, _ = sources()
            return logon_report(aternity, utc(body.start), utc(body.end), username=body.username or None,
                                device=body.device or None, location=body.location or None,
                                unit=settings.boot_unit)
        except ApiError as exc:
            log.warning("Start-ups: %s", exc)
            raise HTTPException(502, _did_not_answer(exc))

    def run_job(job_id: str, job: dict, work, body, what: str) -> None:
        job_log.by_thread[threading.get_ident()] = job["lines"]
        try:
            job["result"] = work(body)
        except HTTPException as exc:
            job["error"] = exc.detail
        except Exception as exc:  # shown in the UI instead of a silent hang
            log.exception("%s failed", what)
            job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job_log.by_thread.pop(threading.get_ident(), None)
            with jobs_lock:
                done_at[job_id] = _clock()
            job["done"] = True

    def start(work, body, what: str = "An investigation", purpose: str = "investigations") -> dict:
        """A job in its own thread. Jobs finished over KEEP_DONE_MIN ago are dropped; a running
        one never is (with 20 kept by count, a 21st start dropped one still running). purpose:
        what its calls to Riverbed are for (the admin page)."""
        job_id = uuid.uuid4().hex
        with jobs_lock:
            gone = [i for i, t in done_at.items() if _clock() - t > KEEP_DONE_MIN * 60]
            for old in gone:
                jobs.pop(old, None)
                done_at.pop(old, None)
            jobs[job_id] = job = {"lines": [], "done": False, "result": None, "error": None}
        threading.Thread(target=_http.as_doing(purpose, run_job), args=(job_id, job, work, body, what),
                         daemon=True).start()
        return {"id": job_id}

    @app.post("/api/jobs")
    def start_job(body: InvestigateBody, request: Request):
        addr = addr_of(request)
        return start(lambda b: counted(b, addr, b.source or "investigate"), body)

    @app.post("/api/logon-jobs")
    def start_logon_job(body: LogonBody, request: Request):
        addr = addr_of(request)

        def work(b: LogonBody) -> dict:
            began = time.monotonic()
            try:
                result = run_logons(b)
            except Exception:
                usage.run("startups", addr, s=round(time.monotonic() - began, 1), ok=False)
                raise
            usage.run("startups", addr, s=round(time.monotonic() - began, 1), ok=True)
            return result
        return start(work, body, "A start-ups report", "start-ups reports")

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "Unknown job (the app may have restarted); run it again.")
        return jobs[job_id]

    # -- the admin page (docs/admin-roadmap.md): no link from the main app, a login ---------------
    admin = {"sessions": {}, "fails": {}}  # token -> expiry; address -> [wrong tries, locked until]
    admin_lock = threading.Lock()

    def admin_in(request: Request) -> bool:
        token = request.cookies.get(ADMIN_COOKIE) or ""
        with admin_lock:
            until = admin["sessions"].get(token)
            if until is None:
                return False
            if until < time.time():
                admin["sessions"].pop(token, None)
                return False
            return True

    def admin_https(request: Request) -> bool:
        """The password only over HTTPS (on plain HTTP it can be read on the network): the scheme
        the browser used (behind a trusted proxy, its X-Forwarded-Proto)."""
        return request.url.scheme == "https"

    @app.get("/admin")
    def admin_page():
        text = (STATIC / "admin.html").read_text(encoding="utf-8")
        try:
            title = load_settings(config_path).title
        except Exception:
            title = ""
        if title:
            t = html.escape(title)
            text = text.replace("<title>Admin</title>", f"<title>Admin | {t}</title>", 1).replace(
                "<!--title-->", f"<i></i><h1>{t}</h1>", 1)
        return HTMLResponse(text, headers={"Cache-Control": "no-cache"})

    @app.get("/admin/api/me")
    def admin_me(request: Request):
        return {"in": admin_in(request), "https": admin_https(request), "set": bool(os.environ.get(ADMIN_ENV)),
                "host": request.url.hostname or "this host"}

    @app.post("/admin/api/login")
    def admin_login(body: LoginBody, request: Request):
        addr = addr_of(request) or "?"
        password = os.environ.get(ADMIN_ENV, "")
        if not password:
            raise HTTPException(503, f"No admin password is set: put {ADMIN_ENV}=... in .env, then "
                                     "docker compose up -d (a restart does not read .env again).")
        if not admin_https(request):
            raise HTTPException(400, "Log in on the HTTPS address (https://…:8444/admin on Docker): on plain "
                                     "HTTP the password can be read on the network.")
        now = time.time()
        with admin_lock:
            tries, until = admin["fails"].get(addr, [0, 0.0])
            if until > now:
                raise HTTPException(423, f"Locked after {ADMIN_TRIES} wrong passwords: try again after "
                                         f"{max(1, round((until - now) / 60))} min.")
            if not hmac.compare_digest(body.password.encode(), password.encode()):
                tries += 1
                if tries >= ADMIN_TRIES:
                    admin["fails"][addr] = [0, now + ADMIN_LOCK_MIN * 60]
                    log.warning("Admin: locked for %d min after %d wrong passwords from %s", ADMIN_LOCK_MIN,
                                ADMIN_TRIES, addr)
                    raise HTTPException(423, f"Locked after {ADMIN_TRIES} wrong passwords: try again after "
                                             f"{ADMIN_LOCK_MIN} min.")
                admin["fails"][addr] = [tries, 0.0]
                log.warning("Admin: a wrong password from %s (%d of %d before a %d-minute lock)", addr, tries,
                            ADMIN_TRIES, ADMIN_LOCK_MIN)
                raise HTTPException(403, f"Wrong password ({ADMIN_TRIES - tries} more tries before a "
                                         f"{ADMIN_LOCK_MIN}-minute lock).")
            admin["fails"].pop(addr, None)
            token = secrets.token_urlsafe(32)
            admin["sessions"] = {k: v for k, v in admin["sessions"].items() if v > now}
            admin["sessions"][token] = now + ADMIN_HOURS * 3600
        log.info("Admin: logged in from %s (for %d hours)", addr, ADMIN_HOURS)
        response = JSONResponse({"in": True})
        response.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_HOURS * 3600, path="/admin", httponly=True,
                            secure=True, samesite="strict")
        return response

    @app.post("/admin/api/logout")
    def admin_logout(request: Request):
        with admin_lock:
            admin["sessions"].pop(request.cookies.get(ADMIN_COOKIE) or "", None)
        log.info("Admin: logged out from %s", addr_of(request) or "?")
        response = JSONResponse({"in": False})
        response.delete_cookie(ADMIN_COOKIE, path="/admin")
        return response

    @app.get("/admin/api/usage")
    def admin_usage(request: Request, day: str = ""):
        if not admin_in(request):
            raise HTTPException(401, "Log in first.")
        if day and not (len(day) == 10 and day[4] == "-" and day[7] == "-" and day.replace("-", "").isdigit()):
            raise HTTPException(400, "day must be YYYY-MM-DD")
        return usage.view(datetime.now(timezone.utc), day or None)

    @app.post("/admin/api/ad")
    def admin_ad(body: AdBody, request: Request):
        """The page's usernames and desktops in Active Directory: each looked up once a day, in
        memory only (ad.Lookup)."""
        if not admin_in(request):
            raise HTTPException(401, "Log in first.")
        return ad_lookup.look(body.users, body.pcs, datetime.now(timezone.utc))

    @app.get("/admin/api/secureview")
    def admin_secureview(request: Request):
        """SecureView: the devices people sat at that match secureview.match, today and since the
        records began (secureview.py)."""
        if not admin_in(request):
            raise HTTPException(401, "Log in first.")
        return sv.view(datetime.now(timezone.utc), today.as_of, today.ready, today.off)

    @app.get("/admin/api/secureview/device")
    def admin_secureview_device(request: Request, name: str = ""):
        """One device's last 90 days: a square a day, and the days with times and people."""
        if not admin_in(request):
            raise HTTPException(401, "Log in first.")
        got = sv.device(name[:256], datetime.now(timezone.utc))
        if got is None:
            raise HTTPException(404, f"SecureView has no record of {name[:256]}.")
        return got

    return app
