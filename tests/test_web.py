import logging
import time

from fastapi.testclient import TestClient

from uxcorrelate import web
from uxcorrelate.config import Settings

BODY = {"username": "jdoe", "start": "2026-10-28T09:00:00Z", "end": "2026-10-28T12:00:00Z"}


def wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["done"]:
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_job_reports_progress_lines_then_the_result(monkeypatch):
    def fake_settings(path=None):
        s = Settings()
        s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
        return s

    def fake_investigate(req, aternity, netim, settings):
        logging.getLogger("uxcorrelate").info("NetIM: checked 25 of 231 devices")
        return {"summary": {"verdict": "network"}}

    monkeypatch.setattr(web, "load_settings", fake_settings)
    monkeypatch.setattr(web, "investigate", fake_investigate)
    client = TestClient(web.create_app())
    job = wait(client, client.post("/api/jobs", json=BODY).json()["id"])
    assert job["error"] is None and job["result"]["summary"]["verdict"] == "network"
    assert job["lines"][0].endswith("NetIM: checked 25 of 231 devices")


def test_job_shows_the_error_instead_of_hanging():
    client = TestClient(web.create_app())
    job = wait(client, client.post("/api/jobs", json=BODY).json()["id"])
    assert "base_url is not set" in job["error"]
    assert client.get("/api/jobs/nope").status_code == 404


def test_a_running_job_is_never_dropped_a_finished_one_after_its_minutes(monkeypatch):
    """With 20 kept by count, a 21st start dropped a job still running: its tab said "Unknown job"."""
    import threading
    now = [1000.0]
    hold = threading.Event()

    def fake_investigate(req, aternity, netim, settings):
        if req.username == "slow":
            hold.wait(5)
        return {"summary": {}}

    def fake_settings(path=None):
        s = Settings()
        s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
        return s

    monkeypatch.setattr(web, "_clock", lambda: now[0])
    monkeypatch.setattr(web, "load_settings", fake_settings)
    monkeypatch.setattr(web, "investigate", fake_investigate)
    client = TestClient(web.create_app())
    running = client.post("/api/jobs", json=dict(BODY, username="slow")).json()["id"]
    ids = [client.post("/api/jobs", json=BODY).json()["id"] for _ in range(30)]
    for i in ids:
        wait(client, i)
    now[0] += web.KEEP_DONE_MIN * 60 - 1  # 9 min 59 s on: every one still there
    client.post("/api/jobs", json=BODY)
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 200
    assert client.get(f"/api/jobs/{running}").json()["done"] is False  # 31 started after it: kept
    now[0] += 2  # past 10 min: the finished ones go, the running one stays
    client.post("/api/jobs", json=BODY)
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 404
    assert client.get(f"/api/jobs/{running}").json()["done"] is False
    hold.set()
    assert wait(client, running)["result"] == {"summary": {}}


def test_logon_job_runs_the_logon_report(monkeypatch):
    def fake_settings(path=None):
        s = Settings()
        s.aternity.base_url = "https://aternity.test/aternity.odata/v2.0"
        return s

    monkeypatch.setattr(web, "load_settings", fake_settings)
    monkeypatch.setattr(web, "logon_report", lambda aternity, start, end, **k: {"total": 0, "filters": k})
    client = TestClient(web.create_app())
    body = {"location": "Denver", "start": BODY["start"], "end": BODY["end"]}
    job = wait(client, client.post("/api/logon-jobs", json=body).json()["id"])
    assert job["error"] is None and job["result"]["filters"]["location"] == "Denver"


def test_the_page_shows_config_titles_after_the_brand(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    page = TestClient(web.create_app(str(path))).get("/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert f"<title>{web.BRAND}</title>" in page.text and "<h1>" not in page.text.split("<main>")[0]  # the brand alone
    path.write_text("title: \"  Field   <Office> \"\n")
    page = TestClient(web.create_app(str(path))).get("/").text
    assert f"<title>{web.BRAND} | Field &lt;Office&gt;</title>" in page  # spaces closed up, never markup
    assert "<i></i><h1>Field &lt;Office&gt;</h1>" in page and "<!--title-->" not in page
    path.write_text("title: [broken\n")
    assert f"<title>{web.BRAND}</title>" in TestClient(web.create_app(str(path))).get("/").text


def test_kubernetes_checks_have_their_own_quiet_address(tmp_path):
    from uxcorrelate.cli import NoHealthCheck
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    assert TestClient(web.create_app(str(path))).get("/api/health").json() == {"ok": True}

    def line(client, path):  # uvicorn's access line: (client, method, path, http version, status)
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 (client, "GET", path, "1.1", 200), None)
    quiet = NoHealthCheck()
    assert not quiet.filter(line("10.42.0.1:51234", "/api/health"))  # the kubelet, from the node
    assert not quiet.filter(line("127.0.0.1:40000", "/"))  # Docker's own check: as before
    assert not quiet.filter(line("10.1.2.3:5000", "/api/status"))
    assert quiet.filter(line("10.1.2.3:5000", "/"))  # someone opening the page is still logged
    assert quiet.filter(line("10.1.2.3:5000", "/api/today"))
