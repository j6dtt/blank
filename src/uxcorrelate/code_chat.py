"""The admin page's CODE tab: this app's own source, listed, shown and asked about (the user,
2026-11-22: "I want to have a chat on admin to inquire source codes. I want to know what every
.py file does, I want to able able to ask where a configuration is, which part in the script does
xx/yy. I want the chat in prod using gpt and have the similar conversation with you", then "Is
this multi turn chat? Can I ask what this 'line of code' means?", "Build it"). The proposal and
what it cannot do: docs/admin-roadmap.md.

Two parts.
- Files, with no AI (`listing`, `file_view`, `search`): every source file with its lines and what
  it does (the file's own first sentence), its parts (settings, functions, classes, with their
  lines; Python's own `ast` reads them) and its code. Right also when the AI service is down.
- The chat (`ask`): the source is far more than a model reads at once (26,500 lines; both models
  read 40,000 characters right on stage), so the AI looks things up in the files on this host,
  as Ask anything does with Aternity (`agent.ask`): `search` (where a word or a name is),
  `outline` (one file's parts), `read` (lines of a file, or a function by its name), `setting`
  (a config.yaml key, an .env name or a constant: where it is defined and read). `MAX_STEPS`
  look-ups a question; the last `HISTORY` questions and answers go with a new one. "Explain these
  lines" gives it the lines picked on the page with the function around them (`about`).

What keeps it honest (`check`): a file it names that is not a source file here, a line outside
the lines it looked at, a name written as code that is in none of its look-ups (said apart when
the code has no such name at all) and a number found nowhere in them are marked under the answer,
not hidden. The code shown under an answer is read from the disk by this module (`shown`), never
the AI's own typing; a code block the AI writes itself is said to be its own (`blocks`).

What it can read: the files of this package's own folder (`ROOT`), `*.py` and `static/*.html`,
by the list only (`Source.names`): a file is never opened from a path that was typed or that the
AI sent. So config.yaml, .env, the certificates and the data folder cannot be read, and nothing
is ever written. A setting is told by its place and default in the code, never by its value on
this host. Nothing is kept: no question, no answer.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import io
import json
import keyword
import logging
import re
import threading
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AiClient
from .chat import allowed, numbers

log = logging.getLogger("uxcorrelate")

ROOT = Path(__file__).resolve().parent  # this package's folder: the only place read
ENDINGS = (".py", ".html")  # what counts as a source file there (and in its static folder)
MAX_STEPS = 8  # look-ups per question (as Ask anything's)
RESULT_CHARS = 24000  # one look-up as given to the AI, at most (as Ask anything's)
HISTORY = 6  # earlier questions and answers sent with a new one
QUESTION_MAX = 500
ANSWER_MAX = 6000
READ_LINES = 160  # lines of one read, at most
READ_CHARS = 20000  # ... and characters (the pages have lines of 350)
LINE_CHARS = 300  # one line as given to the AI or shown in a list, at most
SEARCH_ROWS = 60  # lines of a search given, definitions first
OUTLINE_ROWS = 250  # parts of one file given
SETTING_ROWS = 40  # places of a setting given
DOES_MAX = 220  # "what it does" in the list of files: the file's first sentence, at most
SAYS_MAX = 140  # a function's arguments or a setting's value, on one line
AROUND = 120  # "Explain these lines": the function around them is given whole up to this many lines ...
AROUND_LINES = 30  # ... a longer one: this many lines before and after them
ABOUT_LINES = READ_LINES  # lines that can be picked for "Explain these lines", at most
SHOWN_MAX = 5  # places an answer names, shown under it from the disk
SHOWN_LINES = 40  # lines of each
PER_FILE = 12  # files named in a search's count of lines per file

# the two pages have no docstring: what each does, in our words
PAGES = {
    "static/index.html": "The main page, one file: every tab (Today, Care list, Investigate, Start-ups, Software, "
                         "Inventory) with its styles and its script, the charts drawn as SVG, and Ask anything.",
    "static/admin.html": "The admin page (/admin), one file: its login and its tabs (Statistics, SecureView, Actions, "
                         "Code) with their styles and script.",
}
EMPTY_INIT = "Empty: it only marks this folder as the Python package."
EXAMPLES = ("What does every .py file do?", "Where is refresh_minutes set, and where is it read?",
            "Which part of the code checks the admin password?", "How does the Care list decide that a tile is lit?",
            "What happens when Aternity does not answer?")

SYSTEM = (
    "You are the guide to the source code of UX Correlator, for the people who run it. UX Correlator is a Python "
    "web app (FastAPI) with two single-page HTML pages; it reads Riverbed Aternity and NetIM and shows how PCs, "
    "users and applications are doing. Answer as a colleague who knows this code would, in plain English, for "
    "someone who may not be a programmer.\n"
    "How to work:\n"
    "- Look in the code with the tools before you answer, as many steps as you need: search finds where a word or "
    "a name is, outline lists one file's parts, read gives the lines themselves, setting finds a setting. Never "
    "describe code you have not read in this chat: every file, function, setting and line you name comes from a "
    "tool's result or from the list of files below. To say what a function does, read it first. The list of "
    "files says what each file is for: it is no proof of how one setting or one function behaves, so say that "
    "only of lines you have read.\n"
    "- Say where: name the file and the function, and the lines as file:first-last, for example web.py:655-700. "
    "Use the line numbers the tools gave; never guess one.\n"
    "- Lead with the answer in a sentence or two, then the few facts behind it as a short list with \"- \". Say "
    "what the code does and why, in words. Do not paste code: the page shows the lines you name, read from the "
    "disk. A name from the code goes in `backticks`, exactly as the code writes it.\n"
    "- A setting: say the file and line where it is defined, its default, and where it is read. Its value on this "
    "host is not known to you (config.yaml and .env are never read here): say so when asked for it.\n"
    "- When the code does not have something, say so plainly and say what it does have. When a question is "
    "unclear, answer what you can and ask one short question back.\n"
    "- You can only read this app's source files. You cannot change or run code, and you cannot see data, logs, "
    "passwords or any other file."
)

_S = {"type": "string"}
_N = {"type": "integer"}


def _tool(name: str, description: str, props: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props}}}


TOOLS = [
    _tool("search", "Where a word, a name or a phrase is in the code, any case: each line that has it with its "
                    "file, its line number and the function it is in, the lines that define it first, and how many "
                    f"lines have it per file. Several words with no line that has them together: the lines with all "
                    f"of them. At most {SEARCH_ROWS} lines are given: narrow with file.", {
        "text": {"type": "string", "description": "A word, a name (judge, MAX_STEPS, /api/care) or a few words."},
        "file": {"type": "string", "description": "Only this file, e.g. software.py."}}),
    _tool("outline", "One file's parts with their line numbers: its settings (constants) with their values, its "
                     "functions and classes with what each does (its own first sentence), and for web.py the "
                     "address each function serves. Use it to find the function to read.", {
        "file": _S,
        "text": {"type": "string", "description": "Only the parts whose name or sentence has this."}}),
    _tool("read", f"Lines of one file, each with its number: from start to end (at most {READ_LINES} lines at "
                  "once), or the whole of one function, class or setting by its name (name; with no file, wherever "
                  "it is defined).", {
        "file": _S, "start": _N, "end": _N,
        "name": {"type": "string", "description": "A function, class or setting, e.g. judge or Care.check."}}),
    _tool("setting", "Where a setting is: a key of config.yaml (refresh_minutes, or ai.chat for a key inside a "
                     "section), a line of .env (ADMIN_PASSWORD) or a constant in the code (RETRY_MIN). Where it is "
                     "defined with its default and its comment, and every place that reads it. Never its value on "
                     "this host.", {"name": _S}),
]

SETTING = re.compile(r"^_?[A-Z][A-Z0-9_]*$")  # a module's constant: capitals
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
FILE_RE = r"(?:static/)?[A-Za-z_][\w-]*\.(?:py|html)"
FILE_NAME = re.compile(rf"(?<![\w/.-]){FILE_RE}(?![\w-])")
# a place as an answer writes it: "web.py:655-700", "web.py, lines 655 to 700", "lines 655-700 of web.py"
_SPAN = r"(?P<a>\d{1,5})(?:\s*(?:-|to|through)\s*(?P<b>\d{1,5}))?"
_DASH = r"(?P<a>\d{1,5})(?:\s*-\s*(?P<b>\d{1,5}))?"  # after "file:" only a dash makes a range: "web.py:5 to 9 tries" is line 5
# (the first with nothing around its colon: "agent.py: 8 a question" is a sentence, not line 8)
CITES = (re.compile(rf"(?<![\w/.-])(?P<file>{FILE_RE}):{_DASH}"),
         re.compile(rf"(?<![\w/.-])(?P<file>{FILE_RE})[^.\n;:]{{0,40}}?\blines?\s+{_SPAN}", re.I),
         re.compile(rf"\blines?\s+{_SPAN}\s+(?:of|in)\s+(?P<file>{FILE_RE})", re.I))
FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)
TICKS = re.compile(r"`([^`\n]+)`")
# names that are the language's, not this code's: never looked for
STOP = (set(keyword.kwlist) | set(dir(builtins)) | set(
    "self cls args kwargs true false null undefined const let var function async await return new this typeof "
    "document window console json yaml http https html css python fastapi javascript".split()))
# where a sentence ends: not after "e.g." or "i.e."
SENTENCE = re.compile(r"(?<=[.!?])(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)(?<!vs\.)\s+(?=[A-Z\"'`(\[])")
JS_FUNC = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)")
JS_ARROW = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>")
JS_CONST = re.compile(r"^(?:const|let|var)\s+([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
JS_DECL = re.compile(r"^(?:(?:async\s+)?function|const|let|var|class)\s")
JS_NOTE = re.compile(r"\s//\s?(.*)$")


def _one(text, size: int) -> str:
    """On one line, at most `size` characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[: size - 2].rstrip() + " …"


def _dashes(text: str) -> str:
    """Every dash a "-", every odd space a space: a model writes "655–700" and no-break spaces."""
    return re.sub(r"[\u2010-\u2015\u2212]", "-", str(text or "")).replace("\u00a0", " ").replace("\u202f", " ")


def first_sentences(text, least: int = 40, most: int = DOES_MAX) -> str:
    """The first sentence of a docstring, with the next one when it is very short ("Configuration
    loading."), cut at a word. A list that follows a short first line is begun and left open (" …")."""
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", str(text or "").strip()) if p.strip()]
    out = ""
    for n, paragraph in enumerate(paragraphs[:2]):
        item = re.match(r"(?:\d{1,2}[.)]|[-*•])\s+(.*)$", paragraph) if n else None
        for part in SENTENCE.split(item.group(1) if item else paragraph):
            out = f"{out} {part}".strip()
            if len(out) >= least or item:
                break
        if item:
            out += " …"
        if len(out) >= least or item:
            break
    if len(out) > most:
        out = out[:most].rsplit(" ", 1)[0].rstrip(",;:(") + " …"
    return out


@dataclass
class Item:
    """One part of a file: a setting, a class with its fields and methods, a function."""
    kind: str  # setting, field, function, method, class
    name: str
    line: int  # where it starts ("def", "class", its own line)
    end: int
    top: int  # ... with what is written over it (@app.get(...))
    inside: str = ""  # the class or function it is in
    says: str = ""  # a function's arguments, a setting's value: one line
    doc: str = ""  # its own first sentence, or its comment
    route: str = ""  # web.py: the address it serves, "GET /api/today"

    @property
    def full(self) -> str:
        return f"{self.inside}.{self.name}" if self.inside else self.name

    def what(self) -> str:
        return " · ".join(x for x in (self.route, self.says, self.doc) if x)


@dataclass
class File:
    name: str
    lines: list[str]
    does: str = ""  # its first sentence
    about: str = ""  # its whole docstring
    items: list[Item] = field(default_factory=list)
    owner: list[str] = field(default_factory=list)  # per line: the function or class it is in ("" outside any)
    error: str = ""  # a file Python could not read as code: its lines are still shown

    def inside(self, line: int) -> str:
        return self.owner[line - 1] if 0 < line <= len(self.owner) else ""

    def named(self, name: str) -> list[Item]:
        """The parts with this name ("judge", "Care.check"): as written first, then any case."""
        name = str(name or "").strip().strip("`'\"").removesuffix("()")
        got = [i for i in self.items if name in (i.name, i.full)]
        low = name.lower()
        return got or [i for i in self.items if low in (i.name.lower(), i.full.lower())]


# -- reading a file's parts -------------------------------------------------------------------------
def _comments(text: str) -> dict[int, tuple[bool, str]]:
    """Each line's comment: (alone on its line?, its words)."""
    out: dict[int, tuple[bool, str]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = (not tok.line[: tok.start[1]].strip(), tok.string.lstrip("#").strip())
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def _route(node) -> str:
    """The address a web function serves, from the @app.get("/x") over it."""
    for d in node.decorator_list:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.args
                and d.func.attr in ("get", "post", "put", "delete", "patch")
                and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)):
            return f"{d.func.attr.upper()} {d.args[0].value}"
    return ""


def _python(text: str) -> tuple[str, list[Item]]:
    """A Python file's docstring and parts, by Python's own reading of it (ast)."""
    tree = ast.parse(text)
    notes = _comments(text)
    items: list[Item] = []

    def said(node) -> str:  # a setting's comment: after it on its line, else the comment lines just above it
        got = notes.get(node.lineno)
        if got and not got[0]:
            return _one(got[1], DOES_MAX)
        above, n = [], node.lineno - 1
        while n in notes and notes[n][0]:
            above.append(notes[n][1])
            n -= 1
        return _one(" ".join(reversed(above)), DOES_MAX)

    def walk(body, inside: str, in_class: bool, top: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                items.append(Item("method" if in_class else "function", node.name, node.lineno, node.end_lineno,
                                  first, inside, _one(f"({ast.unparse(node.args)})", SAYS_MAX),
                                  first_sentences(ast.get_docstring(node) or ""), _route(node)))
                walk(node.body, f"{inside}.{node.name}" if inside else node.name, False, False)
            elif isinstance(node, ast.ClassDef):
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                items.append(Item("class", node.name, node.lineno, node.end_lineno, first, inside,
                                  f"({bases})" if bases else "", first_sentences(ast.get_docstring(node) or "")))
                walk(node.body, f"{inside}.{node.name}" if inside else node.name, True, False)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and (top or in_class):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for t in targets for n in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t])
                         if isinstance(n, ast.Name)]
                kind = isinstance(node, ast.AnnAssign) and ast.unparse(node.annotation) or ""
                value = "" if node.value is None else (ast.get_source_segment(text, node.value) or "")
                says = _one((f": {kind}" if kind else "") + (f" = {value}" if value else ""), SAYS_MAX)
                for name in names:
                    if in_class or SETTING.match(name):
                        items.append(Item("field" if in_class else "setting", name, node.lineno,
                                          node.end_lineno or node.lineno, node.lineno, inside, says, said(node)))
            else:  # a def inside an if or a try is still a part of the file
                for part in ("body", "orelse", "finalbody"):
                    inner = getattr(node, part, None)
                    if isinstance(inner, list):
                        walk(inner, inside, in_class, top)
                for handler in getattr(node, "handlers", []) or []:
                    walk(handler.body, inside, in_class, top)

    walk(tree.body, "", False, True)
    return ast.get_docstring(tree) or "", items


def _closed(line: str) -> bool:
    """Does a line of the page's script end what it opens (its brackets, outside quotes)?"""
    depth, quote, skip = 0, "", False
    for i, c in enumerate(line):
        if skip:
            skip = False
        elif quote:
            if c == "\\":
                skip = True
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "/" and line[i + 1: i + 2] == "/" and (i == 0 or line[i - 1].isspace()):
            break  # a comment to the end of the line
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
    return depth <= 0 and not quote


def _page(lines: list[str]) -> list[Item]:
    """A page's script by its top-level lines: functions, and constants written in capitals. Where a
    function ends is read from the script's layout (the "}" at the start of a line), not parsed."""
    spans, inside, start = [], False, 0
    for n, raw in enumerate(lines, 1):
        if not inside and re.match(r"\s*<script[^>]*>\s*$", raw):
            inside, start = True, n + 1
        elif inside and "</script>" in raw:
            spans.append((start, n - 1))
            inside = False
    items: list[Item] = []
    for first, last in spans:
        decls = [n for n in range(first, last + 1) if JS_DECL.match(lines[n - 1])] + [last + 1]
        for at, nxt in zip(decls, decls[1:]):
            raw = lines[at - 1]
            fn, arrow, const = JS_FUNC.match(raw), JS_ARROW.match(raw), JS_CONST.match(raw)
            if not (fn or arrow or const):
                continue
            end = at
            if not _closed(raw):
                close = next((n for n in range(at + 1, nxt) if lines[n - 1][:1] in "}])"), None)
                end = close or nxt - 1
                while not close and end > at and (not lines[end - 1].strip() or lines[end - 1].startswith("//")):
                    end -= 1  # the blank lines and the comment that belong to the next part
            note = JS_NOTE.search(raw)
            above, n = [], at - 1
            while not note and n >= first and lines[n - 1].startswith("//"):
                above.append(lines[n - 1].lstrip("/ ").strip())
                n -= 1
            doc = _one(note.group(1) if note else " ".join(reversed(above)), DOES_MAX)
            if fn or arrow:
                m = fn or arrow
                args = m.group(2) if m.group(2) is not None else (m.group(3) if arrow else "")
                items.append(Item("function", m.group(1), at, end, at, "", _one(f"({args or ''})", SAYS_MAX), doc))
            else:
                value = JS_NOTE.sub("", const.group(2)).rstrip(" ;")
                items.append(Item("setting", const.group(1), at, end, at, "", _one(f"= {value}", SAYS_MAX), doc))
    return items


def parse(name: str, text: str) -> File:
    lines = text.splitlines()
    f = File(name, lines)
    if name.endswith(".py"):
        try:
            f.about, f.items = _python(text)
        except (SyntaxError, ValueError) as exc:
            f.error = f"Python could not read this file as code ({_one(exc, 120)}): its lines are shown, its parts are not."
        f.does = first_sentences(f.about) or (EMPTY_INIT if not text.strip() else "")
    else:
        f.items = _page(lines)
        f.about = f.does = PAGES.get(name, "A page of the app.")
    f.owner = [""] * len(lines)
    for it in sorted((i for i in f.items if i.kind in ("function", "method", "class")), key=lambda i: (i.top, -i.end)):
        for n in range(it.top, min(it.end, len(lines)) + 1):  # an inner part comes later and wins
            f.owner[n - 1] = it.full
    return f


class Source:
    """This app's source files, read from its own folder: each read once, and again when it
    changes on the disk (a code fix is a restart anyway)."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self._kept: dict[str, tuple] = {}
        self._words: tuple = (None, frozenset())

    def names(self) -> list[str]:
        """Every source file, as it is named everywhere here: the .py files, then the pages."""
        def own(p: Path, folder: Path) -> bool:  # a real file of that folder: no link that leads out of it
            return p.suffix in ENDINGS and p.is_file() and not p.is_symlink() and p.resolve().parent == folder
        static = self.root / "static"
        return (sorted(p.name for p in self.root.glob("*.py") if own(p, self.root))
                + sorted(f"static/{p.name}" for p in static.glob("*.html") if own(p, static)))

    def pick(self, text) -> str | None:
        """The listed file a typed name means: "software.py", "software", "uxcorrelate/software.py",
        "index.html". Nothing else is ever opened."""
        t = str(text or "").strip().strip("`'\"").replace("\\", "/").lower()
        t = re.sub(r"^(?:\./)?(?:src/)?(?:uxcorrelate[/.])?", "", t)
        names = self.names()
        for same in (lambda n: n.lower() == t, lambda n: n.lower().rsplit("/", 1)[-1] == t.rsplit("/", 1)[-1],
                     lambda n: n.lower().rsplit("/", 1)[-1].rsplit(".", 1)[0] == t):
            got = [n for n in names if same(n)]
            if len(got) == 1:
                return got[0]
        return None

    def file(self, name: str) -> File | None:
        if name not in self.names():
            return None
        path = self.root / name
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        with self.lock:
            kept = self._kept.get(name)
            if kept and kept[0] == key:
                return kept[1]
        f = parse(name, path.read_text(encoding="utf-8", errors="replace"))
        with self.lock:
            self._kept[name] = (key, f)
        return f

    def files(self) -> list[File]:
        return [f for f in (self.file(n) for n in self.names()) if f is not None]

    def words(self) -> frozenset:
        """Every name written anywhere in the code (to tell a made-up name from one not looked up)."""
        files = self.files()
        key = tuple((f.name, len(f.lines), id(f)) for f in files)
        with self.lock:
            if self._words[0] == key:
                return self._words[1]
        got = frozenset(w for f in files for line in f.lines for w in IDENT.findall(line))
        with self.lock:
            self._words = (key, got)
        return got


# -- the Files part: no AI ---------------------------------------------------------------------------
def listing(src: Source) -> dict:
    files = src.files()
    return {"files": [{"file": f.name, "lines": len(f.lines), "does": f.does,
                       "parts": sum(1 for i in f.items if i.kind in ("function", "method", "class"))} for f in files],
            "total_lines": sum(len(f.lines) for f in files), "examples": list(EXAMPLES),
            "limits": {"steps": MAX_STEPS, "history": HISTORY, "question": QUESTION_MAX, "about": ABOUT_LINES}}


def file_view(src: Source, name: str) -> dict | None:
    """One file for the page: what it does, its parts and every line."""
    picked = src.pick(name)
    f = src.file(picked) if picked else None
    if f is None:
        return None
    return {"file": f.name, "does": f.does, "about": f.about, "error": f.error, "lines": f.lines,
            "items": [{"kind": i.kind, "name": i.name, "full": i.full, "inside": i.inside, "line": i.line,
                       "top": i.top, "end": i.end, "says": i.says, "doc": i.doc, "route": i.route}
                      for i in f.items]}


# -- the look-ups ------------------------------------------------------------------------------------
def _step(tool: str, args: dict, title: str, columns=(), rows=(), summary=None, notes=(), text=None,
          code=None, places=()) -> dict:
    """One look-up for the page and the AI. `places`: the lines it showed, (file, first, last);
    `code`: the lines of a read, for the page."""
    out = {"tool": tool, "args": args, "title": title, "columns": list(columns), "rows": [list(r) for r in rows],
           "total_rows": len(rows), "notes": list(notes), "places": [list(p) for p in places]}
    if summary is not None:
        out["summary"] = summary
    if text is not None:
        out["text"] = text
    if code is not None:
        out["code"] = code
    return out


def _no_file(src: Source, tool: str, args: dict, typed) -> dict:
    close = difflib.get_close_matches(str(typed or "").lower(), [n.lower() for n in src.names()], 4, 0.5)
    return _step(tool, args, "Not found", notes=[
        f"There is no source file named \"{_one(typed, 80)}\"." + (f" Close: {', '.join(close)}." if close else "")
        + " The files are in the list you were given."])


def search(src: Source, text=None, file=None, tool: str = "search") -> dict:
    args = {k: v for k, v in (("text", text), ("file", file)) if v not in (None, "")}
    q = " ".join(str(text or "").split()).strip("`")
    if len(q) < 2:
        return _step(tool, args, "Not run", notes=["Give a word or a name of 2 or more characters."])
    only = None
    if file not in (None, ""):
        only = src.pick(file)
        if only is None:
            return _no_file(src, tool, args, file)
    files = [f for f in src.files() if only in (None, f.name)]
    low, notes = q.lower(), []
    hits = [(f, n) for f in files for n, line in enumerate(f.lines, 1) if low in line.lower()]
    words = low.split()
    # "MAX_STEPS|MAX_READS", as a model writes several names at once (Nemotron, dev, Nov 2026): any of them
    any_of = [a.strip() for a in low.split("|") if len(a.strip()) >= 2] if "|" in low else []
    if not hits and len(any_of) > 1:
        hits = [(f, n) for f in files for n, line in enumerate(f.lines, 1) if any(a in line.lower() for a in any_of)]
        if hits:
            notes.append("Read as several names: the lines with any of " + ", ".join(f"\"{a}\"" for a in any_of) + ".")
    if not hits and len(words) > 1 and not any_of:
        hits = [(f, n) for f in files for n, line in enumerate(f.lines, 1) if all(w in line.lower() for w in words)]
        if hits:
            notes.append("No line has these words together: these have all of them.")
        else:
            per = {w: sum(1 for f in files for line in f.lines if w in line.lower()) for w in words}
            notes.append("No line has all of these words. Lines with each: "
                         + ", ".join(f"\"{w}\" {n:,}" for w, n in per.items()) + ". Search for one of them.")
    where = f" in {only}" if only else ""
    if not hits:
        if len(words) == 1:
            close = difflib.get_close_matches(q, sorted(src.words()), 6, 0.75)
            notes.append(f"No line{where} has \"{q}\"." + (f" Names like it in the code: {', '.join(close)}." if close else ""))
        return _step(tool, args, f"No line with \"{_one(q, 60)}\"{where}", notes=notes,
                     summary={"lines_with_it": 0})
    defines = {(f.name, i.line) for f in files for i in f.items
               if {i.name.lower(), i.full.lower()} & {low, *any_of}}
    hits.sort(key=lambda h: (h[0].name, h[1]) not in defines)  # the lines that define it first; the rest as found
    per: dict[str, int] = {}
    for f, _ in hits:
        per[f.name] = per.get(f.name, 0) + 1
    rows = [[f.name, n, f.inside(n), _one(f.lines[n - 1].strip(), LINE_CHARS)] for f, n in hits[:SEARCH_ROWS]]
    if len(hits) > SEARCH_ROWS:
        notes.append(f"The first {SEARCH_ROWS} of {len(hits):,} lines (those that define it first); the count per "
                     "file is of all of them. Narrow with file, or a longer text.")
    top = sorted(per.items(), key=lambda kv: -kv[1])
    return _step(tool, args, f"Lines with \"{_one(q, 60)}\"{where}: {len(hits):,} in {len(per)} file{'s' if len(per) != 1 else ''}",
                 ["File", "Line", "In", "Code"], rows,
                 {"lines_with_it": len(hits), "files": len(per),
                  "per_file": dict(top[:PER_FILE]) | ({"other files": sum(n for _, n in top[PER_FILE:])} if len(top) > PER_FILE else {})},
                 notes, places=[(r[0], r[1], r[1]) for r in rows])


def outline(src: Source, file=None, text=None) -> dict:
    args = {k: v for k, v in (("file", file), ("text", text)) if v not in (None, "")}
    name = src.pick(file)
    f = src.file(name) if name else None
    if f is None:
        return _no_file(src, "outline", args, file)
    low = " ".join(str(text or "").lower().split())
    items = [i for i in f.items if not low or low in f"{i.full} {i.what()}".lower()]
    notes = [f.error] if f.error else []
    kinds = {k: sum(1 for i in f.items if i.kind == k) for k in ("setting", "function", "class", "method", "field")}
    rows = [[i.kind, i.full, f"{i.top}-{i.end}" if i.end > i.top else str(i.top), i.what()] for i in items[:OUTLINE_ROWS]]
    if len(items) > OUTLINE_ROWS:
        notes.append(f"The first {OUTLINE_ROWS} of {len(items):,} parts, in the file's order: narrow with text, or search.")
    if low and not items:
        notes.append(f"No part of {f.name} has \"{_one(text, 60)}\" in its name or sentence: search looks in every line.")
    if not f.name.endswith(".py"):
        notes.append("A page: its script's top-level functions and constants; where a function ends is read from "
                     "the layout, so read a few lines more when in doubt.")
    return _step("outline", args, f"{f.name}: {len(items):,} part{'s' if len(items) != 1 else ''}"
                 + (f" with \"{_one(text, 40)}\"" if low else ""), ["Kind", "Name", "Lines", "What"], rows,
                 {"file": f.name, "lines": len(f.lines), "does": f.does,
                  "settings": kinds["setting"], "functions": kinds["function"], "classes": kinds["class"],
                  "methods": kinds["method"], "fields": kinds["field"]}, notes,
                 places=[p for i in items[:OUTLINE_ROWS] for p in ((f.name, i.top, i.top), (f.name, i.line, i.line),
                                                                   (f.name, i.end, i.end))])


def numbered(f: File, first: int, last: int) -> tuple[str, int]:
    """Lines first to last as the AI is given them ("412: code"), and the last one that fitted."""
    out, size = [], 0
    for n in range(first, last + 1):
        line = f"{n}: {_one_line(f.lines[n - 1])}"
        size += len(line) + 1
        if out and size > READ_CHARS:
            return "\n".join(out), n - 1
        out.append(line)
    return "\n".join(out), last


def _one_line(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " …(line cut)"


def read(src: Source, file=None, start=None, end=None, name=None) -> dict:
    args = {k: v for k, v in (("file", file), ("start", start), ("end", end), ("name", name)) if v not in (None, "")}
    notes: list[str] = []
    f, item = None, None
    if file not in (None, ""):
        picked = src.pick(file)
        f = src.file(picked) if picked else None
        if f is None:
            return _no_file(src, "read", args, file)
    if name not in (None, ""):
        found = [(g, i) for g in ([f] if f else src.files()) for i in g.named(name)]
        if not found and f:  # not in that file: say where it is
            others = [(g, i) for g in src.files() for i in g.named(name)]
            if others:
                return _step("read", args, "Not found", notes=[
                    f"{f.name} has no part named \"{_one(name, 60)}\". It is in: "
                    + "; ".join(f"{g.name} ({i.full}, lines {i.top}-{i.end})" for g, i in others[:6]) + "."])
        if not found:
            close = difflib.get_close_matches(str(name), sorted({i.name for g in src.files() for i in g.items}), 6, 0.7)
            return _step("read", args, "Not found", notes=[
                f"No function, class or setting is named \"{_one(name, 60)}\"" + (f" in {f.name}" if f else "") + "."
                + (f" Close: {', '.join(close)}." if close else "") + " search looks in every line."])
        f, item = found[0]
        if len(found) > 1:
            notes.append("Others of that name: " + "; ".join(f"{g.name} ({i.full}, lines {i.top}-{i.end})"
                                                             for g, i in found[1:7]) + ".")
        first, last = item.top, item.end
    elif f is None:
        return _step("read", args, "Not run", notes=["Give a file (with start and end), or a name."])
    else:
        try:
            first = max(1, int(start)) if start not in (None, "") else 1
            last = int(end) if end not in (None, "") else first + READ_LINES - 1
        except (TypeError, ValueError):
            return _step("read", args, "Not run", notes=["start and end are line numbers."])
        if last < first:  # "start=318, end=40" (Nemotron, dev, Nov 2026): meant as so many lines from start
            last = first + min(max(last, 1), READ_LINES) - 1
            notes.append(f"end was before start: read as {last - first + 1} lines from line {first}.")
    total = len(f.lines)
    if not total:
        return _step("read", args, f"{f.name} is empty", notes=[f.does or "The file has no line."])
    if first > total:
        return _step("read", args, "Not found", notes=[f"{f.name} has {total:,} lines: there is no line {first:,}."])
    last = max(first, min(last, total))
    wanted = last
    last = min(last, first + READ_LINES - 1)
    text, last = numbered(f, first, last)
    if last < wanted:
        notes.append(f"Lines {first}-{last} of {first}-{wanted} (at most {READ_LINES} lines at once): read on with "
                     f"start={last + 1}.")
    if f.error:
        notes.append(f.error)
    part = item.full if item else f.inside(first)
    title = f"{f.name}, line{'s' if last > first else ''} {first}{f'-{last}' if last > first else ''}" \
            + (f" ({part})" if part else "")
    return _step("read", args, title, text=text, notes=notes,
                 summary={"file": f.name, "first": first, "last": last, "of": total} | ({"part": part} if part else {}),
                 code={"file": f.name, "start": first, "end": last, "lines": f.lines[first - 1: last]},
                 places=[(f.name, first, last)])


def setting(src: Source, name=None) -> dict:
    args = {"name": name} if name not in (None, "") else {}
    q = str(name or "").strip().strip("`'\"")
    parts = [p for p in re.split(r"[.\s:/\[\]]+", q) if p]
    if not parts:
        return _step("setting", args, "Not run", notes=["Give the setting's name."])
    key, section = parts[-1], parts[:-1]
    files = src.files()
    every = [(f, i) for f in files for i in f.items if i.kind in ("setting", "field")]
    defs = [(f, i) for f, i in every if i.name == key] or [(f, i) for f, i in every if i.name.lower() == key.lower()]
    if defs:
        key = defs[0][1].name
    if section and len(defs) > 1:  # ai.chat: the field named chat of the class the field ai is of
        of = {c for _, i in every if i.kind == "field" and i.name.lower() == section[-1].lower()
              for c in re.findall(r"[A-Z]\w+", i.says)}
        defs = [d for d in defs if d[1].inside in of] or defs
    rows, seen = [], set()

    def add(what: str, f: File, n: int) -> None:
        if (f.name, n) not in seen:
            seen.add((f.name, n))
            rows.append([what, f.name, n, f.inside(n), _one(f.lines[n - 1].strip(), LINE_CHARS)])

    for f, i in defs:
        add("defined" + (f" in {i.inside}" if i.inside else ""), f, i.line)
    quoted = (f'"{key}"', f"'{key}'")
    word = re.compile(rf"(?<![\w$]){re.escape(key)}(?![\w$])")
    as_field = bool(defs) and all(i.kind == "field" for _, i in defs)
    dotted = re.compile(rf"(?<![\w$]){re.escape(section[-1])}\.{re.escape(key)}(?![\w$])") if section else None
    attr = re.compile(rf"\.{re.escape(key)}(?![\w$])")
    used = []
    for f in files:
        for n, line in enumerate(f.lines, 1):
            if any(s in line for s in quoted):
                add("named in quotes", f, n)
            elif dotted is not None and dotted.search(line):
                used.append((f, n))
            elif (attr if as_field else word).search(line):
                used.append((f, n))
    if dotted is not None and any(dotted.search(f.lines[n - 1]) for f, n in used):
        used = [(f, n) for f, n in used if dotted.search(f.lines[n - 1])]  # ai.chat, not every ".chat"
    for f, n in used:
        add("used", f, n)
    # a constant that holds this name (ADMIN_ENV = "ADMIN_PASSWORD"): where that one is used is where this is read
    for f, i in every:
        if i.kind == "setting" and any(s in i.says for s in quoted) and i.name != key:
            alias = re.compile(rf"(?<![\w$]){re.escape(i.name)}(?![\w$])")
            for g in files:
                for n, line in enumerate(g.lines, 1):
                    if alias.search(line):
                        add(f"used as {i.name}", g, n)
    notes = ["Its value on this host is not read here: config.yaml and .env are never opened by the code chat."]
    if not rows:
        names = sorted({i.name for _, i in every})
        close = difflib.get_close_matches(key, names, 6, 0.6) or [n for n in names if key.lower() in n.lower()][:6]
        return _step("setting", args, f"No setting named \"{_one(q, 60)}\"", notes=[
            f"The code has no setting, key or constant named \"{_one(key, 60)}\"."
            + (f" Close: {', '.join(close)}." if close else "") + " search looks in every line."],
            summary={"places": 0})
    per: dict[str, int] = {}
    for r in rows:
        per[r[1]] = per.get(r[1], 0) + 1
    if len(rows) > SETTING_ROWS:
        notes.append(f"The first {SETTING_ROWS} of {len(rows):,} places (where it is defined first); the count per "
                     "file is of all of them.")
    given = rows[:SETTING_ROWS]
    return _step("setting", args, f"Where \"{key}\" is: {len(rows):,} place{'s' if len(rows) != 1 else ''}",
                 ["What", "File", "Line", "In", "Code"], given,
                 {"setting": key, "defined": [f"{f.name}:{i.line}" + (f" ({i.full})" if i.inside else "") for f, i in defs],
                  "default": defs[0][1].says if defs else None, "its_comment": defs[0][1].doc if defs else None,
                  "places": len(rows), "per_file": per}, notes,
                 places=[(r[1], r[2], r[2]) for r in given])


def run(src: Source, name: str, args: dict) -> dict:
    """One look-up the AI asked for. Its settings are the AI's: anything that is not as expected
    is a step that says so, never an error."""
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    try:
        if name == "search":
            return search(src, args.get("text"), args.get("file"))
        if name == "outline":
            return outline(src, args.get("file"), args.get("text"))
        if name == "read":
            return read(src, args.get("file"), args.get("start"), args.get("end"), args.get("name"))
        if name == "setting":
            return setting(src, args.get("name"))
    except (TypeError, ValueError, KeyError, OSError) as exc:
        return _step(name, args, "Not run", notes=[f"The settings could not be used ({_one(exc, 120)})."])
    return _step(name, args, "Not run", notes=[f"There is no tool named {name}: search, outline, read or setting."])


def for_ai(step: dict) -> str:
    """One look-up as the AI is given it: without the page's own copies, cut to RESULT_CHARS."""
    given = {k: v for k, v in step.items() if k not in ("tool", "args", "code", "places")}
    text = json.dumps(given, default=str, ensure_ascii=False)
    while len(text) > RESULT_CHARS and given.get("rows"):
        given["rows"] = given["rows"][: len(given["rows"]) // 2]
        given["notes"] = step["notes"] + [f"Only the first {len(given['rows'])} rows are given here (size)."]
        text = json.dumps(given, default=str, ensure_ascii=False)
    return text[:RESULT_CHARS]


# -- "Explain these lines" ---------------------------------------------------------------------------
def about_step(src: Source, about) -> tuple[dict | None, str]:
    """The lines picked on the page (or a part by its name), as a look-up already made: the lines
    and the function around them. Returns the step and what the AI is told of it."""
    if not isinstance(about, dict) or not about.get("file"):
        return None, ""
    name = src.pick(about.get("file"))
    f = src.file(name) if name else None
    if f is None or not f.lines:
        return None, ""
    total = len(f.lines)
    item = None
    if about.get("name"):
        item = next(iter(f.named(about["name"])), None)
    try:
        first = int(about.get("start") or (item.top if item else 0))
        last = int(about.get("end") or (item.end if item else first))
    except (TypeError, ValueError):
        return None, ""
    if first < 1:  # the whole file: its top, the AI reads on
        step = read(src, f.name)
        step["title"] = f"The file asked about: {step['title']}"
        return step, f"The question is about the file {f.name} ({total:,} lines): {f.does}"
    first = min(first, total)
    last = max(first, min(last, total, first + ABOUT_LINES - 1))
    around = [i for i in f.items if i.kind in ("function", "method", "class") and i.top <= first and last <= i.end]
    around.sort(key=lambda i: i.end - i.top)
    part = around[0] if around else None
    if part and part.end - part.top + 1 <= AROUND:
        lo, hi = part.top, part.end
    else:
        lo, hi = max(1, first - AROUND_LINES), min(total, last + AROUND_LINES)
    hi = min(hi, lo + READ_LINES - 1)
    if hi < last:  # the picked lines come first
        lo, hi = first, min(total, first + READ_LINES - 1)
        last = min(last, hi)
    text, hi = numbered(f, lo, hi)
    last = min(last, hi)
    span = f"line {first}" if last == first else f"lines {first}-{last}"
    told = f"The question is about {span} of {f.name}"
    if part:
        told += f", which {'are' if last > first else 'is'} in `{part.full}` ({f.name}:{part.top}-{part.end})"
    told += f". Lines {lo}-{hi} are given, read from the disk."
    step = _step("read", {"file": f.name, "start": first, "end": last},
                 f"The lines asked about: {f.name}, {span}" + (f" ({part.full})" if part else ""),
                 text=text, summary={"file": f.name, "asked_about": [first, last], "given": [lo, hi], "of": total}
                 | ({"part": part.full, "part_lines": [part.top, part.end]} if part else {}),
                 code={"file": f.name, "start": first, "end": last, "lines": f.lines[first - 1: last]},
                 places=[(f.name, lo, hi)] + ([(f.name, part.top, part.top), (f.name, part.end, part.end)] if part else []))
    step["about"] = True
    return step, told


# -- what an answer names, against what was looked at -------------------------------------------------
def _seen(steps: list[dict]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for s in steps:
        for name, first, last in s.get("places") or []:
            out.setdefault(name, set()).update(range(int(first), int(last) + 1))
    return out


def cites(src: Source, answer: str) -> list[dict]:
    """The places an answer names with line numbers: {"file" as written, "name" (the listed file,
    or None), "start", "end"}, each once, in the answer's order."""
    text = _dashes(answer).replace("`", "").replace("*", "")
    found, got = [], set()
    for pattern in CITES:
        for m in pattern.finditer(text):
            a = int(m.group("a"))
            b = int(m.group("b")) if m.group("b") else a
            key = (m.group("file").lower(), min(a, b), max(a, b))
            if key not in got:
                got.add(key)
                found.append((m.start(), {"file": m.group("file"), "name": src.pick(m.group("file")),
                                          "start": min(a, b), "end": max(a, b)}))
    return [c for _, c in sorted(found, key=lambda x: x[0])]


def blocks(src: Source, answer: str) -> list[bool]:
    """Each code block the AI wrote (``` … ```), in order: True when every line of it is a line of
    the files as it stands there, False when it is the AI's own writing (an example, or a guess)."""
    real = {line.strip() for f in src.files() for line in f.lines if line.strip()}
    out = []
    for m in FENCE.finditer(str(answer or "")):
        lines = [x.strip() for x in m.group(1).splitlines() if x.strip()]
        out.append(bool(lines) and all(re.sub(r"^\d{1,5}:\s?", "", x).strip() in real or x in real for x in lines))
    return out


def check(src: Source, answer: str, steps: list[dict], given: list[str]) -> list[dict]:
    """What the answer names that is not in what the AI looked at, each {"text", "kind", "why"}:
    kind "file" (no such source file), "line" (the file is shorter), "unseen" (a line, or a name
    the code has, that was in none of its look-ups), "name" (a name the code does not have),
    "number" (in none of its look-ups)."""
    seen, out, done = _seen(steps), [], set()
    body = FENCE.sub(" ", _dashes(answer))
    known = "\n".join(given)
    said = set(IDENT.findall(known))
    said |= {w.lower() for w in said}  # `HttpOnly` for the code's httponly is the same name
    stems = {n.rsplit("/", 1)[-1].rsplit(".", 1)[0] for n in src.names()}

    def flag(text: str, kind: str, why: str) -> None:
        if (text, kind) not in done:
            done.add((text, kind))
            out.append({"text": text, "kind": kind, "why": why})

    for c in cites(src, body):
        span = f"{c['file']}:{c['start']}" + (f"-{c['end']}" if c["end"] > c["start"] else "")
        f = src.file(c["name"]) if c["name"] else None
        if f is None:
            continue  # the file itself is judged below
        if c["end"] > len(f.lines):
            flag(span, "line", f"{f.name} has {len(f.lines):,} lines")
        elif not {c["start"], c["end"]} <= seen.get(f.name, set()):
            flag(span, "unseen", "not among the lines it looked at")
    for m in FILE_NAME.finditer(body.replace("`", " ")):
        if src.pick(m.group(0)) is None and m.group(0) not in known:
            flag(m.group(0), "file", "not one of this app's source files here")
    names = []
    for chunk in TICKS.findall(body):
        chunk = re.sub(r"\(.*\)$", "", chunk.strip())
        if not chunk or FILE_NAME.fullmatch(chunk.split(":")[0]) or not re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", chunk):
            continue  # a file, an address, a command, a piece of code: not one name
        names += chunk.split(".")
    plain = TICKS.sub(" ", body)
    names += re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*_[\w$]+)(?![\w$])", plain)
    names += re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]{2,})\(\)", plain)
    code = None
    for name in names:
        if (len(name) < 3 or name in STOP or name.lower() in STOP or name in said or name.lower() in said
                or name in stems or name.isdigit()):
            continue
        code = src.words() if code is None else code
        if name in code:
            flag(name, "unseen", "in the code, but in none of its look-ups")
        else:
            flag(name, "name", "not a name in the code")
    for pattern in CITES:  # a line number is judged above, as a place
        plain = pattern.sub(" ", plain)
    for x in sorted(numbers(plain) - allowed(*given, small=10)):
        flag(f"{x:g}", "number", "a number in none of its look-ups")
    return out


def shown(src: Source, answer: str, skip=()) -> list[dict]:
    """The places an answer names, read from the disk for the page: the lines it gives (a single
    line with the two around it), then the functions it names in backticks; each place once.
    `skip`: lines already on the answer's card, (file, first, last): the lines that were asked about."""
    out: list[dict] = []

    def add(f: File, first: int, last: int, part: str = "", mark=None) -> None:
        first, last = max(1, first), min(len(f.lines), last)
        if first > last or len(out) >= SHOWN_MAX:
            return
        if any(name == f.name and a <= (mark or first) and (mark or last) <= b for name, a, b in skip):
            return
        if any(o["file"] == f.name and o["start"] <= last and first <= o["to"] for o in out):
            return  # lines already shown
        cut = min(last, first + SHOWN_LINES - 1)
        out.append({"file": f.name, "start": first, "end": cut, "more": last - cut, "to": last,
                    "part": part or f.inside(first), "mark": mark, "lines": f.lines[first - 1: cut]})

    body = FENCE.sub(" ", _dashes(answer))
    for c in cites(src, body):
        f = src.file(c["name"]) if c["name"] else None
        if f is None or c["start"] > len(f.lines):
            continue
        if c["end"] == c["start"]:
            add(f, c["start"] - 2, c["start"] + 2, mark=c["start"])
        else:
            add(f, c["start"], c["end"])
    files = [src.file(n) for n in dict.fromkeys(src.pick(m.group(0)) for m in FILE_NAME.finditer(body.replace("`", " "))) if n]
    for chunk in TICKS.findall(body):
        name = re.sub(r"\(.*\)$", "", chunk.strip())
        if not re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", name) or len(name) < 3:
            continue
        found = [(f, i) for f in files if f for i in f.named(name) if i.kind in ("function", "method", "class")]
        if not found:
            every = [(f, i) for f in src.files() for i in f.named(name) if i.kind in ("function", "method", "class")]
            found = every if len(every) == 1 else []  # the one of that name, wherever it is
        for f, i in found[:1]:
            add(f, i.top, i.end, i.full)
    return out


# -- the chat ------------------------------------------------------------------------------------------
def system(src: Source) -> str:
    files = src.files()
    listed = "\n".join(f"- {f.name} ({len(f.lines):,} lines): {_one(f.does, 170)}" for f in files)
    return (f"{SYSTEM}\nThe source files on this host ({len(files)} files, {sum(len(f.lines) for f in files):,} "
            f"lines), each with what it does:\n{listed}")


def ask(question: str, history: list, client: AiClient, src: Source | None = None, about: dict | None = None) -> dict:
    """A question about the code. `history`: the page's earlier questions ({"q", "a", "looked"});
    `about`: lines picked on the page ({"file", "start", "end"}) or a part ({"file", "name"})."""
    from .agent import _call, _calls, looked
    src = src or Source()
    started = time.monotonic()
    question = " ".join(str(question or "").split())[:QUESTION_MAX]
    sys_text = system(src)
    turns, said, before = [], [], []
    for h in (history or [])[-HISTORY:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            q, a = str(h["q"])[:QUESTION_MAX], str(h["a"])[:2000]
            turns += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
            said += [q, a]
            if h.get("looked"):  # in the instructions: put after an answer, a model writes it into its own
                before.append(f"- \"{q[:120]}\": {str(h['looked'])[:400]}")
                said.append(str(h["looked"])[:400])
    if before:
        sys_text += "\nWhat you looked up for the questions before in this chat:\n" + "\n".join(before)
    steps: list[dict] = []
    first, told_about = about_step(src, about)
    asked = question
    if first is not None:
        steps.append(first)
        asked = f"{question}\n\n{told_about}\n{first['text']}"
    messages = [{"role": "system", "content": sys_text}, *turns, {"role": "user", "content": asked}]
    note, text, told = None, "", False
    for turn in range(MAX_STEPS + 2):
        reply = client.chat(messages, TOOLS)
        calls = _calls(reply)
        if not calls:
            text = str(reply.get("content") or "").strip()
            break
        if told:  # asked to answer, and still looking
            note = f"The AI was still looking things up after {len(steps)} steps."
            break
        messages.append({"role": "assistant", "content": reply.get("content") or "",
                         "tool_calls": reply.get("tool_calls")})
        for name, args, call_id in calls:
            if len(steps) >= MAX_STEPS:
                content = json.dumps({"notes": [f"No more look-ups ({MAX_STEPS} done): answer with what you have."]})
            else:
                log.info("Code chat: look-up %d, %s", len(steps) + 1, _call(name, args)[:300])
                step = run(src, name, args)
                steps.append(step)
                content = for_ai(step)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        if len(steps) >= MAX_STEPS or turn == MAX_STEPS:
            messages.append({"role": "user", "content": "Answer now with what you have found; no more look-ups."})
            told = True
    text = text[:ANSWER_MAX]
    if not text:
        note = note or "The AI gave no answer."
    given = [asked, sys_text, *said] + [for_ai(s) for s in steps]
    # the lines picked on the page, for the answer's card (a whole file asked about: none)
    picked = first["code"] | {"title": first["title"]} if first is not None and "asked_about" in first["summary"] else None
    return {"question": question, "answer": text, "steps": steps, "looked": looked(steps),
            "flagged": check(src, text, steps, given), "blocks": blocks(src, text), "about": picked,
            "shown": shown(src, text, [(picked["file"], picked["start"], picked["end"])] if picked else ()),
            "note": note, "service": client.cfg.name, "seconds": round(time.monotonic() - started, 1)}
