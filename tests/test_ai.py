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
    assert "Right in 4 of 4 tries" in checks["picks the right question"].message
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
    assert c.ok is False and "Right in 3 of 4 tries" in c.message and "made a pick (wrong)" in c.message
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
    assert checks["explains a long page"].ok is False and "not right at the smallest" in checks["explains a long page"].message


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
