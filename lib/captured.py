#!/usr/bin/env python3
"""Turn the title sidecar into what the panel needs to show.

Adds two things the raw sidecar does not carry:

  app          a name a person recognises. "md.obsidian.Obsidian" is precise and
               unreadable; the class stays available for anyone who wants it.
  resolvable   whether this window can actually be brought back.

`resolvable` is deliberately answered WITHOUT a live process. The saved session
describes windows whose applications are already gone, so anything that needs a
pid can only be guessed at; the .desktop index answers it for real, offline, and
is the same index the restore will use. A panel that says "Ready" for a window
it cannot reopen is the failure this project exists to avoid.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as R  # noqa: E402

# Names worth spelling out. Everything else is derived from the class, which is
# better than nothing and never claims more than it knows.
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


def main() -> int:
    path = Path(sys.argv[1])
    try:
        windows = json.loads(path.read_text()).get("windows", [])
    except (OSError, json.JSONDecodeError):
        json.dump([], sys.stdout)
        return 0

    index = R.desktop_index()
    out = []
    for w in windows:
        klass = w.get("class", "")
        out.append({
            "ws": w.get("workspace"),
            "cls": klass,
            "app": friendly(klass),
            "title": w.get("title", ""),
            "resolvable": klass.lower() in index,
        })
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
