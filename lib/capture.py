#!/usr/bin/env python3
"""Capture the session ourselves: hyprctl for state, our own resolver for the
one field hyprresume got wrong.

This replaces `hyprresume save`. DESIGN.md §2 kept hyprresume only for two
things: it resolves a window to a command through .desktop/cgroup/proc, and it
walks a terminal's process tree for the real cwd. Both are now ours --
lib/resolve.py does the first with a wider net than hyprresume's (it also
answers when a class has no .desktop at all, which hyprresume falls back to
resolving from the process's own binary and silently drops every argument:
measured 2026-09-09, `hyprresume resolve TUI.tile` -> `foot`, losing the
`--app-id=TUI.tile herdr` that made the window anything other than a plain
terminal). resolve.child_cwd() already does the second, and refuses to guess
when it would be wrong instead of confidently returning the multiplexer's
launch directory.

Emits the same TOML shape hyprresume wrote -- [session] then one [[window]]
per captured window -- so replay.py, test/fixtures/last.toml and every
existing test stay valid without change. Nothing here writes to disk; the
caller decides where the result goes, same as before.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as R  # noqa: E402


def hyprctl_json(*args) -> object:
    out = subprocess.run(["hyprctl", *args, "-j"], capture_output=True, text=True, timeout=15)
    return json.loads(out.stdout or "[]")


def toml_str(s: str) -> str:
    """A TOML basic string literal. Matches what tomllib round-trips."""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def window_toml(fields: dict) -> str:
    lines = ["[[window]]"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, str):
            lines.append(f"{key} = {toml_str(value)}")
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            lines.append(f"{key} = [\n    {value[0]},\n    {value[1]},\n]")
        else:
            raise ValueError(f"unsupported TOML value for {key!r}: {value!r}")
    return "\n".join(lines)


def capture(name: str = "last") -> str:
    clients = hyprctl_json("clients")
    monitors = hyprctl_json("monitors")
    index = R.desktop_index()

    windows = [
        c for c in clients
        if c.get("mapped") and c.get("workspace", {}).get("id", 0) > 0
    ]

    doc = [f'[session]\nname = "{name}"\ntimestamp = {int(time.time())}\n']

    for c in windows:
        klass = c.get("class", "")
        result = R.resolve({**c, "initialClass": c.get("initialClass") or klass}, index)
        cmd = R.command(result)

        fields = {
            "app_id": klass,
            "launch_cmd": cmd,
            "workspace": str(c["workspace"]["id"]),
            "monitor": next((m["name"] for m in monitors if m.get("id") == c.get("monitor")), None),
            "floating": bool(c.get("floating")),
            "fullscreen": bool(c.get("fullscreen")),
            "position": c.get("at"),
            "size": c.get("size"),
            "cwd": result.get("cwd"),
        }
        doc.append(window_toml(fields))

    return "\n\n".join(doc) + "\n"


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "last"
    sys.stdout.write(capture(name))
