#!/usr/bin/env python3
"""Turn the title sidecar into what the panel needs to show.

Adds two things the raw sidecar does not carry:

  app          a name a person recognises. "md.obsidian.Obsidian" is precise and
               unreadable; the class stays available for anyone who wants it.
  resolvable   whether this window can actually be brought back.

`resolvable` is answered from the toml the restore will actually read: a window
comes back if it carries a launch_cmd, because that is what replay.py relaunches
from. An earlier version answered from the .desktop index, which is what the
resolver would use if the resolver were driving the restore -- it is not, so the
panel was promising in the name of something else. A panel that says a window
will come back when it will not is the failure this project exists to avoid.
"""

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as R  # noqa: E402

# Names worth spelling out. Everything else is derived from the class, which is
# better than nothing and never claims more than it knows.
# O replay deixa estes restaurarem as próprias janelas e abas (DESIGN.md §5),
# então a linha de detalhe pode dizer isso sem inventar nada.
BROWSER_CLASSES = {"chromium", "google-chrome", "brave-browser",
                   "vivaldi-stable", "firefox", "microsoft-edge"}

FRIENDLY = {
    "foot": "Foot", "Alacritty": "Alacritty", "kitty": "Kitty",
    "com.mitchellh.ghostty": "Ghostty",
    "chromium": "Chromium", "google-chrome": "Google Chrome",
    "brave-browser": "Brave", "firefox": "Firefox", "vivaldi-stable": "Vivaldi",
    "org.gnome.Nautilus": "Files", "md.obsidian.Obsidian": "Obsidian",
    "code": "VS Code", "org.telegram.desktop": "Telegram",
}


def friendly(klass: str) -> str:
    if klass in FRIENDLY:
        return FRIENDLY[klass]
    tail = klass.split(".")[-1].replace("-", " ").replace("_", " ")
    return " ".join(w[:1].upper() + w[1:] for w in tail.split() if w) or klass


def resolvable_map(toml_path: Path) -> dict[tuple[str, object], int]:
    """How many windows of each (class, workspace) the replay can relaunch.

    The promise has to come from what the RESTORE uses, not from what this
    module happens to know. replay.py relaunches from `launch_cmd` in the toml
    and skips a window without one ("no launch_cmd, skipped"); the .desktop
    index is what the resolver would offer if it were driving, and it is not.
    Answering from the index made the panel promise in the name of something
    that does not do the restoring -- a window marked "Ready" that never comes
    back is worse than promising nothing.
    """
    counts: dict[tuple[str, object], int] = {}
    try:
        doc = tomllib.loads(toml_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return counts
    for w in doc.get("window", []):
        if not w.get("launch_cmd"):
            continue
        key = (w.get("app_id", ""), str(w.get("workspace", "")))
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> int:
    path = Path(sys.argv[1])
    toml_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        windows = json.loads(path.read_text()).get("windows", [])
    except (OSError, json.JSONDecodeError):
        json.dump([], sys.stdout)
        return 0

    # Sem o toml não há como saber o que o restore fará, e inventar um "Ready"
    # seria o defeito que este arquivo existe para evitar.
    counts = resolvable_map(toml_path) if toml_path else {}
    remaining = dict(counts)

    out = []
    for w in windows:
        klass = w.get("class", "")
        key = (klass, str(w.get("workspace", "")))
        left = remaining.get(key, 0)
        resolvable = left > 0
        if resolvable:
            remaining[key] = left - 1

        detail = w.get("cwd") or ""
        if not detail and klass in BROWSER_CLASSES:
            detail = "tabs restored by the browser"

        out.append({
            "ws": w.get("workspace"),
            "mon": w.get("monitorName") or ("Monitor %s" % w.get("monitor", "?")),
            "cls": klass,
            "app": friendly(klass),
            "title": w.get("title", ""),
            "resolvable": resolvable,
            "detail": detail,
            "warn": ("" if w.get("cwd") or not w.get("cwdNote")
                     else "directory not recoverable"),
        })
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
