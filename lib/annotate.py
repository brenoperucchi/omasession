#!/usr/bin/env python3
"""Add to each saved window what only a live process can tell us.

Two facts die with the session and cannot be recovered afterwards: the real
working directory of a terminal, and the reason we could not get it. Recording
them at save time is what lets the panel promise only what it can deliver --
"~/Devs/api" for a window that will come back there, and "directory not
recoverable" for one that will not, instead of a uniform "Ready" that is a
guess in both cases.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as R  # noqa: E402


def main() -> int:
    doc = json.loads(Path(sys.argv[1]).read_text())
    for w in doc.get("windows", []):
        klass = w.get("class", "")
        if klass not in R.TERMINAL_CWD_FLAG:
            continue
        pid = int(w.get("pid") or 0)
        if not pid:
            continue
        cwd, why = R.child_cwd(pid)
        w["cwd"] = cwd
        w["cwdNote"] = why
    json.dump(doc, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
