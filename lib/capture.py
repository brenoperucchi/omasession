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
import os
import re
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
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
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


def allowed_programs() -> set[str]:
    """terminalPrograms config, passed in by `omasession save` as a comma list."""
    raw = os.environ.get("OMASESSION_TERMINAL_PROGRAMS", "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def program_windows(windows: list[dict], allowed: set[str]) -> dict[str, dict]:
    """Which listed program each terminal window was running, by address.

    One terminal process can serve several windows (Ghostty does by default),
    and nothing ties a window to its shell. So: a window whose title names the
    program gets it; then, if exactly one window and one program are left over
    in that process, they belong together. Anything still ambiguous gets no
    program -- it comes back as a plain terminal, as before.
    """
    result: dict[str, dict] = {}
    by_pid: dict[int, list[dict]] = {}
    for c in windows:
        if c.get("pid"):
            by_pid.setdefault(int(c["pid"]), []).append(c)
    for pid, wins in by_pid.items():
        programs = R.terminal_programs(pid, allowed)
        if not programs:
            continue
        left = list(wins)
        names = [p["name"].lower() for p in programs]

        def names_in(title: str) -> set[str]:
            return {n for n in names
                    if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w.-])", title, re.I)}

        for program in list(programs):
            # Two of the same program in one process, or a title naming two
            # programs: the title cannot say which is which, so no match.
            if names.count(program["name"].lower()) > 1:
                continue
            named = [w for w in left if names_in(w.get("title", "")) == {program["name"].lower()}]
            if len(named) == 1:
                result[named[0].get("address", "")] = program
                left.remove(named[0])
                programs.remove(program)
        if len(left) == 1 and len(programs) == 1:
            result[left[0].get("address", "")] = programs[0]
    return result


def capture(name: str = "last", clients: object | None = None) -> str:
    if clients is None:
        clients = hyprctl_json("clients")
    monitors = hyprctl_json("monitors")
    index = R.desktop_index()

    windows = [
        c for c in clients
        if c.get("mapped") and c.get("workspace", {}).get("id", 0) > 0
    ]

    doc = [f'[session]\nname = {toml_str(name)}\ntimestamp = {int(time.time())}\n']
    programs = program_windows(windows, allowed_programs())

    for c in windows:
        klass = c.get("class", "")
        result = R.resolve({**c, "initialClass": c.get("initialClass") or klass}, index)
        program = programs.get(c.get("address", ""))
        if program and result.get("argv"):
            argv = R.with_program(result["argv"], program)
            if argv:
                result["argv"] = argv
                result["cwd"] = program.get("cwd") or result.get("cwd")
        cmd = R.command(result)

        fields = {
            "app_id": klass,
            "title": c.get("title", ""),
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
    # session-save.sh reads `hyprctl clients` once, to decide the guard, and
    # pipes that exact reading in here -- otherwise this does its own second
    # read at a different instant, and the guard ends up comparing two
    # different screens (window opened/closed between the two reads) instead
    # of the one it was built to protect. Found in review, round 4: nothing
    # broke visibly, the invariant the comment above the guard already
    # describes just stopped being true the moment the reader became us
    # instead of hyprresume.
    #
    # `isatty()` alone is not "nobody piped anything in": found in review,
    # round 5 -- a systemd unit, cron, or any `subprocess` call that does not
    # set `stdin=` gets /dev/null, which is not a tty either, and reading it
    # as JSON crashed with a traceback instead of falling back to fetching
    # fresh. Reading whatever is actually there and only trusting it if it
    # parses covers both the piped case and the closed/empty one; standalone
    # manual runs (`python3 capture.py name`) still fetch fresh either way.
    #
    # Non-empty but not valid JSON is the last way this can fail: found in
    # review, round 6 -- `echo nope | capture.py` still raised. Only
    # session-save.sh pipes anything in today, and always valid JSON, so this
    # cannot fire yet -- falling back instead of trusting garbage is what
    # keeps it that way if that ever stops being true.
    piped = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    try:
        piped_clients = json.loads(piped) if piped else None
    except json.JSONDecodeError:
        piped_clients = None
    sys.stdout.write(capture(name, clients=piped_clients))
