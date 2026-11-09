"""`uxcorrelate verify --ai`: a self-hosted AI service checked against a stand-in."""

import json

import httpx

from uxcorrelate.config import AiService, Settings, load_settings
from uxcorrelate.verify import AI_OUTSIDE, AI_TRIES, render_checks, verify_ai

FULL = {"outlook": "Microsoft Outlook", "mossley": "MOSSLEY"}  # what a model sends for the words asked
PICKS = {q: {k: FULL.get(v, v) if isinstance(v, str) else v for k, v in want.items()} for q, want in AI_TRIES}


def service(seen, picks=PICKS, login="bearer", models=("model-a", "model-b"), tools=True, numbers="2,885 and 412"):
    """A stand-in OpenAI-style service: picks from `picks`, then words the counts."""
    def handler(request: httpx.Request):
        seen.append(request)
        key = (request.headers.get("authorization", "").removeprefix("Bearer ") if login == "bearer"
               else request.headers.get("api-key", ""))
        if key != "k-1":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        body = json.loads(request.content)
        assert set(body) <= {"model", "messages", "tools"}  # no token limit, no temperature
        last = body["messages"][-1]
        if "tools" in body and not tools:
            return httpx.Response(400, json={"error": {"message": "tools are not supported"}})
        if last["role"] == "tool":
            return reply(f"The widest are {numbers} PCs.")
        pick = picks.get(last["content"]) if "tools" in body else None
        if pick:
            args = json.dumps({k: v for k, v in pick.items() if v is not None and k != "tool"})
            return reply(None, [{"id": "call-1", "type": "function",
                                 "function": {"name": pick["tool"], "arguments": args}}])
        return reply("ready" if "tools" not in body else "I cannot answer that from the data.")

    def reply(content, calls=None):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content,
                                                                  "tool_calls": calls}}]})
    return httpx.MockTransport(handler)


def settings(**kw):
    s = Settings()
    s.ai.services.append(AiService(name="one", base_url="https://ai.test/v1", model="model-a", key_env="AI_KEY_1",
                                   **kw))
    return s


def run(seen=None, env=None, cfg=None, **kw):
    checks = verify_ai(cfg or settings(), service(seen if seen is not None else [], **kw),
                       {"AI_KEY_1": "k-1"} if env is None else env)
    return {c.item.split(": ", 1)[1]: c for c in checks}, render_checks(checks)


def test_a_good_service_passes_every_check_and_the_key_is_never_printed():
    seen = []
    checks, text = run(seen)
    assert [c.ok for c in checks.values()] == [None, True, True, True, True]
    assert "Right in 4 of 4 tries" in checks["picks the right question"].message
    assert "made no pick (right)" in checks["picks the right question"].message
    assert "2 of the 2 counts" in checks["repeats the counts"].message
    assert "k-1" not in text and "AI service:" in text and "All checks passed." in text
    assert seen[0].url.path == "/v1/models" and seen[1].url.path == "/v1/chat/completions"


def test_the_api_key_header_is_found_and_said():
    checks, _ = run(login="api-key")
    assert checks["reachable"].ok and "Login: api-key" in checks["reachable"].message
    assert "login: api-key" in checks["login"].message


def test_a_wrong_key_says_which_env_line_and_how_to_reload_it():
    checks, _ = run(env={"AI_KEY_1": "wrong"})
    assert checks["reachable"].ok is False
    assert "AI_KEY_1" in checks["reachable"].fix and "docker compose up -d" in checks["reachable"].fix
    assert "wrong" not in checks["reachable"].message


def test_a_model_not_listed_shows_the_closest_names():
    cfg = settings()
    cfg.ai.services[0].model = "model-aa"
    checks, _ = run(cfg=cfg)
    assert checks["model"].ok is False and "Closest names: model-a" in checks["model"].message


def test_wrong_picks_and_a_pick_for_an_outside_question_fail_with_what_it_picked():
    picks = dict(PICKS)
    first = AI_TRIES[0][0]
    picks[first] = {"tool": "events", "group_by": "day", "days": 7}
    picks[AI_OUTSIDE] = {"tool": "events"}
    checks, _ = run(picks=picks)
    c = checks["picks the right question"]
    assert c.ok is False and "Right in 3 of 4 tries" in c.message and "made a pick (wrong)" in c.message
    assert any(first in r and "picked events(group_by=day, days=7); a right pick is events(group_by=event, days=7)"
               in r for r in c.rows)


def test_a_service_without_tool_calling_is_said():
    checks, _ = run(tools=False)
    c = checks["picks the right question"]
    assert c.ok is False and "tools are not supported" in c.message and "tool calling" in c.fix


def test_a_count_worded_wrong_fails():
    checks, _ = run(numbers="2,858 and 412")
    c = checks["repeats the counts"]
    assert c.ok is False and "1 of the 2 counts" in c.message and "2,858" in c.rows[0]


def test_no_service_set_is_a_note_with_what_to_add_not_a_failure(tmp_path):
    checks = verify_ai(Settings(), None, {})
    assert checks[0].ok is None and "config.example.yaml" in checks[0].message
    path = tmp_path / "config.yaml"
    path.write_text("ai:\n  services:\n    - name: one\n      base_url: https://ai.test/v1\n      model: m\n"
                    "      key_env: AI_KEY_1\n      query: {api-version: '1'}\n")
    got = load_settings(path).ai.services[0]
    assert (got.name, got.model, got.key_env, got.query) == ("one", "m", "AI_KEY_1", {"api-version": "1"})
