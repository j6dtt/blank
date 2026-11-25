"""The admin page's CODE tab (code_chat.py): the source listed, shown and asked about."""

import json
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from uxcorrelate import code_chat as cc
from uxcorrelate import web
from uxcorrelate.verify import AI_CODE

PY = '''"""A small file. It is here for the tests: its parts are read by Python's own reading of it."""

import os

EVERY_MIN = 15  # how often it reads
# the most rows of one read,
# whatever is asked
ROWS_MAX = 500
log = None


class Limits:
    """The limits of one read."""
    rows: int = 500  # at most
    wait: float = 2.5

    def held(self, n):
        """Is n within the limits?"""
        return n <= self.rows


def make(app):
    """Build the app's addresses."""
    @app.get("/api/thing")
    def thing(name: str = ""):
        """One thing by its name."""
        return os.environ.get("THING_KEY", name)

    return thing
'''
PAGE = '''<!doctype html>
<html><head><script>(() => { const t = 1; })();</script></head>
<body>
<script>
"use strict";
const $ = id => document.getElementById(id);
// how many rows are drawn before "Show all"
const ROWS = 200;  // rows drawn
const NAMES = { a: "one",
  b: "two" };
function draw(j) {
  const s = `{${j}`;
  return s;
}
// the latest read wins
async function load(day = "") {
  await fetch("x?" + day);
}

addEventListener("load", () => load());
</script>
</body></html>
'''


def tree(tmp_path, **more):
    root = tmp_path / "pkg"
    (root / "static").mkdir(parents=True)
    (root / "small.py").write_text(PY)
    (root / "__init__.py").write_text("")
    (root / "static" / "page.html").write_text(PAGE)
    for name, text in more.items():
        (root / name).write_text(text)
    return cc.Source(root)


def call(tool, **args):
    return {"content": "", "tool_calls": [{"id": f"c-{tool}", "type": "function",
                                           "function": {"name": tool, "arguments": json.dumps(args)}}]}


class Script:
    """A stand-in AI: its replies in order; what it was sent is kept."""
    def __init__(self, *replies):
        self.replies, self.seen, self.cfg = list(replies), [], SimpleNamespace(name="gpt")

    def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


# -- the files and their parts ---------------------------------------------------------------------
def test_every_source_file_of_the_app_is_listed_with_what_it_does():
    src = cc.Source()
    got = cc.listing(src)
    names = [f["file"] for f in got["files"]]
    assert {"web.py", "software.py", "code_chat.py", "static/index.html", "static/admin.html"} <= set(names)
    assert names == sorted(n for n in names if "/" not in n) + sorted(n for n in names if "/" in n)
    assert got["total_lines"] == sum(f["lines"] for f in got["files"])
    assert all(f["does"] for f in got["files"])  # every .py file has its own first sentence; the pages ours
    does = {f["file"]: f["does"] for f in got["files"]}
    assert does["__init__.py"] == cc.EMPTY_INIT and does["http.py"] == "Thin httpx wrapper with retries and readable errors."
    assert does["static/admin.html"] == cc.PAGES["static/admin.html"]
    assert all(len(d) <= cc.DOES_MAX + 2 for d in does.values())
    assert got["limits"] == {"steps": cc.MAX_STEPS, "history": cc.HISTORY, "question": cc.QUESTION_MAX,
                             "about": cc.ABOUT_LINES}


def test_a_python_files_parts_are_read_with_their_lines_values_and_comments(tmp_path):
    f = tree(tmp_path).file("small.py")
    assert f.does == "A small file. It is here for the tests: its parts are read by Python's own reading of it."
    parts = {i.full: i for i in f.items}
    assert list(parts) == ["EVERY_MIN", "ROWS_MAX", "Limits", "Limits.rows", "Limits.wait", "Limits.held", "make",
                           "make.thing"]  # `log` is no setting: not in capitals
    assert (parts["EVERY_MIN"].kind, parts["EVERY_MIN"].line, parts["EVERY_MIN"].says) == ("setting", 5, "= 15")
    assert parts["EVERY_MIN"].doc == "how often it reads"  # the comment after it
    assert parts["ROWS_MAX"].doc == "the most rows of one read, whatever is asked"  # the comment lines over it
    assert (parts["Limits.rows"].kind, parts["Limits.rows"].says, parts["Limits.rows"].doc) == ("field", ": int = 500", "at most")
    assert parts["Limits"].kind == "class" and parts["Limits"].doc == "The limits of one read."
    assert (parts["Limits.held"].kind, parts["Limits.held"].says) == ("method", "(self, n)")
    thing = parts["make.thing"]  # a function inside a function, with the address it serves
    assert (thing.kind, thing.route, thing.inside, thing.doc) == ("function", "GET /api/thing", "make", "One thing by its name.")
    assert thing.top == thing.line - 1 and f.lines[thing.top - 1].strip() == '@app.get("/api/thing")'
    assert f.inside(thing.line + 1) == "make.thing" and f.inside(parts["make"].line + 1) == "make" and f.inside(5) == ""
    assert [i.full for i in f.named("thing")] == ["make.thing"] and [i.full for i in f.named("LIMITS.HELD")] == ["Limits.held"]


def test_a_pages_script_gives_its_functions_and_constants(tmp_path):
    f = tree(tmp_path).file("static/page.html")
    parts = {i.name: i for i in f.items}
    assert list(parts) == ["$", "ROWS", "NAMES", "draw", "load"]
    assert (parts["ROWS"].kind, parts["ROWS"].says, parts["ROWS"].doc) == ("setting", "= 200", "rows drawn")
    assert (parts["NAMES"].line, parts["NAMES"].end) == (9, 10)  # to the line before the next part
    assert (parts["draw"].line, parts["draw"].end, parts["draw"].says) == (11, 14, "(j)")  # a "{" in a string is no bracket
    assert (parts["load"].line, parts["load"].end, parts["load"].doc) == (16, 18, "the latest read wins")
    assert f.inside(12) == "draw" and f.inside(3) == ""  # the one-line script in the head is not read as parts
    assert f.does == "A page of the app."


def test_a_file_python_cannot_read_is_still_shown_and_said(tmp_path):
    src = tree(tmp_path, **{"broken.py": "def f(:\n    pass\n"})
    f = src.file("broken.py")
    assert f.items == [] and len(f.lines) == 2 and "could not read this file as code" in f.error
    assert f.error in cc.outline(src, "broken.py")["notes"] and cc.read(src, "broken.py")["code"]["lines"][0] == "def f(:"


def test_the_first_sentence_of_a_docstring():
    assert cc.first_sentences("Thin wrapper with retries and readable errors.\n\nMore.") == "Thin wrapper with retries and readable errors."
    assert cc.first_sentences("Loading.\n\nSettings come from a YAML file on the disk. And more.") \
        == "Loading. Settings come from a YAML file on the disk."
    assert cc.first_sentences("Engine.\n\n1. Pull the rows. Then count.\n2. Next.") == "Engine. Pull the rows. …"
    assert cc.first_sentences("For the slow ones (e.g. \"Teams Launch\") that were slow last week. Next.") \
        == "For the slow ones (e.g. \"Teams Launch\") that were slow last week."
    assert cc.first_sentences("word " * 80).endswith(" …") and len(cc.first_sentences("word " * 80)) <= cc.DOES_MAX + 2
    assert cc.first_sentences("") == ""


# -- what can be read --------------------------------------------------------------------------------
def test_what_a_file_does_is_a_sentence_that_is_true_by_itself():
    """The AI is told each file's first sentence and repeats it. On the live host (Nov 23 2026) both
    models were told config.py's sentence cut at its colon, "... can be overridden with environment
    variables for secrets:", and one answered that refresh_minutes "can be overridden with an
    environment variable"; "tool calls per question" on MAX_STEPS sent it to MAX_READS for "how many
    look-ups"; the chats' files still said "on the Today tab"."""
    src = cc.Source()
    does = {f["file"]: f["does"] for f in cc.listing(src)["files"]}
    assert not [name for name, text in does.items() if text.rstrip(" …").endswith(":")]  # cut before what it introduces
    assert "set only" in does["config.py"] and "no other setting" in does["config.py"]
    assert "every tab" in does["agent.py"] and "MAX_STEPS" in does["agent.py"] and "Today tab" not in does["agent.py"]
    assert "off the page" in does["chat.py"] and "chat_service" in does["chat.py"]
    assert "off the page" in does["software_chat.py"] and "Ask anything" in does["software_chat.py"]
    assert "drawn by the page (static/index.html)" in does["status.py"]
    steps = next(r for r in cc.outline(src, "agent.py")["rows"] if r[1] == "MAX_STEPS")
    assert steps[3].startswith("= 8 · look-ups per question")  # the page's word, as the question asks it


def test_only_the_listed_source_files_can_be_read(tmp_path):
    """config.yaml, .env, a data folder, any other kind of file and a link that leads out of the
    folder are not in the list, and nothing is opened that is not in it."""
    src = tree(tmp_path, **{".env": "ADMIN_PASSWORD=hunter2-secret\n", "config.yaml": "title: hunter2-secret\n",
                            "notes.txt": "hunter2-secret\n"})
    root = src.root
    (root / "data").mkdir()
    (root / "data" / "kept.py").write_text("X = 'hunter2-secret'\n")
    (tmp_path / "outside.py").write_text("Y = 'hunter2-secret'\n")
    (root / "leak.py").symlink_to(tmp_path / "outside.py")
    (root / "static" / "leak.html").symlink_to(root / ".env")
    assert src.names() == ["__init__.py", "small.py", "static/page.html"]
    for typed in (".env", "config.yaml", "../config.yaml", "/config/config.yaml", "notes.txt", "data/kept.py", "kept.py",
                  "leak.py", "static/leak.html", "../outside.py", "/etc/passwd", "small.py/../.env", ""):
        assert src.pick(typed) is None and src.file(typed) is None and cc.file_view(src, typed) is None
        for step in (cc.read(src, typed, 1, 50), cc.outline(src, typed), cc.search(src, "ADMIN", typed or "x.y"),
                     cc.run(src, "read", {"file": typed, "name": "X"})):
            assert step["title"] in ("Not found", "Not run") and "hunter2" not in json.dumps(step)
        assert cc.about_step(src, {"file": typed, "start": 1, "end": 5}) == (None, "")
    assert cc.search(src, "hunter2")["rows"] == [] and cc.search(src, "hunter2")["summary"] == {"lines_with_it": 0}
    everything = [cc.search(src, "secret"), cc.search(src, "ADMIN"), cc.setting(src, "ADMIN_PASSWORD"),
                  cc.setting(src, "title"), cc.read(src, name="X"), cc.read(src, name="Y"), cc.outline(src, "small.py")]
    assert "hunter2" not in json.dumps(everything) and "hunter2" not in " ".join(src.words())
    assert "hunter2" not in cc.system(src)
    # a name is how the files call it, however it is typed
    assert src.pick("small") == src.pick("SMALL.PY") == src.pick("src/uxcorrelate/small.py") == src.pick("uxcorrelate.small") == "small.py"
    assert src.pick("page.html") == src.pick("`static/page.html`") == "static/page.html"


def test_a_look_up_with_settings_that_make_no_sense_is_a_step_that_says_so(tmp_path):
    src = tree(tmp_path)
    assert cc.run(src, "read", {"file": "small.py", "start": "the top"})["notes"] == ["start and end are line numbers."]
    assert cc.run(src, "delete", {"file": "small.py"})["notes"] == ["There is no tool named delete: search, outline, read or setting."]
    assert cc.run(src, "read", {})["notes"] == ["Give a file (with start and end), or a name."]
    assert cc.run(src, "search", {"text": "x"})["notes"] == ["Give a word or a name of 2 or more characters."]
    assert cc.run(src, "setting", {})["notes"] == ["Give the setting's name."]
    assert cc.run(src, "read", {"file": "small.py", "start": 99})["notes"] == ["small.py has 29 lines: there is no line 99."]
    assert cc.TOOLS and [t["function"]["name"] for t in cc.TOOLS] == ["search", "outline", "read", "setting"]


# -- the look-ups, on the app's own source -----------------------------------------------------------
def test_search_gives_the_lines_that_define_a_name_first_and_counts_per_file():
    src = cc.Source()
    got = cc.search(src, "judge")
    assert got["columns"] == ["File", "Line", "In", "Code"] and got["rows"][0][0] == "software.py"
    assert got["rows"][0][2:] == ["judge", "def judge(app: dict) -> None:"]
    assert got["summary"]["lines_with_it"] == sum(got["summary"]["per_file"].values()) >= len(got["rows"])
    assert got["places"][0] == ["software.py", got["rows"][0][1], got["rows"][0][1]]
    one = cc.search(src, "refresh_minutes", "config")
    assert {r[0] for r in one["rows"]} == {"config.py"} and one["title"].startswith('Lines with "refresh_minutes" in config.py: ')
    words = cc.search(src, "def care_status request")  # no line has them together: the lines with all of them
    assert words["notes"][0] == "No line has these words together: these have all of them."
    assert words["rows"][0][0] == "web.py" and "def care_status(request: Request):" in words["rows"][0][3]
    none = cc.search(src, "zzqqxx")
    assert none["rows"] == [] and none["summary"] == {"lines_with_it": 0} and none["notes"] == ['No line has "zzqqxx".']
    assert cc.search(src, "judge", "nowhere.py")["title"] == "Not found"
    several = cc.search(src, "CHECK_EVERY_S|CHECK_TIMEOUT_S", "status.py")  # several names at once: any of them
    assert several["notes"][0] == 'Read as several names: the lines with any of "check_every_s", "check_timeout_s".'
    assert [r[3].split(" = ")[0] for r in several["rows"][:2]] == ["CHECK_EVERY_S", "CHECK_TIMEOUT_S"]
    many = cc.search(src, "self")
    assert len(many["rows"]) == cc.SEARCH_ROWS and many["summary"]["lines_with_it"] > cc.SEARCH_ROWS
    assert f"The first {cc.SEARCH_ROWS} of" in many["notes"][-1] and len(cc.for_ai(many)) <= cc.RESULT_CHARS


def test_outline_gives_a_files_parts_and_the_address_each_web_function_serves():
    src = cc.Source()
    got = cc.outline(src, "web", "admin_login")
    assert got["rows"] == [["function", "create_app.admin_login", got["rows"][0][2], got["rows"][0][3]]]
    assert got["rows"][0][3].startswith("POST /admin/api/login · (body: LoginBody, request: Request)")
    first, last = (int(x) for x in got["rows"][0][2].split("-"))
    assert src.file("web.py").lines[first - 1].strip() == '@app.post("/admin/api/login")' and last > first
    whole = cc.outline(src, "status.py")
    s = whole["summary"]
    assert s["file"] == "status.py" and s["lines"] == len(src.file("status.py").lines)
    assert s["settings"] + s["functions"] + s["classes"] + s["methods"] + s["fields"] == len(whole["rows"])
    assert ["setting", "CHECK_EVERY_S", "25", "= 60"] in whole["rows"] or any(r[1] == "CHECK_EVERY_S" for r in whole["rows"])
    assert "A page: its script's top-level functions" in cc.outline(src, "index.html")["notes"][-1]


def test_read_gives_numbered_lines_a_part_by_its_name_and_at_most_so_many_at_once():
    src = cc.Source()
    got = cc.read(src, name="chat_service")
    assert got["summary"]["file"] == "chat.py" and got["summary"]["part"] == "chat_service"
    first, last = got["summary"]["first"], got["summary"]["last"]
    assert got["text"].splitlines()[0] == f"{first}: def chat_service(settings: Settings) -> AiService | None:"
    assert got["code"] == {"file": "chat.py", "start": first, "end": last, "lines": src.file("chat.py").lines[first - 1: last]}
    assert got["places"] == [["chat.py", first, last]] and got["title"] == f"chat.py, lines {first}-{last} (chat_service)"
    long = cc.read(src, "software.py", 100, 5000)
    assert long["summary"]["last"] == 100 + cc.READ_LINES - 1 and len(long["text"].splitlines()) == cc.READ_LINES
    assert long["notes"][0] == (f"Lines 100-{99 + cc.READ_LINES} of 100-{len(src.file('software.py').lines)} (at most "
                                f"{cc.READ_LINES} lines at once): read on with start={100 + cc.READ_LINES}.")
    back = cc.read(src, "web.py", 318, 40)  # an end before the start: so many lines from the start
    assert (back["summary"]["first"], back["summary"]["last"]) == (318, 357)
    assert back["notes"] == ["end was before start: read as 40 lines from line 318."]
    elsewhere = cc.read(src, "web.py", name="judge")  # not in that file: where it is
    assert elsewhere["title"] == "Not found" and "It is in: software.py (judge, lines " in elsewhere["notes"][0]
    assert "No function, class or setting is named" in cc.read(src, name="no_such_part_here")["notes"][0]
    page = cc.read(src, name="answerHtml")
    assert page["summary"]["file"] == "static/index.html" and "function answerHtml(text, tags) {" in page["text"]
    assert all(len(line) <= cc.LINE_CHARS + 30 for line in cc.read(src, "static/index.html", 1, 160)["text"].splitlines())


def test_setting_says_where_a_key_is_defined_its_default_and_where_it_is_read_never_its_value():
    src = cc.Source()
    got = cc.setting(src, "refresh_minutes")
    assert got["rows"][0][:4] == ["defined in Settings", "config.py", got["rows"][0][2], "Settings"]
    assert got["rows"][0][4] == "refresh_minutes: int = 15" and got["summary"]["default"] == ": int = 15"
    assert got["summary"]["defined"] == [f"config.py:{got['rows'][0][2]} (Settings.refresh_minutes)"]
    assert any(r[0] == "named in quotes" and r[3] == "load_settings" for r in got["rows"])
    assert got["notes"][0].startswith("Its value on this host is not read here")
    chat = cc.setting(src, "ai.chat")  # the field of the class the section is of, and ".chat" only after "ai"
    assert chat["rows"][0][0] == "defined in AiConfig" and chat["summary"]["setting"] == "chat"
    assert all("ai.chat" in r[4] or '"chat"' in r[4] or r[0].startswith("defined") for r in chat["rows"])
    env = cc.setting(src, "ADMIN_PASSWORD")  # a line of .env: the constant that holds its name, and where that is used
    assert ["named in quotes", "web.py"] in [r[:2] for r in env["rows"]]
    assert any(r[0] == "used as ADMIN_ENV" and r[3] == "create_app.admin_login" for r in env["rows"])
    const = cc.setting(src, "CHECK_EVERY_S")
    assert const["rows"][0][:2] == ["defined", "status.py"] and const["summary"]["default"] == "= 60"
    none = cc.setting(src, "no_such_setting_xyz")
    assert none["summary"] == {"places": 0} and "has no setting, key or constant named" in none["notes"][0]


# -- what an answer names, against what was looked at ---------------------------------------------------
def test_the_places_an_answer_names_are_read_however_it_writes_them(tmp_path):
    src = tree(tmp_path)
    said = ("See small.py:5 and `small.py:13-19`, then small.py, lines 22 to 28, also lines 6–8 of page.html, "
            "and nowhere.py:3. Again small.py:5.")
    assert cc.cites(src, said) == [
        {"file": "small.py", "name": "small.py", "start": 5, "end": 5},
        {"file": "small.py", "name": "small.py", "start": 13, "end": 19},
        {"file": "small.py", "name": "small.py", "start": 22, "end": 28},
        {"file": "page.html", "name": "static/page.html", "start": 6, "end": 8},
        {"file": "nowhere.py", "name": None, "start": 3, "end": 3}]


def test_what_an_answer_names_that_is_in_none_of_the_look_ups_is_marked(tmp_path):
    src = tree(tmp_path)
    steps = [cc.read(src, "small.py", 1, 10), cc.search(src, "thing")]
    given = ["the question", cc.system(src)] + [cc.for_ai(s) for s in steps]

    def marks(answer):
        return [(x["text"], x["kind"]) for x in cc.check(src, answer, steps, given)]

    # all of it from what was looked at: nothing marked
    assert marks("`EVERY_MIN` is 15 in small.py:5; `thing` serves `/api/thing` (small.py:27). `os.environ` holds "
                 "`THING_KEY`. The file has 29 lines; `ROWS_MAX` is 500. See static/page.html.") == []
    assert marks("`EVERY_MIN` in small.py: 15 minutes.") == []  # a sentence, not line 15 of the file
    assert marks("It is in small.py:20.") == [("small.py:20", "unseen")]  # a line it did not look at
    assert marks("It is in small.py:5-20.") == [("small.py:5-20", "unseen")]
    assert marks("It is in small.py:900.") == [("small.py:900", "line")]  # the file is shorter
    assert marks("It is in helper.py, see helper.py:3.") == [("helper.py", "file")]  # no such source file
    assert marks("`Limits` and `held` limit it.") == [("Limits", "unseen"), ("held", "unseen")]  # in the code, not looked up
    assert marks("`fetch_rows_fast` does it, and read_all_rows() too.") == [("fetch_rows_fast", "name"), ("read_all_rows", "name")]
    assert marks("It waits 42 seconds.") == [("42", "number")]
    # a number after a place is a number, not the end of a range of lines
    assert marks("See small.py:5 and 4242 tries, or small.py:5 to 77 of them.") == [("77", "number"), ("4242", "number")]
    assert marks("It reads `THING_KEY` as `Thing_Key`, with `os.environ.get`.") == []  # capitals apart, the same name
    assert marks("Run `docker compose restart`, then set `a: b` and open `/api/thing`.") == []  # not one name: not judged
    why = {x["kind"]: x["why"] for x in cc.check(src, "small.py:900, helper.py, `held`, `nope_x`, 42, small.py:20",
                                                 steps, given)}
    assert why == {"line": "small.py has 29 lines", "file": "not one of this app's source files here",
                   "unseen": "in the code, but in none of its look-ups", "name": "not a name in the code",
                   "number": "a number in none of its look-ups"}
    assert [x["why"] for x in cc.check(src, "small.py:20", steps, given)] == ["not among the lines it looked at"]
    # a file named in what it read (a comment that names a file that is not here) is not its invention
    assert [x["kind"] for x in cc.check(src, "See other.py.", steps, given + ["# as other.py does"])] == []


def test_the_lines_an_answer_names_are_shown_from_the_disk(tmp_path):
    src = tree(tmp_path)
    f = src.file("small.py")
    got = cc.shown(src, "`thing` in small.py:26-29 reads the key at small.py:5; `Limits` holds the limits. Again small.py:27.")
    assert [(s["file"], s["start"], s["end"], s["mark"], s["part"]) for s in got] == [
        ("small.py", 26, 29, None, "make.thing"), ("small.py", 3, 7, 5, ""), ("small.py", 12, 19, None, "Limits")]
    assert got[0]["lines"] == f.lines[25:29] and got[1]["lines"][2] == "EVERY_MIN = 15  # how often it reads"
    assert got[0]["more"] == 0 and got[0]["to"] == 29
    long = cc.shown(cc.Source(), "See `judge` in software.py.")[0]  # a long function: its first lines, and how many more
    assert long["part"] == "judge" and long["end"] - long["start"] + 1 == cc.SHOWN_LINES
    assert long["more"] == long["to"] - long["end"] > 0
    assert len(cc.shown(src, " ".join(f"small.py:{n}" for n in range(1, 31, 5)))) <= cc.SHOWN_MAX
    # the lines that were asked about are on the card already
    assert cc.shown(src, "small.py:26-29 and small.py:5", [("small.py", 20, 30)])[0]["mark"] == 5
    assert cc.shown(src, "nowhere.py:3 and small.py:999") == []


def test_a_code_block_the_ai_wrote_is_told_from_lines_of_the_files(tmp_path):
    src = tree(tmp_path)
    answer = ("As it stands:\n```python\nEVERY_MIN = 15  # how often it reads\n27: def thing(name: str = \"\"):\n```\n"
              "In config.yaml you would write:\n```yaml\nrefresh_minutes: 5\n```\nDone.")
    assert cc.blocks(src, answer) == [True, False]
    assert cc.blocks(src, "No block here.") == []
    # a name inside its own code block is not judged as a name it gave
    assert cc.check(src, "```\nmade_up_name(1)\n```", [], [""]) == []


# -- the chat ------------------------------------------------------------------------------------------
def test_a_question_is_answered_from_what_was_looked_up_in_the_files():
    src = cc.Source()
    ai = Script(call("search", text="admin_login"), call("read", name="admin_login"),
                {"content": "The password is checked in `admin_login` in web.py, which serves `/admin/api/login`."})
    a = cc.ask("Where is the  admin password checked?", [], ai, src)
    assert a["question"] == "Where is the admin password checked?" and a["note"] is None and a["flagged"] == []
    assert [s["tool"] for s in a["steps"]] == ["search", "read"] and a["looked"] == "search(text=admin_login); read(name=admin_login)"
    assert a["steps"][1]["summary"]["part"] == "create_app.admin_login" and a["about"] is None and a["service"] == "gpt"
    assert [(s["file"], s["part"]) for s in a["shown"]] == [("web.py", "create_app.admin_login")]
    first, last = ai.seen[0], ai.seen[-1]
    assert first[0]["content"].startswith(cc.SYSTEM) and "- web.py (" in first[0]["content"]
    assert "- static/index.html (" in first[0]["content"] and len(first[0]["content"]) < 12000
    assert last[-1]["role"] == "tool" and "def admin_login(body: LoginBody, request: Request):" in last[-1]["content"]
    given = json.loads(last[-1]["content"])
    assert not {"tool", "args", "code", "places"} & set(given)  # the page's own copies are not sent


def test_explain_these_lines_gives_the_lines_picked_and_the_function_around_them():
    src = cc.Source()
    part = src.file("chat.py").named("chat_service")[0]
    ai = Script({"content": f"These lines pick the service named by `ai.chat` (chat.py:{part.top + 3}), else the first one."})
    a = cc.ask("What do these lines do?", [], ai, src, {"file": "chat.py", "start": part.top + 2, "end": part.top + 3})
    assert len(a["steps"]) == 1 and a["steps"][0]["title"] == f"The lines asked about: chat.py, lines {part.top + 2}-{part.top + 3} (chat_service)"
    assert a["about"] == {"file": "chat.py", "start": part.top + 2, "end": part.top + 3,
                          "lines": src.file("chat.py").lines[part.top + 1: part.top + 3], "title": a["steps"][0]["title"]}
    asked = ai.seen[0][-1]["content"]
    assert asked.startswith(f"What do these lines do?\n\nThe question is about lines {part.top + 2}-{part.top + 3} of chat.py, "
                            f"which are in `chat_service` (chat.py:{part.top}-{part.end}). Lines {part.top}-{part.end} are given")
    assert f"{part.top}: def chat_service(settings: Settings) -> AiService | None:" in asked  # the whole function, numbered
    assert a["flagged"] == [] and a["shown"] == []  # the line it names is among those asked about: not shown twice
    # a part by its name (verify --ai asks so), and a whole file
    by_name = cc.ask("What does it do?", [], Script({"content": "It picks the service."}), src, {"file": "chat.py", "name": "chat_service"})
    assert by_name["about"]["start"] == part.top and by_name["about"]["end"] == part.end
    ai3 = Script({"content": "It is the chat."})
    whole = cc.ask("What does chat.py do?", [], ai3, src, {"file": "chat.py", "start": 0, "end": 0})
    assert whole["about"] is None and whole["steps"][0]["title"].startswith("The file asked about: chat.py, lines 1-")
    assert "\n\nThe question is about the file chat.py (" in ai3.seen[0][-1]["content"]
    # lines picked in a long function: those lines with some around them, never more than one read
    name, big = next((f.name, i) for f in src.files() for i in f.items
                     if f.name.endswith(".py") and i.kind == "function" and i.end - i.top > cc.AROUND + 100)
    step, told = cc.about_step(src, {"file": name, "start": big.top + 60, "end": big.top + 61})
    lo, hi = step["summary"]["given"]
    assert (lo, hi) == (big.top + 60 - cc.AROUND_LINES, big.top + 61 + cc.AROUND_LINES) and f"in `{big.full}`" in told
    many, _ = cc.about_step(src, {"file": "software.py", "start": 10, "end": 5000})
    assert many["summary"]["asked_about"] == [10, 10 + cc.ABOUT_LINES - 1]


def test_a_follow_up_question_carries_the_answers_before():
    src = cc.Source()
    ai = Script({"content": "8 hours: `ADMIN_HOURS`."})
    history = [{"q": "Where is the password checked?", "a": "In `admin_login` in web.py.", "looked": "read(name=admin_login)"},
               {"q": "", "a": "left out"}] + [{"q": f"q{n}", "a": f"a{n}"} for n in range(9)]
    a = cc.ask("And how long does a login last?", history, ai, src)
    sent = ai.seen[0]
    assert [m["role"] for m in sent] == ["system"] + ["user", "assistant"] * cc.HISTORY + ["user"]
    assert sent[-1]["content"] == "And how long does a login last?" and sent[1]["content"] == "q3"
    early = cc.ask("And then?", history[:1], Script({"content": "`admin_login` again."}), src)
    assert early["flagged"] == []  # a name from an answer before is not new
    assert "ADMIN_HOURS" in [x["text"] for x in a["flagged"]]  # in the code, but looked up by nobody in this chat
    ai2 = Script({"content": "ok"})
    cc.ask("x?", history[:1], ai2, src)
    assert 'What you looked up for the questions before in this chat:\n- "Where is the password checked?": read(name=admin_login)' in ai2.seen[0][0]["content"]


def test_the_look_ups_of_one_question_end_and_an_empty_answer_is_said():
    src = cc.Source()
    ai = Script(*[call("search", text="judge")] * 30)
    a = cc.ask("Keep looking", [], ai, src)
    assert len(a["steps"]) == cc.MAX_STEPS and a["answer"] == "" and a["note"] == f"The AI was still looking things up after {cc.MAX_STEPS} steps."
    assert ai.seen[-1][-1] == {"role": "user", "content": "Answer now with what you have found; no more look-ups."}
    assert cc.ask("Say nothing", [], Script({"content": ""}), src)["note"] == "The AI gave no answer."
    odd = cc.ask("Odd", [], Script(call("write", file="web.py"), {"content": "I cannot change code."}), src)
    assert odd["steps"][0]["title"] == "Not run" and odd["answer"] == "I cannot change code."


# -- the admin page's endpoints ---------------------------------------------------------------------------
def test_the_code_tab_needs_the_admin_login_and_reads_only_source_files(monkeypatch, tmp_path):
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    https = TestClient(web.create_app(), base_url="https://testserver")
    for path in ("/admin/api/code", "/admin/api/code/file?name=web.py", "/admin/api/code/search?q=judge",
                 "/admin/api/code/job/abc"):
        assert https.get(path).status_code == 401
    assert https.post("/admin/api/code/ask", json={"question": "x"}).status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    got = https.get("/admin/api/code").json()
    assert got["service"] is None and "web.py" in [f["file"] for f in got["files"]] and got["examples"] == list(cc.EXAMPLES)
    one = https.get("/admin/api/code/file?name=status").json()
    assert one["file"] == "status.py" and one["lines"][0].startswith('"""The connection lights')
    assert any(i["name"] == "CHECK_EVERY_S" and i["kind"] == "setting" for i in one["items"])
    for name in (".env", "../config.yaml", "/config/config.yaml", "..%2F..%2Fconfig.yaml", "nowhere.py", ""):
        assert https.get(f"/admin/api/code/file?name={name}").status_code == 404
    assert https.get("/admin/api/code/search?q=j").status_code == 400
    found = https.get("/admin/api/code/search?q=admin_login").json()
    assert found["rows"][0][0] == "web.py" and found["rows"][0][2] == "create_app.admin_login"
    r = https.post("/admin/api/code/ask", json={"question": "Where is it?"})
    assert r.status_code == 503 and "ai.services" in r.json()["detail"] and "Files part works without one" in r.json()["detail"]
    page = (web.STATIC / "admin.html").read_text(encoding="utf-8")
    assert 'data-t="code">Code<' in page and 'code: "code"' in page and '<section class="panel" id="code" hidden>' in page
    assert all(f'fetch("admin/api/code{x}' in page for x in ('"', '/file?name="', '/search?q="', '/ask"', '/job/"'))
    assert "confirm(" not in page and "alert(" not in page and "prompt(" not in page


def test_a_code_question_is_a_job_only_a_logged_in_admin_can_read_one_at_a_time(monkeypatch, tmp_path):
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    service = SimpleNamespace(name="GPT", model="m", key_env="", base_url="https://ai.test/v1", login="", timeout=5,
                              chat_path="/chat/completions", models_path="/models", query={})
    monkeypatch.setattr(web, "chat_service", lambda settings: service)
    go, asked = threading.Event(), []

    def fake(question, history, client, src, about):
        asked.append((question, history, about))
        assert go.wait(10)
        return {"question": question, "answer": "In web.py.", "steps": [], "looked": "", "flagged": [], "shown": [],
                "blocks": [], "about": None, "note": None, "service": "GPT", "seconds": 0.1}
    monkeypatch.setattr(cc, "ask", fake)
    https = TestClient(web.create_app(), base_url="https://testserver")
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    assert https.get("/admin/api/code").json()["service"] == "GPT"
    assert https.post("/admin/api/code/ask", json={"question": "  "}).status_code == 400
    job = https.post("/admin/api/code/ask", json={"question": "Where is it?", "history": [{"q": "a", "a": "b"}],
                                                  "about": {"file": "web.py", "start": 1, "end": 2}}).json()["id"]
    busy = https.post("/admin/api/code/ask", json={"question": "And this?"})
    assert busy.status_code == 409 and "is being answered" in busy.json()["detail"]
    assert https.get(f"/admin/api/code/job/{job}").json()["done"] is False
    go.set()
    for _ in range(100):
        got = https.get(f"/admin/api/code/job/{job}").json()
        if got["done"]:
            break
        time.sleep(0.05)
    assert got["error"] is None and got["result"]["answer"] == "In web.py."
    assert asked == [("Where is it?", [{"q": "a", "a": "b"}], {"file": "web.py", "start": 1, "end": 2})]
    # the main page has no login: its job address does not give an admin job, and a visitor gets nothing here
    assert https.get(f"/api/jobs/{job}").status_code == 404
    stranger = TestClient(https.app, base_url="https://testserver")
    assert stranger.get(f"/admin/api/code/job/{job}").status_code == 401
    assert https.get("/admin/api/code/job/nope").status_code == 404
    again = https.post("/admin/api/code/ask", json={"question": "And this?"})  # the one before is done: the next runs
    assert again.status_code == 200


# -- verify --ai's questions rest on names the code has ----------------------------------------------------
def test_the_names_verify_asks_the_code_chat_for_are_in_the_code():
    """A rename fails here, on dev, and not as a wrong FAIL of verify --ai on the live host."""
    src = cc.Source()

    def part(file, name):
        return src.file(file).named(name)[0]

    assert part("status.py", "CHECK_EVERY_S").says == "= 60"
    assert part("web.py", "admin_login").route == "POST /admin/api/login"
    assert (part("config.py", "refresh_minutes").inside, part("config.py", "refresh_minutes").says) == ("Settings", ": int = 15")
    assert part("software.py", "judge").kind == "function" and part("software.py", "_check").kind == "function"
    assert part("agent.py", "MAX_STEPS").says == "= 8"
    assert part("web.py", "care_status").route == "GET /api/MSS/care/status"
    assert part("static/index.html", "answerHtml").kind == "function" and part("static/index.html", "mdHtml").kind == "function"
    about = AI_CODE[-1][2]
    step, told = cc.about_step(src, about)
    assert "ai.chat" in step["text"] and "else the first one" in step["text"] and "`chat_service`" in told
    files = {g for _, must, _ in AI_CODE for group in must for g in group if g.endswith((".py", ".html"))}
    assert all(src.pick(f) for f in files) and len(AI_CODE) == 8


def test_a_host_without_the_code_chats_file_still_starts_and_says_what_is_missing(monkeypatch, tmp_path, caplog):
    """web.py copied to a host without code_chat.py: the app starts, the tab and verify say what to copy."""
    import sys

    from uxcorrelate import verify
    monkeypatch.setenv(web.ADMIN_ENV, "s3cret-test")
    monkeypatch.setenv("UXCORRELATE_DATA", str(tmp_path))
    import uxcorrelate
    monkeypatch.setitem(sys.modules, "uxcorrelate.code_chat", None)  # as if the file were not there
    monkeypatch.delattr(uxcorrelate, "code_chat")
    https = TestClient(web.create_app(), base_url="https://testserver")
    assert "The CODE tab is off: src/uxcorrelate/code_chat.py is not on this host" in caplog.text
    assert https.get("/admin/api/code").status_code == 401
    assert https.post("/admin/api/login", json={"password": "s3cret-test"}).status_code == 200
    assert https.get("/admin/api/usage").status_code == 200  # the other tabs work
    for r in (https.get("/admin/api/code"), https.get("/admin/api/code/file?name=web.py"),
              https.get("/admin/api/code/search?q=judge"), https.post("/admin/api/code/ask", json={"question": "x"})):
        assert r.status_code == 503 and "copy src/uxcorrelate/code_chat.py" in r.json()["detail"]
    said = []
    verify._code_answers(None, lambda item, ok, message, fix="", rows=(): said.append((item, ok, message)))
    assert said[0][:2] == ("answers questions about the code (admin page)", None) and "is not on this host" in said[0][2]

