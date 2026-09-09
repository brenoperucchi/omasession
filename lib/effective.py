#!/usr/bin/env python3
"""Which session the restore would actually use, and from where.

`status` used to describe whatever was in `last.toml`. But the restore does not
always use that: a torn or broken pair sends it to the previous generation, so
the panel could show one session while the button restored another. Asking the
same code the restore asks is the only way the two agree.
"""

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("replay", _here / "replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

toml = Path(sys.argv[1])
notes: list[str] = []


class _Tee:
    """load_pair explains itself on stdout; keep the words, free the channel."""
    def write(self, text):
        text = text.strip()
        if text:
            notes.append(text)
    def flush(self):
        pass


with contextlib.redirect_stdout(_Tee()):
    windows, titles, sidecar = replay.load_pair(toml)

json.dump({
    "windows": len(windows),
    "titles": len(titles),
    "source": str(replay.sidecar_for(toml).parent / sidecar.name).replace(
        str(sidecar.parent) + "/", ""),
    "sidecar": str(sidecar),
    "fallback": sidecar.name != replay.sidecar_for(toml).name,
    "notes": notes,
}, sys.stdout)
