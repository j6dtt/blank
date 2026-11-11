"""`uxcorrelate verify --ai`: a self-hosted AI service checked against a stand-in."""

import json

import httpx

from uxcorrelate import explain
from uxcorrelate.config import AiService, Settings, load_settings
from uxcorrelate.verify import AI_OUTSIDE, AI_SW_TRIES, AI_TRIES, render_checks, verify_ai

FULL = {"outlook": "Microsoft Outlook", "mossley": "MOSSLEY", "edge": "Microsoft Edge", "kb5126652": "KB5126652",
        "teams": "Microsoft Teams Meeting Add-in for Microsoft Office"}  # what a model sends
PICKS = {q: {k: FULL.get(v, v) if isinstance(v, str) else v for k, v in want.items()}
         for q, want in AI_TRIES + AI_SW_TRIES}


def explained(content: str, how: str, reads: int) -> str:
    """The stand-in's answer to an "Explain" question: right, with a number of its own, or
    right only on pages up to `reads` characters."""
    if "A long made-up panel" in content:
        return f"The line gives {explain.LONG_ANSWER} PCs." if len(content) <= reads else "I see many programs."
    words = next(w for tab, panel, lines, q, w in explain.SAMPLES if f"Panel: {panel}\n" in content)
    return f"This panel shows {words[0]}." + (" That is 37.5% of them." if how == "invents" else "")


def service(seen, picks=PICKS, login="bearer", models=("model-a", "model-b"), tools=True, numbers="2,885 and 412",
            explains="right", reads=10**9):
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
        if body["messages"][0]["content"] == explain.SYSTEM:
            assert "tools" not in body
            return reply(explained(last["content"], explains, reads))
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
    assert [c.ok for c in checks.values()] == [None, True, True, True, True, True, True, True]
    assert "Right for 10 of 10 made-up panels" in checks["explains a panel"].message
    assert "right up to 40,000." in checks["explains a long page"].message
    assert len(checks["explains a long page"].rows) == 3
    assert "Right in 6 of 6 tries" in checks["picks the right question"].message
    assert "Right in 10 of 10 tries" in checks["picks the right question (Software tab)"].message
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
    assert c.ok is False and "Right in 5 of 6 tries" in c.message and "made a pick (wrong)" in c.message
    assert any(first in r and "picked events(group_by=day, days=7); a right pick is events(group_by=event, days=7)"
               in r for r in c.rows)


def test_the_software_chats_red_flags_need_all_of_them():
    """Stage, Nov 2026: GPT answered "What are the red flags?" with the crash flags alone."""
    q = "What are the red flags?"
    left_out = dict(PICKS)
    left_out[q] = {"tool": "flags"}  # no kind: the flags block reads it as all of them
    checks, _ = run(picks=left_out)
    assert "Right in 10 of 10 tries" in checks["picks the right question (Software tab)"].message
    one_kind = dict(PICKS)
    one_kind[q] = {"tool": "flags", "kind": "crash"}
    c = run(picks=one_kind)[0]["picks the right question (Software tab)"]
    assert c.ok is False and "Right in 9 of 10 tries" in c.message
    assert any("picked flags(kind=crash); a right pick is flags(kind=all)" in r for r in c.rows)


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


def test_explain_a_number_of_its_own_fails_and_a_long_page_is_measured():
    checks, text = run(explains="invents")
    c = checks["explains a panel"]
    assert c.ok is False and "Right for 0 of 10" in c.message and len(c.rows) == 10
    assert "a number that is not on the panel (37.5)" in c.rows[0] and c.rows[0].startswith("Today, \"Major slow actions\"")
    checks, _ = run(reads=20_000)  # reads a tile and a whole tab, not a whole investigation
    c = checks["explains a long page"]
    assert c.ok is None and "right up to 12,000." in c.message
    assert c.rows[-1].startswith("40,000 characters: it did not find the line asked about (1,713 PCs)")
    checks, _ = run(reads=100)
    assert checks["explains a long page"].ok is False and "right at none" in checks["explains a long page"].message


def test_explain_every_tab_has_meanings_and_two_made_up_panels():
    assert set(explain.GLOSSARY) == set(explain.TABS)
    for tab in explain.TABS:
        assert len([s for s in explain.SAMPLES if s[0] == tab]) == 2
        text = explain.given(tab, "P", ["a line"])
        assert "The panel's lines:\na line" in text and all(f"- {k}: " in text for k in explain.COMMON)
    for size in explain.SIZES:
        lines = explain.long_page(size)
        total = sum(len(x) + 1 for x in lines)
        assert size <= total < size + 200 and sum(explain.LONG_ANSWER in x for x in lines) == 1
        at = next(i for i, x in enumerate(lines) if explain.LONG_ANSWER in x)
        assert 0.7 < at / len(lines) < 0.8
    ok = ("today", "Major slow actions", ["318 on 141 PCs"], "Explain this panel.")
    assert explain.refused("There were 318 on 141 PCs today, which is 2 kinds.", *ok) is None  # 2: in the meanings
    assert "(459)" in explain.refused("That is 459 in all.", *ok)
    assert "twice" in explain.refused("That is twice as many as PCs.", *ok)
    assert explain.refused("  ", *ok) == "it gave no answer"


def test_explain_our_own_words_and_a_no_break_space_are_not_held_against_a_model():
    """Stage, Nov 2026: GPT's "half the start-ups took less" (our meaning of typical) was refused."""
    tab, panel, lines, q, words = next(x for x in explain.SAMPLES if x[1] == "Start-ups: each measurement")
    assert explain.refused("Typical is the middle value: half the start-ups took less.", tab, panel, lines, q) is None
    assert explain.refused("1 in 10 over: 90% of start-ups took less than that.", tab, panel, lines, q) is None
    assert "Half the" in explain.refused("Half the PCs are slow.", "today", "P", ["318 on 141 PCs"], "Explain.")
    flag = next(x for x in explain.SAMPLES if x[1] == "Red flag: Crashes more")
    assert explain.refused("A version is raised when it crashes at twice the share of the others.", *flag[:4]) is None
    assert "twice" in explain.refused("That is twice their usual.", "care", "P", ["14 by 10:15"], "Why?")
    assert explain.says("Look at Sample\u00a0ERP first.", ("sample erp",)) and explain.odd("Sample\u00a0ERP") == "U+00A0"
    assert not explain.says("Look at the mail first.", ("sample erp",)) and explain.odd("plain") == ""


def test_explain_the_pages_answer_is_the_ais_when_its_numbers_are_the_panels_else_our_meanings(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_chat import ai_service

    from uxcorrelate import ai, web
    lines = ["Major slow actions: 318 on 141 PCs", "As usual by 14:00; the 5 working days before had 120 to 160"]
    said = {"text": "The panel shows 318 Major slow actions on 141 PCs, which is as usual."}
    seen = []

    class Stand:
        def chat(self, messages, tools=None):
            seen.append(messages)
            if said["text"] is None:
                raise web.ApiError("no", status=503)
            return {"content": said["text"]}
    a = explain.explain(Stand(), "today", "Today's issues", lines, "", "gpt")
    assert a["answer"] == said["text"] and not a["ours"] and a["note"] is None and a["service"] == "gpt"
    assert a["question"] == explain.QUESTION and "Major slow actions: 318 on 141 PCs" in seen[0][1]["content"]
    said["text"] = "That is 459 in all."
    a = explain.explain(Stand(), "today", "Today's issues", lines, "What does usual mean?", "gpt")
    assert a["ours"] and "(459)" in a["note"] and a["answer"].startswith("usual: Today against the same kind of day before")
    assert "\nMajor slow action: One action of a user" in a["answer"]  # what was asked about comes first
    said["text"] = None
    a = explain.explain(Stand(), "today", "P", lines, "", "gpt")
    assert a["ours"] and "did not answer" in a["note"] and a["answer"]
    a = explain.explain(None, "care", "Care list", ["✓ OK"], "")
    assert a["ours"] and "No AI service is set" in a["note"] and "\n✓ OK: No open issue" in a["answer"]
    assert explain.our_words("care", "P", ["nothing known here"]) == explain.NO_ANSWER
    assert explain.our_words("care", "P", ["an overview to prevent it"]) == explain.NO_ANSWER  # whole words
    kept, cut = explain.trim(["a" * 30_000, "b" * 9_000, "c" * 2_000, " "])
    assert cut and len(kept) == 2 and explain.trim(["x", "", " y  z "]) == (["x", "y z"], False)

    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  services:\n    - name: gpt\n      base_url: https://ai.test/v1\n      model: m\n")
    said["text"] = "It shows 318 Major slow actions on 141 PCs."
    monkeypatch.setattr(ai, "AiClient", lambda *a, **k: ai_service({}, None, []) and Stand())
    http = TestClient(web.create_app(str(cfg)))
    got = http.post("/api/explain", json={"tab": "today", "panel": "Today's issues", "lines": lines}).json()
    assert got["answer"] == said["text"] and got["service"] == "gpt" and not got["cut"]
    assert http.post("/api/explain", json={"tab": "nothing", "lines": lines}).status_code == 400
    assert http.post("/api/explain", json={"tab": "today", "lines": [" "]}).status_code == 400
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    got = TestClient(web.create_app(str(empty))).post("/api/explain", json={"tab": "today", "lines": lines}).json()
    assert got["ours"] and "No AI service is set" in got["note"]
