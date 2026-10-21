#!/usr/bin/env python3
"""A saved session as drawable geometry, for the panel's layout preview.

    layout.py <session-dir> <name>

Prints JSON: the monitors as rectangles in Hyprland's layout space, already
in logical pixels and turned for rotation, and every saved window with its
workspace, slot, rectangle and a short label. Windows on a workspace that is
not on screen keep the coordinates of the monitor they belong to, so any
workspace can be drawn on its monitor. Sessions saved before monitor geometry
was recorded get a monitor box that just encloses their windows.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import captured  # noqa: E402


def monitor_box(mon: dict) -> dict | None:
    """Logical rectangle of a monitor; None without geometry."""
    try:
        w, h = float(mon["width"]), float(mon["height"])
        scale = float(mon.get("scale") or 1) or 1
        x, y = float(mon["x"]), float(mon["y"])
    except (KeyError, TypeError, ValueError):
        return None
    # Transforms 1, 3, 5 and 7 are quarter turns: the screen stands on its side.
    if int(mon.get("transform") or 0) % 2 == 1:
        w, h = h, w
    return {"x": x, "y": y, "w": w / scale, "h": h / scale}


def label(window: dict) -> str:
    """What a person would call the window: its app (or a web app's name), plus
    the program a terminal runs ("Ghostty · btop")."""
    name = captured.friendly(window.get("app_id", ""))
    cmd = window.get("launch_cmd") or ""
    for flag in (" -e ", " -- "):
        if flag in cmd:
            program = cmd.split(flag, 1)[1].split()[0].strip("'\"")
            return f"{name} · {Path(program).name}"
    return name


def load(session_dir: Path, name: str) -> dict:
    toml = session_dir / f"{name}.toml"
    sidecar = session_dir / f"{name}.titles.json"
    windows = tomllib.loads(captured._read_capped(toml)).get("window", [])
    try:
        side = json.loads(captured._read_capped(sidecar))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        side = {}

    monitors = []
    for mon in side.get("monitors", []) if isinstance(side, dict) else []:
        box = monitor_box(mon)
        monitors.append({"name": mon.get("name"), "description": mon.get("description"),
                         "activeWorkspace": mon.get("activeWorkspace"),
                         "transform": int(mon.get("transform") or 0), "box": box})

    out_windows = []
    for w in windows:
        pos, size = w.get("position") or [0, 0], w.get("size") or [0, 0]
        out_windows.append({
            "label": label(w), "title": w.get("title", ""), "app": w.get("app_id", ""),
            "workspace": str(w.get("workspace", "")), "monitor": w.get("monitor"),
            "order": w.get("order"), "floating": bool(w.get("floating")),
            "box": {"x": pos[0], "y": pos[1], "w": size[0], "h": size[1]},
        })

    # Older saves: no geometry. One box around each monitor's windows.
    for mon in monitors:
        if mon["box"] is None:
            mine = [w["box"] for w in out_windows if w["monitor"] == mon["name"]] or \
                   [w["box"] for w in out_windows]
            if mine:
                x0 = min(b["x"] for b in mine); y0 = min(b["y"] for b in mine)
                x1 = max(b["x"] + b["w"] for b in mine); y1 = max(b["y"] + b["h"] for b in mine)
                mon["box"] = {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
    # A monitor nothing places (an older, empty save) cannot be drawn.
    monitors = [m for m in monitors if m["box"] is not None]
    if not monitors and out_windows:
        boxes = [w["box"] for w in out_windows]
        x0 = min(b["x"] for b in boxes); y0 = min(b["y"] for b in boxes)
        monitors = [{"name": "screen", "description": "", "activeWorkspace": None, "transform": 0,
                     "box": {"x": x0, "y": y0,
                             "w": max(b["x"] + b["w"] for b in boxes) - x0,
                             "h": max(b["y"] + b["h"] for b in boxes) - y0}}]

    workspaces = sorted({w["workspace"] for w in out_windows}, key=lambda s: (len(s), s))
    return {"name": name, "monitors": monitors, "workspaces": workspaces, "windows": out_windows}


def main() -> int:
    if len(sys.argv) != 3 or "/" in sys.argv[2] or sys.argv[2].startswith("."):
        print("usage: layout.py <session-dir> <name>", file=sys.stderr)
        return 2
    try:
        print(json.dumps(load(Path(sys.argv[1]), sys.argv[2])))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
