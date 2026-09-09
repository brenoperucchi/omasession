#!/usr/bin/env python3
"""Resolve a mapped window back to a command that reopens it.

This is the piece the whitelist approach cannot reach. `dimef.omaresume` keys
its launcher off eight known classes and returns nothing for anything else;
hyprresume does resolve arbitrary apps, but it has been unmaintained since March
2026 and is the last dependency this plugin has. Owning the resolver is the same
work as removing that dependency (docs/plans/003).

Four paths, tried in this order, because the cheap one is also the most
misleading:

  .desktop   the class is matched against StartupWMClass and against the entry
             id, and the Exec= line -- minus its field codes -- is the command.
             This is what brings back nautilus and obsidian with no user
             configuration at all.
  cgroup     systemd names the scope after the launcher, so a Flatpak reads as
             app-flatpak-<appid>-<pid>.scope. Needed because /proc/<pid>/cmdline
             for a Flatpak shows bwrap, not the application.
  cmdline    last resort. It is right for a plain binary and wrong in the two
             cases that matter most: a browser's line contains none of its URLs,
             and an Electron app shows the packaged binary's private path.
  (none)     saying so is a result. A window we cannot resolve must be reported,
             not silently dropped -- that is how a restore reports success it
             never earned.

Standard library only: this runs at login, before anything is guaranteed to be
installed.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

# Exec= field codes, to strip. %% is an escaped percent and is left alone here
# because it is handled before the split.
FIELD_CODES = re.compile(r'(?<!%)%[uUfFickvmdDnNs]')

# Terminals take their working directory as an argument. The class is what
# Hyprland reports; the flag is what the emulator accepts.
TERMINAL_CWD_FLAG = {
    "foot": "--working-directory",
    "Alacritty": "--working-directory",
    "kitty": "--directory",
    "com.mitchellh.ghostty": "--working-directory",
    "org.wezfurlong.wezterm": "--cwd",
}

# Applications that own every one of their windows from a single process. There
# is no point launching one per saved window: the second invocation talks to the
# first and exits. The replay asks the app to restore its own windows instead.
SINGLE_INSTANCE = {
    "chromium", "google-chrome", "brave-browser", "vivaldi-stable",
    "microsoft-edge", "firefox", "code", "org.gnome.Nautilus",
}


def xdg(name: str, fallback: str) -> str:
    """An XDG variable, treating empty as unset -- which the spec requires.

    `os.environ.get(name, fallback)` returns "" for a variable that is set but
    empty, and "".split(":") yields nothing usable. Measured: with an empty
    XDG_DATA_DIRS the index drops from 139 entries to 32. That matters more than
    it looks, because the resolver runs at login, which is exactly the context
    where a minimal environment shows up.
    """
    value = os.environ.get(name, "")
    return value if value.strip() else fallback


def data_dirs() -> list[Path]:
    """XDG application directories, most specific first."""
    home = Path(xdg("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    system = xdg("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs = [home] + [Path(p) for p in system.split(":") if p]
    # Flatpak exports live outside XDG_DATA_DIRS on some setups.
    dirs += [Path.home() / ".local/share/flatpak/exports/share",
             Path("/var/lib/flatpak/exports/share")]
    return [d / "applications" for d in dirs]


def parse_desktop(path: Path) -> dict | None:
    """The [Desktop Entry] fields we care about, or None if it is not usable."""
    entry, in_section = {}, False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            # Only the main section. Actions carry their own Exec= lines and
            # picking one of those up launches "New Window" instead of the app.
            in_section = line == "[Desktop Entry]"
            continue
        if not in_section or "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        entry[key.strip()] = value.strip()
    if not entry.get("Exec"):
        return None
    if entry.get("Hidden", "").lower() == "true":
        return None
    if entry.get("Type", "Application") != "Application":
        return None
    return entry


def desktop_index() -> dict[str, dict]:
    """Map lowercased class names to desktop entries.

    Two keys per entry, because neither alone is reliable: StartupWMClass is
    authoritative when present but most entries omit it, and the entry id
    matches the class for the majority that do.
    """
    index: dict[str, dict] = {}
    for directory in data_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            entry = parse_desktop(path)
            if entry is None:
                continue
            entry["_path"] = str(path)
            keys = [path.stem]
            if entry.get("StartupWMClass"):
                keys.append(entry["StartupWMClass"])
            for key in keys:
                # setdefault: data_dirs() is most-specific-first, so a user
                # override in ~/.local/share wins over /usr/share.
                index.setdefault(key.lower(), entry)
    return index


def exec_argv(exec_line: str) -> list[str]:
    """The Exec= line as argv, with field codes removed.

    A leftover %U turns into a literal argument the application then tries to
    open as a file, which is why this cannot just be shlex.split.
    """
    cleaned = FIELD_CODES.sub("", exec_line).replace("%%", "%")
    try:
        argv = shlex.split(cleaned)
    except ValueError:
        argv = cleaned.split()
    # env VAR=1 prog ... -- keep it; it is part of how the entry expects to run.
    return [a for a in argv if a]


def from_cgroup(pid: int) -> list[str] | None:
    """Flatpak app id from the systemd scope, when there is one.

    /proc/<pid>/cmdline for a Flatpak shows bwrap and its sandbox arguments;
    the scope name is the only place the application id survives.
    """
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    match = re.search(r"app-flatpak-([A-Za-z0-9_.\-]+?)-\d+\.scope", text)
    if match:
        return ["flatpak", "run", match.group(1)]
    return None


def from_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if not argv:
        return None
    return argv


# Processes whose cwd IS the answer, and processes whose cwd merely looks like
# one. A multiplexer client runs wherever it was launched; the shell the user is
# actually in belongs to the server, in a different process tree entirely.
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh",
          "nu", "elvish", "xonsh", "ion"}
MULTIPLEXERS = {"tmux", "tmux: client", "screen", "abduco", "dtach", "zellij"}


def _comm(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _children(pid) -> list[str]:
    try:
        return Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return []


def child_cwd(pid: int) -> tuple[str | None, str]:
    """The directory a terminal's shell is in, and why, or (None, reason).

    /proc/<pid>/cwd is the emulator's own -- wherever it was started, almost
    never what the user sees -- so the answer is in a descendant. But taking the
    first child is wrong in a way that is invisible: on this machine every kitty
    window's first children are `kitten` and `tmux: client`, all reporting the
    home directory, while the shells the user is actually in live inside the
    tmux server, in another tree. Restoring those windows to $HOME would look
    like a success.

    So: search descendants for something that is actually a shell, and if all we
    find is a multiplexer client, say the cwd is unavailable. An honest "not
    recovered" beats a confident wrong directory.
    """
    seen, queue, saw_mux = set(), [(str(pid), 0)], False
    while queue:
        current, depth = queue.pop(0)
        if current in seen or depth > 3:
            continue
        seen.add(current)
        for kid in _children(current):
            comm = _comm(kid)
            if comm in MULTIPLEXERS:
                saw_mux = True
                continue
            if comm in SHELLS:
                try:
                    return os.readlink(f"/proc/{kid}/cwd"), f"shell: {comm}"
                except OSError:
                    pass
            queue.append((kid, depth + 1))
    if saw_mux:
        return None, "cwd lives in the multiplexer server, not in this tree"
    return None, "no shell found under the terminal"


def window_class(window: dict) -> str:
    return window.get("initialClass") or window.get("class") or ""


def resolve(window: dict, index: dict[str, dict] | None = None) -> dict:
    """Resolve one window. Always returns a dict; `argv` is None on failure.

    `via` is part of the result on purpose. A resolver that is wrong and a
    resolver that is right look identical from the outside until the app fails
    to open, so the path taken has to be inspectable (`omasession resolve`).
    """
    if index is None:
        index = desktop_index()

    klass = window_class(window)
    pid = int(window.get("pid") or 0)
    result = {"class": klass, "pid": pid, "argv": None, "cwd": None,
              "via": "unresolved", "single_instance": klass in SINGLE_INSTANCE,
              "note": ""}

    entry = index.get(klass.lower())
    if entry:
        result["argv"] = exec_argv(entry["Exec"])
        result["via"] = ".desktop"
        result["note"] = entry["_path"]

    if result["argv"] is None and pid:
        argv = from_cgroup(pid)
        if argv:
            result["argv"] = argv
            result["via"] = "cgroup"
            result["note"] = "flatpak"

    if result["argv"] is None and pid:
        argv = from_cmdline(pid)
        if argv:
            result["argv"] = argv
            result["via"] = "cmdline"
            result["note"] = "last resort; may not carry the app's own state"

    if klass in TERMINAL_CWD_FLAG and pid:
        cwd, why = child_cwd(pid)
        result["cwd_note"] = why
        if cwd:
            result["cwd"] = cwd
            flag = TERMINAL_CWD_FLAG[klass]
            if result["argv"] and not any(a.startswith(flag) for a in result["argv"]):
                result["argv"] = result["argv"] + [f"{flag}={cwd}"]

    return result


def command(result: dict) -> str | None:
    """The resolved argv as one shell-safe string, for exec_cmd."""
    if not result.get("argv"):
        return None
    return " ".join(shlex.quote(a) for a in result["argv"])
