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
