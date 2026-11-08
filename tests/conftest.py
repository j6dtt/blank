"""Snapshots: with SNAPSHOT_DIR set, every investigation a test runs is saved
there (the text report and the page data), one file per test, so that two runs,
before and after a change, can be compared line by line (scripts/snapshot.sh).
Without SNAPSHOT_DIR nothing changes."""
import json
import os
import re

import uxcorrelate.correlate as correlate
from uxcorrelate.report import render_text

OUT = os.environ.get("SNAPSHOT_DIR")

if OUT:
    _real = correlate.investigate
    _seen: dict[str, int] = {}

    def investigate(*a, **k):
        result = _real(*a, **k)
        test = re.sub(r"\W+", "_", os.environ.get("PYTEST_CURRENT_TEST", "outside").split(" ")[0])
        _seen[test] = _seen.get(test, 0) + 1
        with open(os.path.join(OUT, f"{test}-{_seen[test]}.txt"), "w") as fh:
            fh.write(render_text(result) + "\n\n----- page data -----\n")
            fh.write(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
        return result

    correlate.investigate = investigate  # before the test modules import it
