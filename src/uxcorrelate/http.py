"""Thin httpx wrapper with retries and readable errors."""

from __future__ import annotations

import contextvars
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}

# The admin page's counts of what the app asks of Riverbed and the AI services (usage.py): every
# call made through a RestClient is told to METER(system, purpose, source, seconds, rows, retries,
# ok), and a read Aternity cut short to CUT(system, purpose, source). None: nothing is counted
# (the CLI, tests). The purpose is what the app was doing ("Today reader", "investigations"…),
# set by web.py around each thread and request (doing).
METER = None
CUT = None
PURPOSE: contextvars.ContextVar[str] = contextvars.ContextVar("purpose", default="other")


@contextmanager
def doing(purpose: str):
    """What the calls made inside are for (the admin page's "Load on Riverbed")."""
    token = PURPOSE.set(purpose)
    try:
        yield
    finally:
        PURPOSE.reset(token)


def as_doing(purpose: str, target):
    """A thread's target whose calls are counted under `purpose` (a new thread starts with none)."""
    def run(*args, **kwargs):
        with doing(purpose):
            return target(*args, **kwargs)
    return run


def source_of(url: str) -> str:
    """What was read, from the URL: Aternity's data source ("BUSINESS_ACTIVITIES_RAW"), NetIM's
    or the AI service's path without ids ("devices/interfaces")."""
    parts = [p for p in urlsplit(str(url)).path.split("/") if p and not p.isdigit()]
    ver = [i for i, p in enumerate(parts) if re.fullmatch(r"v\d+(\.\d+)*", p)]
    parts = parts[ver[-1] + 1:] if ver and ver[-1] + 1 < len(parts) else parts
    return "/".join(parts[-2:]) or "(root)"


def cut_short(system: str, source: str) -> None:
    if CUT is not None:
        try:
            CUT(system, PURPOSE.get(), source)
        except Exception:  # counting never stops a read
            pass


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason  # the server's own short message, if it gave one

    def short(self) -> str:
        """For people: the server's own message or the HTTP status, never the URL or body."""
        if self.reason:
            return self.reason
        if self.status:
            return f"HTTP {self.status}"
        cause = self.__cause__
        return f"no connection ({type(cause).__name__})" if cause else "no reply"


def short_error(exc: Exception) -> str:
    """Any error in a few words for a person (an ApiError without its URL or body)."""
    return exc.short() if isinstance(exc, ApiError) else (str(exc)[:120] or type(exc).__name__)


class RestClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 60.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        if not base_url:
            raise ApiError(f"{name}: base_url is not set; add {name.lower()}.base_url to config.yaml "
                           "(see config.example.yaml)")
        self.name = name
        self.retries = retries
        self._tries = threading.local()  # retries of the call in this thread (a client is shared by threads)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(username, password) if username else None,
            verify=False,  # Riverbed hosts often use self-signed certificates
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ApiError(f"{self.name}: {method} {url} failed: {exc}") from exc
                self._tries.n = getattr(self._tries, "n", 0) + 1
                time.sleep(2**attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < self.retries:
                self._tries.n = getattr(self._tries, "n", 0) + 1
                time.sleep(2**attempt)
                continue
            if response.is_error:
                m = re.search(r"<message>(.*?)</message>|\"message\"\s*:\s*\"([^\"]*)", response.text, re.S)
                raise ApiError(
                    f"{self.name}: {method} {response.request.url} -> "
                    f"HTTP {response.status_code}: {response.text[:300]}",
                    response.status_code, ((m.group(1) or m.group(2)) if m else "").strip()[:200],
                )
            return response
        raise AssertionError("unreachable")

    def get_json(self, url: str, **kwargs):
        return self._metered("GET", url, **kwargs)

    def post_json(self, url: str, body, **kwargs):
        return self._metered("POST", url, json=body, **kwargs)

    def _metered(self, method: str, url: str, **kwargs):
        """A call and its JSON, told to METER: seconds, rows (Aternity's "value", NetIM's "items"),
        retries, answered or not."""
        if METER is None:
            return self._json(self.request(method, url, **kwargs))
        self._tries.n = 0
        start, ok, rows = time.monotonic(), False, None
        try:
            data = self._json(self.request(method, url, **kwargs))
            ok = True
            if isinstance(data, dict):
                got = data.get("value", data.get("items"))
                rows = len(got) if isinstance(got, list) else None
            elif isinstance(data, list):
                rows = len(data)
            return data
        finally:
            try:
                METER(self.name, PURPOSE.get(), source_of(url), time.monotonic() - start, rows,
                      getattr(self._tries, "n", 0), ok)
            except Exception:  # counting never stops a read
                pass

    def _json(self, response: httpx.Response):
        """The reply as JSON. Not JSON (a proxy's or login page with status 200) is an ApiError,
        as every other failed read: a plain decode error got past the guards that keep an
        investigation or a reader going."""
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"{self.name}: {response.request.method} {response.request.url} -> not JSON: "
                           f"{response.text[:300]}", response.status_code, "the reply was not JSON") from exc

    def close(self) -> None:
        self._client.close()
