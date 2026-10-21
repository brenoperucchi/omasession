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


def layout_slot_order() -> dict[str, list[str]]:
    """Window addresses per workspace, in the order the layout slots them.

    Hyprland does not report a workspace's tiling order, and its client list
    keeps creation order even after windows are swapped. A Lua config can fill
    the gap by defining a global `window_slot_order()` that returns JSON of
    the form {"<workspace id>": ["0x<address>", ...]} -- a Lua layout sees that
    order in ctx.targets on every recalculate. Without it, {} (callers fall
    back to the client list order).
    """
    try:
        out = subprocess.run(
            ["hyprctl", "repl",
             'return type(window_slot_order) == "function" and window_slot_order() or ""'],
            capture_output=True, text=True, timeout=15,
        )
        order = json.loads(out.stdout.strip() or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    if not isinstance(order, dict):
        return {}
    return {str(ws): [a for a in addrs if isinstance(a, str)]
            for ws, addrs in order.items() if isinstance(addrs, list)}


def slot_orders(windows: list[dict], layout_order: dict[str, list[str]]) -> dict[str, int]:
    """Each window's position among its workspace's windows, by address.

    The layout's own order where it reports one; otherwise the client list
    order, which is right until windows are swapped.
    """
    result: dict[str, int] = {}
    by_workspace: dict[str, list[str]] = {}
    for c in windows:
        by_workspace.setdefault(str(c["workspace"]["id"]), []).append(c.get("address", ""))
    for ws, addrs in by_workspace.items():
        ranked = [a for a in layout_order.get(ws, []) if a in addrs]
        ranked += [a for a in addrs if a not in ranked]
        for i, addr in enumerate(ranked):
            result[addr] = i
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
    orders = slot_orders(windows, layout_slot_order())

    for c in windows:
        klass = c.get("class", "")
        result = R.resolve({**c, "initialClass": c.get("initialClass") or klass}, index)
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
            "order": orders.get(c.get("address", "")),
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
