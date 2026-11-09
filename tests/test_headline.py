"""The headline rules (docs/headline-rules.md): what the user felt, then one verdict.

One test per class A-H, plus several apps, several PCs and a source not read.
Each pins the exact headline, so a change to any of them is a change you meant."""

from datetime import datetime, timedelta, timezone

from uxcorrelate.answer import build_answer
from uxcorrelate.models import UserSample

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


def act(minute, app="ERP", activity="Open", seconds=6.0, device="PC1", status="Minor", virtual=None):
    return UserSample(T0 + timedelta(minutes=minute), "amy", device, app, activity,
                      response_time=seconds, status=status, virtual=virtual)


def problem(issue, text, slow_idx, what="ERP crashed", source="Aternity", kind="pc", limit=None, obj="PC1"):
    return {"source": source, "kind": kind, "object": obj, "what": what, "app": "ERP" if issue in ("crash", "hang")
            else None, "limit": limit, "worst": None, "times": 1, "first": T0.isoformat(), "last": T0.isoformat(),
            "issue": issue, "slow_idx": slow_idx, "slow_actions": len(slow_idx), "of_slow": 0, "text": text,
            "why": {"text": []}}


def normal(app="ERP", activity="Open", device="PC1", took=6.0, usual=1.0, since=None, changes=(), enough=True,
           days_used=5, slow_days=0, n=1, virtual=False):
    return {"application": app, "activity": activity, "device": device, "slow_actions": n, "took": took,
            "worst": took, "usual": usual, "others_typical": None, "others": 0, "slow_days": slow_days,
            "days_used": days_used, "since": since, "changes": list(changes), "changed": len(changes),
            "enough": enough, "runs": 12, "back": "last 5 days", "virtual": virtual, "status": "Minor",
            "from_runs": 9 if since else 0, "from_slow": 7 if since else 0, "normal_day": None,
            "persistent": bool(since), "before_slow": 0, "before_runs": 12,
            "text": [f"{app} {activity} took {took} s."], "todo": []}


def change(name, day=5):
    return {"time": f"2026-11-0{day}T13:11:00Z", "name": name, "change": "updated", "version": "2",
            "version_before": "1"}


def headline(slow, problems=(), samples=None, **kw):
    a = build_answer(list(problems), samples or slow, slow, "amy", kw.pop("impact", None), True, [], None, None, 5,
                     None, **kw)
    return a["title"]


def test_a_a_crash_beats_a_cpu_reading_at_as_many_slow_actions():
    slow = [act(0, seconds=6.0), act(10, seconds=8.0)]
    cpu = problem("limit", "PC1: CPU reached 95% (limit 85.0)", [0], what="CPU %", limit=85.0)
    crash = problem("crash", "ERP crashed on PC1", [1])
    assert headline(slow, [cpu, crash]) == ("2 slow actions, slowest ERP Open 8.0 s on PC1 (Minor). "
                                            "Possibly: ERP crashed on PC1 at 1 of them; 1 more issue "
                                            "(see What went wrong)")


def test_b_many_users_slow_at_once():
    impact = {"applications": ["ERP"], "users": 40, "slow": 30, "unusual": True, "common_point": None,
              "rate": .75, "usual_rate": .1, "groups": [], "affected_users": [], "affected_total": 0}
    assert headline([act(0)], impact=impact) == ("ERP Open took 6.0 s on PC1 (Minor). 30 of the 40 other ERP "
                                                 "users were slow at the same time, more than on other days")


def test_c_one_change_is_named():
    c = normal(since="2026-11-06T09:00:00Z", changes=[change("Print Agent")])
    # what was counted from the first slow run, never "slow since" (runs in between may be fast)
    assert headline([act(0)], normal=[c]) == ("ERP Open on PC1: slow 7 of 9 times from Nov 6 (6.0 s, usually 1.0 s); "
                                              "normal on each of the 5 days before. "
                                              "Possibly: Print Agent updated on Nov 5")


def test_c_changes_on_several_days_are_counted():
    c = normal(since="2026-11-06T09:00:00Z", changes=[change("A", 3), change("B", 5)])
    assert headline([act(0)], normal=[c]).endswith("2 changes logged there between Nov 3 and Nov 5: "
                                                   "possibly one of them")


def test_d_new_with_no_change_logged_says_the_log_can_run_late_on_a_virtual_desktop():
    c = normal(device="VD-1", since="2026-11-06T09:00:00Z", virtual=True)
    assert headline([act(0, device="VD-1", virtual=True)], normal=[c]) == (
        "ERP Open on VD-1: slow 7 of 9 times from Nov 6 (6.0 s, usually 1.0 s); normal on each of the 5 days "
        "before. No change to check was logged there before it (on a virtual desktop the log can run late)")


def test_e_right_after_a_start_up_on_every_pc_of_its_kind():
    c = {"application": "ERP", "activity": "Open", "kind": "virtual desktops", "count": 1, "device": "VD-1",
         "seconds": 6.0, "minutes": 3, "slower": True, "enough": True, "hours": 24,
         "after": {"runs": 50, "slow": 45, "share": .9, "median": 9.0},
         "later": {"runs": 50, "slow": 10, "share": .2, "median": 2.0}}
    assert headline([act(0, device="VD-1", virtual=True)], after_start=[c]) == (
        "ERP Open took 6.0 s on VD-1 (Minor), 3 min after a start-up. On virtual desktops 90% are slow then, "
        "20% later")


def test_f_slow_every_day_is_not_new():
    c = normal(slow_days=5, days_used=5)
    assert headline([act(0)], normal=[c]) == ("ERP Open took 6.0 s on PC1 (Minor), usually 1.0 s. Aternity "
                                              "marked most runs slow on each of the 5 days amy used it: not new")


def test_g_others_as_slow_as_usual():
    impact = {"applications": ["ERP"], "users": 40, "slow": 4, "unusual": False, "common_point": None,
              "rate": .1, "usual_rate": .1, "groups": [], "affected_users": [], "affected_total": 0}
    assert headline([act(0)], impact=impact) == ("ERP Open took 6.0 s on PC1 (Minor). Other ERP users were as "
                                                 "slow as usual at this time")


def test_h_nothing_found_and_a_source_not_read():
    assert headline([act(0)]) == "ERP Open took 6.0 s on PC1 (Minor). No cause found"
    assert headline([act(0)], unread=["Health events not checked: Aternity gave an error."]) == (
        "ERP Open took 6.0 s on PC1 (Minor). No cause found in what could be read (1 source not read: "
        "see Warnings)")


def test_several_apps_each_get_their_own_line():
    slow = [act(0, seconds=4.0), act(5, app="Mail", activity="Send", seconds=9.0)]
    c = normal(since="2026-11-06T09:00:00Z", changes=[change("Print Agent")], took=4.0)
    mail = normal(app="Mail", activity="Send", took=9.0, usual=None, enough=False)
    mail["others_typical"] = 2.0
    a = build_answer([], slow, slow, "amy", None, True, [], None, None, 5, None, normal=[c, mail])
    assert a["title"].startswith("ERP Open on PC1: slow 7 of 9 times from Nov 6")  # the class beats the slowest
    assert a["reason"].endswith("Also slow: Mail Send 9.0 s on PC1, others typical 2.0 s.")


def test_the_pcs_are_named_never_these_pcs():
    one = [act(0)]
    two = [act(0), act(5, device="PC2")]
    pick = lambda slow: build_answer([], slow, slow, "amy", None, True, [], None, None, 5, None)["pcs_label"]  # noqa: E731
    assert (pick(one), pick(two)) == ("PC1", "PC1 and PC2")
    three = two + [act(9, device="PC3")]
    assert pick(three) == "the 3 PCs"


def _two(changes_b):
    c = normal(device="VD-1", since="2026-11-06T09:00:00Z", changes=[change("Print Agent")], virtual=True, n=2)
    c["pcs"] = [{"device": "VD-1", "runs": 9, "slow": 7, "took": 6.0, "changes": c["changes"], "changed": 1},
                {"device": "VD-2", "runs": 4, "slow": 1, "took": 5.0, "changes": changes_b,
                 "changed": len(changes_b)}]
    slow = [act(0, device="VD-1", virtual=True), act(9, device="VD-2", virtual=True, seconds=5.0)]
    return build_answer([], slow, slow, "amy", None, True, [], None, None, 5, None, normal=[c])


def test_c_slow_on_two_pcs_one_unchanged_does_not_blame_the_changes():
    a = _two([])
    assert a["title"] == ("ERP Open slow on 2 desktops from Nov 6: VD-1 7 of 9 times (6.0 s), VD-2 1 of 4 times "
                          "(5.0 s); usually 1.0 s, normal on each of the 5 days before. 1 change on VD-1 on Nov 5, "
                          "none on VD-2: they do not explain it alone")
    assert a["level"] == "unsure"


def test_c_slow_on_two_pcs_both_changed():
    a = _two([change("Print Agent", 4)])
    assert a["title"].endswith("1 change on VD-1 on Nov 5 and 1 change on VD-2 on Nov 4: possibly one of them")
    assert a["level"] == "possible"


def test_occasional_slowness_is_never_the_headline():
    """Slow 2 of 22 times since the first slow run: not a slowdown, an "Also slow" line."""
    c = normal(since="2026-11-06T09:00:00Z", changes=[change("Print Agent")], took=4.0, usual=0.2)
    c.update(persistent=False, before_slow=0, before_runs=32, pcs=[
        {"device": "PC1", "runs": 11, "slow": 1, "took": 3.6, "changes": c["changes"], "changed": 1},
        {"device": "PC2", "runs": 11, "slow": 1, "took": 5.2, "changes": [], "changed": 0}])
    slow = [act(0, seconds=3.6), act(9, device="PC2", seconds=5.2)]
    a = build_answer([], slow, slow, "amy", None, True, [], None, None, 5, None, normal=[c])
    assert a["title"] == ("2 slow actions, slowest ERP Open 5.2 s on PC2 (Minor), slow 2 of 22 times from Nov 6, "
                          "usually 0.2 s: occasional. No cause found")
    assert not any("check what changed" in t for t in a["todo"])


def test_occasional_beside_another_verdict_is_an_also_line():
    c = normal(since="2026-11-06T09:00:00Z", changes=[change("Print Agent")], took=4.0, usual=0.2)
    c.update(persistent=False, before_slow=1, before_runs=32, pcs=[
        {"device": "PC1", "runs": 11, "slow": 1, "took": 3.6, "changes": c["changes"], "changed": 1}])
    e = {"application": "Mail", "activity": "Send", "kind": "physical PCs", "count": 1, "device": "PC1",
         "seconds": 9.0, "minutes": 3, "slower": True, "enough": True, "hours": 24,
         "after": {"runs": 50, "slow": 45, "share": .9, "median": 9.0},
         "later": {"runs": 50, "slow": 10, "share": .2, "median": 2.0}}
    slow = [act(0, seconds=3.6), act(5, app="Mail", activity="Send", seconds=9.0)]
    a = build_answer([], slow, slow, "amy", None, True, [], None, None, 5, None, normal=[c], after_start=[e])
    assert a["title"].startswith("Mail Send took 9.0 s on PC1 (Minor), 3 min after a start-up")
    assert a["reason"].endswith("Also slow: ERP Open 3.6 s on PC1: slow 1 of 11 times from Nov 6, usually 0.2 s "
                                "(before: 1 of 32 slow).")
