#!/usr/bin/env python3
"""Remove images from saved Claude Code conversations.

Each image becomes the text "[image removed]"; everything else is kept,
so a session still resumes. The session's folder in ~/.claude/uploads
is deleted too. Close the session first: a file changed in
the last 5 minutes is skipped (it may still be in use).

  strip-images.py              every conversation of every project
  strip-images.py FILE.jsonl   only these files
"""
import glob
import json
import os
import shutil
import sys
import time

RECENT_S = 300


def strip(o, n):
    if isinstance(o, dict):
        if o.get("type") == "image":
            n[0] += 1
            return {"type": "text", "text": "[image removed]"}
        return {k: strip(v, n) for k, v in o.items()}
    if isinstance(o, list):
        return [strip(v, n) for v in o]
    return o


def clean(path):
    if time.time() - os.path.getmtime(path) < RECENT_S:
        print(f"Skipped {path}: changed in the last 5 minutes, close that session first.")
        return
    n, out = [0], []
    with open(path) as fh:
        for line in fh:
            try:
                out.append(json.dumps(strip(json.loads(line), n)) + "\n")
            except ValueError:
                out.append(line)  # not JSON: kept as it is
    if n[0]:
        before = os.path.getsize(path)
        tmp = path + ".new"
        with open(tmp, "w") as fh:
            fh.writelines(out)
        os.replace(tmp, path)
        print(f"Removed {n[0]} images from {path}: {before / 1e6:.0f} MB -> {os.path.getsize(path) / 1e6:.0f} MB")
    # Claude Code also keeps each attached image as a file, per session
    uploads = os.path.expanduser(f"~/.claude/uploads/{os.path.basename(path)[:-len('.jsonl')]}")
    if os.path.isdir(uploads):
        shutil.rmtree(uploads)
        print(f"Removed {uploads}")


files = sys.argv[1:] or glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
for f in files:
    clean(f)
print("Done.")
