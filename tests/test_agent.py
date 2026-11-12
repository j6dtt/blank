"""Ask anything (agent.py): the AI looks things up in several steps and answers in its own words."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from uxcorrelate import agent, web
from uxcorrelate.verify import ask_records


def call(name, **args):
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "type": "function",
                                           "function": {"name": name, "arguments": json.dumps(args)}}]}


class Script:
    """A stand-in AI: its replies in order; what it was sent is kept."""
    def __init__(self, *replies):
        self.replies, self.seen, self.cfg = list(replies), [], SimpleNamespace(name="gpt")

    def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def test_a_hard_drive_question_is_found_by_meaning_in_two_steps():
    """Stage, Nov 2026: "What PCs had hard drive issue this week?" was "no event named Hard drive"."""
    t, names, now = ask_records()
    ai = Script(call("find", text="hard drive"),
                call("events", kinds=["HD Failure", "Faulty HD S.M.A.R.T. Status"], days=7),
                {"content": "Two PCs had a hard drive fault this week: PC-K1 (HD Failure, today) and PC-K2 "
                            "(Faulty HD S.M.A.R.T. Status, earlier)."})
    a = agent.ask("What PCs had hard drive issues this week?", [], ai, t, names, now)
    found, hd = a["steps"]
    assert "HD Failure" in found["summary"]["kinds_of_event_there_are"]  # no name has the words: the kinds to choose
    assert [r[2] for r in hd["rows"]] == ["PC-K1", "PC-K2"] and hd["summary"]["pcs"] == 2
    assert hd["rows"][0][1] == "sample04" and hd["rows"][1][1] is None  # the user is known today only
    assert [r["device"] for r in hd["runs"]] == ["PC-K1", "PC-K2"]
    assert a["answer"].startswith("Two PCs") and a["flagged"] == [] and a["note"] is None
    sent = ai.seen[-1]
    assert sent[0]["content"].startswith(agent.SYSTEM) and "HD Failure" in sent[0]["content"]
    assert sent[-1]["role"] == "tool" and "PC-K2" in sent[-1]["content"] and "runs" not in sent[-1]["content"]
    assert a["looked"] == "find(text=hard drive); events(kinds=['HD Failure', 'Faulty HD S.M.A.R.T. Status'], days=7)"


def test_a_number_found_in_none_of_what_it_looked_at_is_marked_not_hidden():
    t, names, now = ask_records()
    a = agent.ask("How many hard drive faults?", [], Script(call("events", kinds=["HD Failure"], days=7),
                                                            {"content": "PC-K1 had 37 of them, twice as many."}),
                  t, names, now)
    assert a["answer"] == "PC-K1 had 37 of them, twice as many." and a["flagged"] == ["37", "twice as"]
    # "twice" alone is a count, not a comparison (Nemotron: "opened Sample Mail twice", 2 rows; stage, Nov 2026)
    two = agent.ask("And sample02?", [], Script(call("activity", user="sample02"),
                                                {"content": "sample02 opened Sample Mail twice on VD-77."}), t, names, now)
    assert two["flagged"] == []


def test_a_name_with_a_no_break_hyphen_is_the_same_name():
    """Stage, Nov 2026: Nemotron's answer about sample02 was judged without VD-77 in it."""
    from uxcorrelate import verify
    t, names, now = ask_records()
    nb = agent.activity(t, now, pc="VD-77")["rows"]
    assert agent.run("activity", {"pc": "VD\u201177"}, t, names, now)["rows"] == nb != []
    asked = {verify.AI_ASK[0][0]: ("events", {"kinds": ["HD Failure", "Faulty HD S.M.A.R.T. Status"], "days": 7}),
             verify.AI_ASK[1][0]: ("activity", {"user": "sample02"}), verify.AI_ASK[2][0]: ("activity", {})}

    class Hyphens:  # answers with every hyphen a no-break one
        cfg = SimpleNamespace(name="nemotron")

        def chat(self, messages, tools=None):
            if messages[-1]["role"] == "tool":
                return {"content": "Found: " + messages[-1]["content"].replace("-", "\u2011")}
            return call(*asked[messages[-1]["content"]][:1], **asked[messages[-1]["content"]][1])
    checks = []
    verify._answers(Hyphens(), lambda item, ok, message, fix="", rows=(): checks.append((ok, message, list(rows))))
    ok, message, rows = checks[0]
    assert ok is True and "Right for 3 of 3" in message and "unusual characters: U+2011" in rows[1]


def test_it_stops_after_its_steps_and_is_asked_to_answer():
    t, names, now = ask_records()
    ai = Script(call("overview"))  # never answers
    a = agent.ask("How is today?", [], ai, t, names, now)
    assert len(a["steps"]) == agent.MAX_STEPS and "still looking things up" in a["note"]
    assert any(m["role"] == "user" and m["content"].startswith("Answer now") for m in ai.seen[-1])
    assert a["answer"] == "" and len(ai.seen) == agent.MAX_STEPS + 1


def test_the_tools_read_what_the_app_holds():
    t, names, now = ask_records()
    me = agent.activity(t, now, user="sample02")
    assert me["summary"]["pcs_used"] == ["VD-77 (virtual)"] and me["summary"]["actions_measured"] == 3
    assert [r[3] for r in me["rows"]] == ["Sample Mail", "Sample Mail"] and me["runs"][0]["device"] == "VD-77"
    erp = agent.activity(t, now, app="erp")  # the one that has it
    assert (erp["summary"]["runs"], erp["summary"]["major"], erp["summary"]["pcs_with_major"]) == (6, 5, 1)
    every = agent.activity(t, now)
    assert every["summary"]["applications_most_major"] == {"Sample ERP": 5, "Sample Mail": 2}
    assert agent.activity(t, now, user="nobody")["notes"][-1] == 'No user "nobody" had a measured action today.'
    by_user = agent.events(t, now, user="sample04", days=7)
    assert [r[2] for r in by_user["rows"]] == ["PC-K1"] and any("keep the PC, not the user" in n for n in by_user["notes"])
    assert [r[1:4] for r in agent.events(t, now, pc="pc-k2", days=7)["rows"]] == [[None, "PC-K2",
                                                                                    "Faulty HD S.M.A.R.T. Status"]]
    mail = agent.events(t, now, app="Sample Mail")
    assert [r[2] for r in mail["rows"]] == ["PC-K3"] and mail["summary"]["pcs_by_application"] == {"Sample Mail": 1}
    f = agent.find(t, names, now, "sample0")
    assert [r for r in f["rows"] if r[0] == "user"] == [["user", "sample02"], ["user", "sample03"], ["user", "sample05"]]
    assert ["application", "Sample ERP"] in agent.find(t, names, now, "erp")["rows"]
    assert ["kind of event", "HD Failure"] in agent.find(t, names, now, "HD")["rows"]
    o = agent.overview(t, now)
    assert o["summary"]["critical_and_major_events"][0]["name"] == "HD Failure"
    m = agent.meanings("Major")
    assert m["rows"] and all("major" in r[0].lower() for r in m["rows"])
    assert "There is no tool named nope." in agent.run("nope", {}, t, names, now)["notes"]


def test_a_follow_up_is_sent_with_the_answers_and_look_ups_before():
    t, names, now = ask_records()
    ai = Script({"content": "PC-K1 and PC-K2."})
    agent.ask("And yesterday?", [{"q": "What PCs had hard drive issues?", "a": "PC-K1 and PC-K2.",
                                  "looked": "events(kinds=['HD Failure'], days=7)"}], ai, t, names, now)
    sent = ai.seen[0]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert "(I looked at: events(kinds=['HD Failure'], days=7))" in sent[2]["content"]


def test_the_page_endpoint_needs_an_ai_service():
    r = TestClient(web.create_app()).post("/api/ask", json={"question": "hi"})
    assert r.status_code == 503 and "needs an AI service" in r.json()["detail"]
