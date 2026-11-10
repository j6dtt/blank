"""The connection lights in the top bar: one small read of Aternity, NetIM and each AI service."""

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from uxcorrelate import status as st
from uxcorrelate import web
from uxcorrelate.config import AiService, Settings

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def settings() -> Settings:
    s = Settings()
    s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
    s.netim.base_url = "https://netim.test"
    s.ai.services = [AiService(name="nemotron", base_url="https://nemo.test/v1", model="nemo-3", key_env="NEMO_KEY"),
                     AiService(name="gpt", base_url="https://gpt.test/v1", model="gpt-x", key_env="GPT_KEY")]
    s.ai.chat = "gpt"
    return s


def service(seen: list, down=(), status=None, models=("gpt-x", "nemo-3")):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = request.url.host
        if host in down:
            raise httpx.ConnectError("refused")
        if status and host in status:
            return httpx.Response(status[host], json={"message": "no"})
        if host == "aternity.test":
            return httpx.Response(200, json={"value": []})
        if host == "netim.test":
            return httpx.Response(200, json={"items": [{"id": "1"}]})
        return httpx.Response(200, json={"data": [{"id": m} for m in models]})
    return httpx.MockTransport(handler)


def states(items) -> dict:
    return {i["name"]: i["state"] for i in items}


def test_every_connection_is_one_small_read():
    seen: list = []
    items = st.check_all(settings(), NOW, {"GPT_KEY": "k1"}, service(seen))
    assert states(items) == {"Aternity": "ok", "NetIM": "ok", "nemotron": "ok", "gpt": "ok"}
    assert [i["chat"] for i in items[2:]] == [False, True] and items[3]["text"].endswith("gpt-x is listed.")
    assert len(seen) == 4  # one call each, no second try
    aternity, netim, nemo, gpt = seen
    assert "$top=1" in str(aternity.url) and "$filter=" in str(aternity.url) and "BUSINESS_ACTIVITIES_RAW" in aternity.url.path
    assert netim.url.path.endswith("/devices") and netim.url.params["limit"] == "1"
    assert gpt.url.path == "/v1/models" and gpt.headers["Authorization"] == "Bearer k1"  # as the chats log in
    assert "Authorization" not in nemo.headers


def test_down_refused_not_listed_and_not_set():
    seen: list = []
    items = st.check_all(settings(), NOW, {}, service(seen, down=("netim.test",), status={"aternity.test": 401,
                                                                                         "nemo.test": 404},
                                                      models=("other",)))
    by = {i["name"]: i for i in items}
    assert by["Aternity"]["state"] == "down" and "refused the username or password" in by["Aternity"]["text"]
    assert by["NetIM"]["state"] == "down" and by["NetIM"]["text"].startswith("No answer: no connection")
    assert len([r for r in seen if r.url.host == "netim.test"]) == 1  # not tried again until the next check
    assert by["nemotron"]["state"] == "ok" and "no model list" in by["nemotron"]["text"]
    assert by["gpt"]["state"] == "warn" and "gpt-x is not in its list of 1 models" in by["gpt"]["text"]
    assert "test" not in by["NetIM"]["text"] and "http" not in by["Aternity"]["text"].lower()  # no URL on the page
    refused = st.check_ai(settings().ai.services[1], True, {"GPT_KEY": "bad"}, service([], status={"gpt.test": 403}))
    assert refused["state"] == "down" and "refused the key" in refused["text"]
    assert st.check_ai(settings().ai.services[1], True, {}, service([], status={"gpt.test": 500}))["state"] == "down"
    assert states(st.check_all(Settings(), NOW)) == {"Aternity": "off", "NetIM": "off"}


def test_since_when_and_the_endpoint(monkeypatch, caplog):
    s = st.Status()
    later = NOW.replace(minute=5)
    ok = lambda: [st._item("Aternity", "api", "ok", "Answered in 0.1 s.")]  # noqa: E731
    s.set(ok(), NOW)
    s.set(ok(), later)
    assert s.view()["items"][0]["since"] == NOW.isoformat() and s.view()["checked"] == later.isoformat()
    with caplog.at_level("INFO", logger="uxcorrelate"):
        s.set([st._item("Aternity", "api", "down", "No answer: HTTP 503.")], later)
        s.set(ok(), later.replace(minute=7))
    assert s.view()["items"][0]["since"] == later.replace(minute=7).isoformat()
    assert [r.getMessage() for r in caplog.records] == ["Connection to Aternity: down. No answer: HTTP 503.",
                                                        "Connection to Aternity: back. Answered in 0.1 s."]

    app = web.create_app()
    assert TestClient(app).get("/api/status").json() == {"checked": None, "every_s": 60, "note": "", "items": []}
    monkeypatch.setattr(st, "check_all", lambda settings, now: [st._item("NetIM", "api", "down", "No answer: no reply.")])
    st.keep_checking(app.state.status, Settings, lambda s: None, lambda: NOW, rounds=1)
    got = TestClient(app).get("/api/status").json()
    assert got["items"][0]["state"] == "down" and got["checked"] == NOW.isoformat()

    def broken():
        raise ValueError("config.yaml: line 3")
    st.keep_checking(app.state.status, broken, lambda s: None, lambda: NOW, rounds=1)
    assert "could not be read (config.yaml: line 3)" in TestClient(app).get("/api/status").json()["note"]
