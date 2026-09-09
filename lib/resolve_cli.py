#!/usr/bin/env python3
"""Resolve every mapped window on stdin (`hyprctl clients -j`) to JSON."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as R  # noqa: E402

windows = json.load(sys.stdin)
index = R.desktop_index()
out = [R.resolve(w, index) for w in windows
       if w.get("mapped") and w.get("workspace", {}).get("id", 0) > 0]
for r in out:
    r["command"] = R.command(r)
json.dump(out, sys.stdout, indent=1)
