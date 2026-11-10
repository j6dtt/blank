"""The connection lights in the page's top bar (`/api/status`): Aternity, NetIM and each
AI service in ai.services, checked every `CHECK_EVERY_S` by one background thread.

Each check is the smallest call of a kind already proven on the live systems:
- Aternity: one activity row of the last few minutes (a time filter and $top=1);
- NetIM: one device (the device list with limit 1);
- an AI service: its model list, with the key and login header the chats use.
A check proves the connection and the login, not that a model words an answer well:
`uxcorrelate verify --ai` does that.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .chat import chat_service
from .config import Settings
from .http import ApiError, short_error

CHECK_EVERY_S = 60
CHECK_TIMEOUT_S = 15.0  # a check waits this long at most, and is not tried again until the next one
ATERNITY_BACK = timedelta(minutes=5)
REFUSED = (401, 403)
NO_LIST = (404, 405)  # an AI service that answers but has no model list: still connected
OK, DOWN, WARN, OFF, WAIT = "ok", "down", "warn", "off", "wait"
log = logging.getLogger("uxcorrelate")


def _item(name: str, kind: str, state: str, text: str, **more) -> dict:
    return {"name": name, "kind": kind, "state": state, "text": text, **more}


def _failed(exc: Exception, login: str) -> str:
    if isinstance(exc, ApiError) and exc.status in REFUSED:
        return f"It answered, but refused {login} ({exc.short()})."
    return f"No answer: {short_error(exc)}."


def _timed(call) -> float:
    started = time.monotonic()
    call()
    return time.monotonic() - started


def check_aternity(settings: Settings, now: datetime, transport=None) -> dict:
    from .aternity import AternityClient
    cfg = settings.aternity
    if not cfg.base_url:
        return _item("Aternity", "api", OFF, "Not set (aternity.base_url in config.yaml).")
    try:
        client = AternityClient(replace(cfg, timeout=CHECK_TIMEOUT_S), transport=transport)
        client.http.retries = 0
        took = _timed(lambda: client.query(
            cfg.entities["activities"], client._time_filter("activities", now - ATERNITY_BACK, now),
            select=[cfg.fields["activities"]["timestamp"]], top=1))
    except Exception as exc:  # shown on the light, never an error of the page
        return _item("Aternity", "api", DOWN, _failed(exc, "the username or password"))
    return _item("Aternity", "api", OK, f"Answered in {took:.1f} s.")


def check_netim(settings: Settings, transport=None) -> dict:
    from .netim import NetimClient
    cfg = settings.netim
    if not cfg.base_url:
        return _item("NetIM", "api", OFF, "Not set (netim.base_url in config.yaml).")
    try:
        client = NetimClient(replace(cfg, timeout=CHECK_TIMEOUT_S), transport=transport)
        client.http.retries = 0
        took = _timed(lambda: client.http.get_json(f"{cfg.api_prefix}/devices", params={"offset": 0, "limit": 1}))
    except Exception as exc:
        return _item("NetIM", "api", DOWN, _failed(exc, "the username or password"))
    return _item("NetIM", "api", OK, f"Answered in {took:.1f} s.")


def check_ai(cfg, used: bool, env=None, transport=None) -> dict:
    """One AI service, with the key and login header the chats send."""
    from .ai import AiClient
    env = os.environ if env is None else env
    name = cfg.name or cfg.model or "AI service"
    more = {"model": cfg.model, "chat": used}
    if not cfg.base_url or not cfg.model:
        return _item(name, "model", OFF, "Not set (base_url and model in ai.services, config.yaml).", **more)
    listed: list[str] | None = None
    try:
        client = AiClient(replace(cfg, timeout=CHECK_TIMEOUT_S), env.get(cfg.key_env, "") if cfg.key_env else "",
                          transport=transport)
        client.http.retries = 0
        started = time.monotonic()
        try:
            listed = client.models()
        except ApiError as exc:
            if exc.status not in NO_LIST:
                raise
        took = time.monotonic() - started
    except Exception as exc:
        return _item(name, "model", DOWN, _failed(exc, "the key"), **more)
    if listed is None:
        return _item(name, "model", OK, f"Answered in {took:.1f} s (it has no model list to look {cfg.model} up in).",
                     **more)
    if listed and cfg.model not in listed:
        return _item(name, "model", WARN, f"Answered in {took:.1f} s, but {cfg.model} is not in its list of "
                     f"{len(listed)} models (uxcorrelate verify --ai prints them).", **more)
    return _item(name, "model", OK, f"Answered in {took:.1f} s; {cfg.model} is listed.", **more)


def check_all(settings: Settings, now: datetime, env=None, transport=None) -> list[dict]:
    used = chat_service(settings)
    return [check_aternity(settings, now, transport), check_netim(settings, transport)] + [
        check_ai(s, s is used, env, transport) for s in settings.ai.services]


class Status:
    """The latest check of every connection, and since when each is in its state."""

    def __init__(self):
        self.started = False
        self.items: list[dict] = []
        self.checked: datetime | None = None
        self.note = ""  # e.g. config.yaml could not be read
        self._since: dict[str, tuple[str, datetime]] = {}

    def set(self, items: list[dict], now: datetime, note: str = "") -> None:
        for i in items:
            key = f"{i['kind']} {i['name']}"
            before = self._since.get(key)
            if before is None or before[0] != i["state"]:
                if before is not None or i["state"] in (DOWN, WARN):
                    log.log(logging.WARNING if i["state"] in (DOWN, WARN) else logging.INFO,
                            "Connection to %s: %s %s", i["name"],
                            {OK: "back.", DOWN: "down.", WARN: "check it.", OFF: "not set."}.get(i["state"], ""),
                            i["text"])
                self._since[key] = (i["state"], now)
            i["since"] = self._since[key][1].isoformat()
        self.items, self.checked, self.note = items, now, note

    def view(self) -> dict:
        return {"checked": self.checked.isoformat() if self.checked else None, "every_s": CHECK_EVERY_S,
                "note": self.note, "items": self.items}


def keep_checking(status: Status, settings_of, sleep, clock=lambda: datetime.now(timezone.utc),
                  rounds: int | None = None) -> None:
    """Runs in a background thread: every connection, every CHECK_EVERY_S."""
    while rounds is None or rounds > 0:
        now = clock()
        try:
            status.set(check_all(settings_of(), now), now)
        except Exception as exc:  # a broken config.yaml: said in the panel, tried again
            status.set([], now, f"The settings could not be read ({short_error(exc)}).")
        if rounds is not None:
            rounds -= 1
        sleep(CHECK_EVERY_S)
