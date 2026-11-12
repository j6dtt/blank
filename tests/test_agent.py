"""Ask anything (agent.py): the AI looks things up in several steps and answers in its own words."""

import json
import time
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
             verify.AI_ASK[1][0]: ("activity", {"user": "sample02"}), verify.AI_ASK[2][0]: ("activity", {}),
             verify.AI_ASK[3][0]: ("logons", {"user": "sample05"}),
             verify.AI_ASK[4][0]: ("startups", {"user": "sample02"}),
             verify.AI_ASK[5][0]: ("programs_per_pc", {})}

    class Hyphens:  # answers with every hyphen a no-break one
        cfg = SimpleNamespace(name="nemotron")

        def chat(self, messages, tools=None):
            if messages[-1]["role"] == "tool":
                return {"content": "Found: " + messages[-1]["content"].replace("-", "\u2011")}
            return call(*asked[messages[-1]["content"]][:1], **asked[messages[-1]["content"]][1])
    checks = []
    verify._answers(Hyphens(), lambda item, ok, message, fix="", rows=(): checks.append((ok, message, list(rows))))
    ok, message, rows = checks[0]
    assert ok is True and "Right for 6 of 6" in message and "unusual characters: U+2011" in rows[1]


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
    # the look-ups go in the instructions: put after an answer, the AI wrote "(I looked at: …)" into its own (stage)
    assert sent[2]["content"] == "PC-K1 and PC-K2." and "(I looked at" not in json.dumps(sent)
    assert "\"What PCs had hard drive issues?\": events(kinds=['HD Failure'], days=7)" in sent[0]["content"]


def test_the_page_endpoint_needs_an_ai_service():
    r = TestClient(web.create_app()).post("/api/ask", json={"question": "hi"})
    assert r.status_code == 503 and "needs an AI service" in r.json()["detail"]


# -- step 2: look-ups that read Aternity now ---------------------------------------
from datetime import timedelta  # noqa: E402

from synthetic import demo_sources  # noqa: E402
from test_software import client as sw_client  # noqa: E402
from test_software import tables as sw_tables  # noqa: E402

from uxcorrelate import software as sw  # noqa: E402
from uxcorrelate.config import Settings  # noqa: E402
from uxcorrelate.correlate import InvestigationRequest  # noqa: E402
from uxcorrelate.correlate import investigate as run_investigation  # noqa: E402
from uxcorrelate.models import BootSample  # noqa: E402


class Boots:
    """A stand-in Aternity with sample02's logons on VD-77: today's took 95 s, the 3 before ~30 s."""
    def __init__(self, now):
        self.warnings = []
        self.rows = [BootSample(timestamp=now - timedelta(hours=h), device="VD-77", username="sample02", type="Logon",
                                duration=d) for h, d in ((2, 95.0), (26, 30.0), (50, 31.0), (74, 29.0))]

    def boots(self, start, end, username=None, device=None, location=None):
        return [r for r in self.rows if start <= r.timestamp < end and username in (None, r.username)
                and device in (None, r.device)]


def test_a_users_logons_and_start_ups_are_read_now():
    t, names, now = ask_records()
    reach = agent.Reach(aternity_of=lambda: Boots(now), boot_unit="s")
    lg = agent.run("logons", {"user": "sample02"}, t, names, now, reach)
    assert [r[2] for r in lg["rows"]] == ["VD-77"] * 4 and lg["summary"]["total"] == "4 logons"
    assert "sample02 last logged in on VD-77" in lg["summary"]["in_short"] and lg["runs"][0]["device"] == "VD-77"
    now_on = agent.run("logons", {"user": "sample02", "current": True}, t, names, now, reach)  # no read: today's actions
    assert now_on["rows"][0][1] == "VD-77"
    st = agent.run("startups", {"user": "sample02", "days": 7}, t, names, now, reach)
    assert st["summary"]["start_ups"] == 4 and st["rows"][0][3].startswith("Logon 95.0 s")  # slowest first
    assert st["open"] == {"tab": "log", "username": "sample02", "device": "", "start": st["open"]["start"],
                          "end": agent._iso(now)}
    assert "Unit: s." in st["notes"]
    assert agent.run("startups", {}, t, names, now, reach)["title"] == "Whose start-ups?"
    # several names have it: the names to choose from, nothing read
    assert agent.run("startups", {"user": "sample0"}, t, names, now, reach)["title"] == "Which user?"
    # without Aternity: said, not an error
    assert agent.NOT_SET in agent.run("startups", {"pc": "VD-77"}, t, names, now, agent.Reach())["notes"]


def test_a_pcs_software_and_what_changed_on_it_are_read_now():
    t, names, now = ask_records()
    data = sw_tables()
    at = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    log_row = lambda m, name, change, version: {  # noqa: E731
        "DEVICE_NAME": "PC-01", "INSTALLED_SW_CHANGE_TIMESTAMP": at(m), "INSTALLED_SW_NAME": name,
        "INSTALLED_SW_CHANGE_TYPE": change, "INSTALLED_SW_VERSION": version}
    data["INSTALLED_SOFTWARE_CHANGE_LOG"] = (
        [log_row(3 * 1440, "Zoom", "Uninstalled", "5.0"), log_row(3 * 1440 - 2, "Zoom", "Installed", "5.1"),
         log_row(1440, "Rare Tool", "Installed", "1.0")]
        + [log_row(2 * 1440, f"Old Tool {i}", "Uninstalled", "1.0") for i in range(5)])  # one minute: a batch
    c, s = sw_client(data=data)
    reach = agent.Reach(aternity_of=lambda: c, cfg=s.aternity)
    pc = agent.run("pc_software", {"pc": "PC-01"}, t, names, now, reach)
    assert [r[:2] for r in pc["rows"]] == [["Java", "8.1"], ["Java", "11.0"], ["Microsoft Edge", "121.0"],
                                           ["Zoom", "5.1"], ["KB1", None]]  # by name, oldest first; Windows updates last
    assert pc["summary"]["windows"] == "Microsoft Windows 11 Enterprise 23H2" and pc["summary"]["programs"] == 3 and pc["summary"]["rows"] == 4  # Java twice
    assert pc["summary"]["windows_updates"] == ["KB1"] and pc["rows"][3][4] == "Specific User"
    assert [r[0] for r in agent.run("pc_software", {"pc": "PC-01", "program": "java"}, t, names, now,
                                    reach)["rows"]] == ["Java", "Java"]
    ch = agent.run("pc_changes", {"pc": "PC-01"}, t, names, now, reach)
    assert [r[1:5] for r in ch["rows"]] == [["Rare Tool", "Installed", "1.0", None],
                                            ["5 programs: Old Tool 0, Old Tool 1, Old Tool 2, Old Tool 3 and 1 more",
                                             "Uninstalled", None, None], ["Zoom", "updated", "5.1", "5.0"]]
    assert ch["summary"]["changes"] == 2 and ch["summary"]["removed_in_a_batch"] == 5 and "one by one" in ch["rows"][1][5]


def test_the_software_tabs_look_ups_and_an_investigation(tmp_path):
    t, names, now = ask_records()
    c, s = sw_client()
    r = sw.Software("", tmp_path)
    r.save(sw.read_snapshot(c, s.aternity, now, r))
    reach = agent.Reach(aternity_of=lambda: c, cfg=s.aternity, software=r)
    v = agent.run("versions", {"program": "Edge"}, t, names, now, reach)  # as the Software chat's block
    assert v["title"].startswith("Microsoft Edge") and v["summary"]["in_short"]
    assert agent.run("most", {"problem": "crash"}, t, names, now, agent.Reach())["title"] == "Not read yet"
    settings = Settings()
    at, net, start, end = demo_sources(settings, now=now)
    asked = []

    def inv(u, d, a, t0, t1):
        asked.append((u, d, a, t0, t1))
        return run_investigation(InvestigationRequest(start=t0, end=t1, username=u, device=d, application=a),
                                 at, net, settings)
    reach = agent.Reach(investigate=inv)
    names.seen["username"]["jdoe"] = now
    step = agent.run("investigate", {"user": "jdoe", "hours": 3}, t, names, now, reach)
    assert asked == [("jdoe", None, None, now - timedelta(hours=3), now)]
    assert step["summary"]["headline"] == step["open"]["result"]["answer"]["title"] and step["text"].startswith(
        "User experience investigation: jdoe")
    assert "open" not in agent.for_ai(step) and "User experience investigation" in agent.for_ai(step)
    local = agent.run("investigate", {"user": "jdoe", "start": "2020-01-01 08:00", "end": "2020-01-03 08:00"}, t, names,
                      now, agent.Reach(investigate=lambda *a: asked.append(a) or step["open"]["result"]))
    assert asked[-1][4] - asked[-1][3] == timedelta(hours=agent.INVESTIGATE_MAX_H) and "Cut to the last" in \
        local["notes"][0]


def test_reads_of_aternity_are_limited_per_question():
    t, names, now = ask_records()
    reach = agent.Reach(aternity_of=lambda: Boots(now))
    ai = Script(*[call("logons", user="sample02")] * (agent.MAX_READS + 1),
                call("investigate", user="sample02"), call("investigate", user="sample02"),
                {"content": "Done."})
    ran = []
    reach.investigate = lambda *a: ran.append(a) or {"answer": {"title": "x"}, "request": {}}
    a = agent.ask("Tell me about sample02", [], ai, t, names, now, reach)
    assert len(a["steps"]) == agent.MAX_READS  # the 6th read and both investigations: told to answer
    told = [m["content"] for m in ai.seen[-1] if m["role"] == "tool"][agent.MAX_READS:]
    assert "No more reads of Aternity" in told[0] and ran == []


def test_the_page_runs_a_question_as_a_job(monkeypatch):
    app = web.create_app()
    t, names, now = ask_records()
    app.state.today.__dict__.update(t.__dict__)
    from uxcorrelate import ai as ai_mod
    monkeypatch.setattr(web, "chat_service", lambda settings: SimpleNamespace(name="gpt", key_env=""), raising=False)
    monkeypatch.setattr(ai_mod, "AiClient", lambda service, key: Script({"content": "Nothing to look up."}))
    tc = TestClient(app)
    r = tc.post("/api/ask", json={"question": "hi"})
    assert r.status_code == 200 and set(r.json()) == {"id"}
    for _ in range(100):
        job = tc.get(f"/api/jobs/{r.json()['id']}").json()
        if job["done"]:
            break
        time.sleep(0.05)
    assert job["result"]["answer"] == "Nothing to look up." and any("asked of gpt" in x for x in job["lines"])
    # the tab the page has open goes with the question (step 3: the one chat, on every tab)
    ai = Script(call("page"), {"content": "Mail crashed on VD-7."})
    monkeypatch.setattr(ai_mod, "AiClient", lambda service, key: ai)
    r = tc.post("/api/ask", json={"question": "What is this?", "page": {"tab": "care", "lines": ["Mail crashed on VD-7"]}})
    for _ in range(100):
        job = tc.get(f"/api/jobs/{r.json()['id']}").json()
        if job["done"]:
            break
        time.sleep(0.05)
    assert job["result"]["steps"][0]["title"] == "The Care list tab, as shown"
    assert any("asked of gpt on the Care list tab" in x for x in job["lines"])


def test_the_tab_open_is_given_as_shown_with_the_meaning_of_its_terms():
    """Step 3: one chat on every tab; "what does this mean?" is answered from what the user sees."""
    t, names, now = ask_records()
    shown = {"tab": "care", "lines": ["Care list · checked every 15 min",
                                      "Director · sample02 · VD-77 · 3 Major slow actions by 10:05 · Clears after 11:00"]}
    ai = Script(call("page"), {"content": "sample02 had 3 Major slow actions by 10:05; it clears after 11:00 "
                                          "if no new Major action comes."})
    a = agent.ask("Why is the director flagged?", [], ai, t, names, now, shown=shown)
    step = a["steps"][0]
    assert step["title"] == "The Care list tab, as shown" and step["summary"] == {"lines": 2}
    assert "The user has the Care list tab open now" in ai.seen[0][0]["content"]
    given = ai.seen[1][-1]["content"]  # the page's lines, then our meaning of each term on that tab
    assert "Director · sample02" in given and "Clears after" in given and "their usual by now" in given
    assert a["flagged"] == []  # its numbers are on the page
    assert "text" not in json.dumps(step["summary"]) and step["rows"] == []
    # nothing sent, or a tab with nothing on it yet: said, not made up
    none = agent.ask("What is this?", [], Script(call("page"), {"content": "I can't see a page."}), t, names, now)
    assert none["steps"][0]["notes"] == ["No page was sent with this question."]
    empty = agent.ask("Explain this", [], Script(call("page"), {"content": "No investigation yet."}), t, names, now,
                      shown={"tab": "investigate", "lines": []})
    assert empty["steps"][0]["notes"] == ["The Investigate tab shows nothing yet (no result on it)."]
    # a long tab is cut to the size both models read right (explain.LINES_MAX), and given whole
    big = {"tab": "software", "lines": [f"Program {i}: on {i} PCs, 2 versions" for i in range(3000)]}
    ai = Script(call("page"), {"content": "Program 1 is on 1 PC."})
    a = agent.ask("Which program is on the most PCs?", [], ai, t, names, now, shown=big)
    assert a["steps"][0]["notes"] == ["The tab is long: only its first part was given to the AI."]
    assert "Program 1000:" in ai.seen[1][-1]["content"] and len(ai.seen[1][-1]["content"]) > agent.RESULT_CHARS


def test_programs_per_pc_are_aternitys_count_only_when_it_is_the_pcs_own_lists(monkeypatch):
    """Stage, Nov 2026: "Which PC has the most software installed?" was "I can't tell"; then
    Aternity's count was 511 for a PC whose list has 619 rows, and every PC's list in one read
    was refused ("Returned data too long")."""
    import test_software
    t, names, now = ask_records()
    monkeypatch.setattr(test_software, "MEASURES", test_software.MEASURES | {"INSTALLED_SW_COUNT"})

    def counted(count):  # the installed list, with INSTALLED_SW_COUNT per row as count(row, seen before)
        data, seen = sw_tables(), set()
        for r in data["INSTALLED_SOFTWARE"]:
            key = (r["DEVICE_NAME"], r["INSTALLED_SW_NAME"])
            r["INSTALLED_SW_COUNT"] = count(key in seen)
            seen.add(key)
        reads = []
        c, s = sw_client(seen=reads, data=data)
        agent._per_pc.clear()
        return agent.run("programs_per_pc", {}, t, names, now, agent.Reach(aternity_of=lambda: c, cfg=s.aternity)), reads

    # Aternity counts each program name once: PC-01 has Java 8.1 and 11.0, Edge, Zoom = 3 programs in 4 rows
    by_name, reads = counted(lambda again: 0 if again else 1)
    assert by_name["rows"][0] == ["PC-01", 3, 1, "no action today"] and by_name["summary"]["counted"].startswith(
        "added up by Aternity (INSTALLED_SW_COUNT): the different program names, as in the own lists of")
    assert len(reads) == 3  # the count, then the lists of the PCs with the most and the fewest
    assert "different program names" in by_name["notes"][-1]
    by_row, _ = counted(lambda again: 1)  # one per row: the rows, said so
    assert by_row["rows"][0][:2] == ["PC-01", 4] and "the rows, as in" in by_row["summary"]["counted"]
    neither, _ = counted(lambda again: 2)  # neither: the numbers are said, no count is given
    assert neither["rows"] == [] and "no count is given: PC-01: Aternity 10, its list 4 names in 5 rows" in \
        neither["notes"][-1]
    # kept an hour: asked again, not read again
    c, s = sw_client(data=sw_tables())
    agent._per_pc["k"] = (time.monotonic(), {"pcs": {"PC-01": {"programs": 3, "windows_updates": 1}},
                                             "counts": "different program names", "how": "x"}, [], "16:00")
    c.cfg.base_url = "k"
    kept = agent.run("programs_per_pc", {"order": "fewest"}, t, names, now, agent.Reach(aternity_of=lambda: c))
    assert kept["rows"] == [["PC-01", 3, 1, "no action today"]] and "Counted at 16:00" in kept["notes"][0]
    # only virtual desktops: those Aternity marked so in today's actions
    vd = agent.run("programs_per_pc", {"kind": "virtual"}, t, names, now, agent.Reach(aternity_of=lambda: c))
    assert vd["rows"] == [] and "Only virtual ones" in vd["notes"][1]
