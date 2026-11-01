"""Keep a log of every restore that really ran, with what explains it.

`last-restore.log` used to be the only trace, and every restore overwrote it:
the login restore of 2026-10-31 came back with empty Chrome windows and bare
terminals, and a manual restore sixteen minutes later had already erased what
the login one did. The lab cannot stand in for this -- it runs on clean
profiles and shuts down its own way -- so the evidence has to come from the
machine where it went wrong.

Each restore now writes `restores/<stamp>-<trigger>.log` next to a copy of the
session it replayed, and the newest KEEP of them stay. `last-restore.log` is
still written, as the copy of the latest one. Nothing here may fail a restore:
a log that cannot be written is reported once on stderr and dropped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from safe_fs import Refused, TooLarge, open_dir_chain, read_capped_path, write_bytes

KEEP = 20
DIR = "restores"
LAST = "last-restore.log"
TRIGGERS = ("login", "manual", "app")
ENTRY = re.compile(r"^(\d{8}-\d{6}-[a-z]+(?:-\d+)?)\.(log|session\.toml|titles\.json)$")
JOURNAL_TIMEOUT = 5
# How far back from the previous boot's last entry to look for the shutdown.
# Bounding the search keeps it to milliseconds: an unbounded pattern search
# that finds nothing scans the whole boot (8.5 s measured on an 11-day boot),
# and the login restore still holds the session lock while this runs.
SHUTDOWN_WINDOW = 600
TIMED_OUT = object()
ROOT = Path(__file__).resolve().parents[1]


def trigger_from_env() -> str:
    trigger = os.environ.get("OMASESSION_RESTORE_TRIGGER", "manual")
    return trigger if trigger in TRIGGERS else "manual"


def free_stem(dir_fd: int, now: float, trigger: str) -> str:
    """A name no earlier restore holds; two restores in one second both stay.

    The name only has to be unique; what came first is decided by when each
    log was written (see newest_first), since a `manual` and an `app` restore
    in the same second would sort by their trigger, not by their order.
    """
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{trigger}"
    # After the highest stem already taken, never in a gap pruning left.
    taken = 0
    for name in os.listdir(dir_fd):
        m = ENTRY.match(name)
        if not m or not m.group(1).startswith(base):
            continue
        rest = m.group(1)[len(base):]
        if rest == "":
            taken = max(taken, 1)
        elif rest[1:].isdigit() and rest[0] == "-":
            taken = max(taken, int(rest[1:]))
    return base if taken == 0 else f"{base}-{taken + 1:02d}"


def by_age(dir_fd: int) -> list[tuple[str, list[str]]]:
    """Restores oldest first, each with its files.

    Ordered by when the log was last written, not by name. Restores never
    overlap (they hold the session lock), so the later one is always written
    last; a stem without its log sorts as the oldest.
    """
    groups: dict[str, list[str]] = {}
    for name in os.listdir(dir_fd):
        m = ENTRY.match(name)
        if m:
            groups.setdefault(m.group(1), []).append(name)

    def written(stem: str) -> tuple[int, str]:
        try:
            return os.stat(f"{stem}.log", dir_fd=dir_fd, follow_symlinks=False).st_mtime_ns, stem
        except OSError:
            return 0, stem

    return [(stem, groups[stem]) for stem in sorted(groups, key=written)]


def prune(dir_fd: int, keep: int = KEEP) -> None:
    """Drop all but the newest `keep` restores, each with its session copy."""
    aged = by_age(dir_fd)
    for _stem, names in aged[:-keep] if keep > 0 else aged:
        for name in names:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                # A cleanup that cannot finish costs nothing but disk.
                pass


class RestoreLog:
    """Publishes the restore's output to its own file and to last-restore.log."""

    def __init__(self, state_dir: Path, trigger: str, now: float | None = None,
                 keep: int = KEEP) -> None:
        self.trigger = trigger
        self.state_fd: int | None = None
        self.dir_fd: int | None = None
        self.stem: str | None = None
        self.failed = False
        self.keep = keep
        self.pruned = False
        try:
            self.state_fd = open_dir_chain(str(state_dir), create=True)
            self.dir_fd = open_dir_chain(str(state_dir / DIR), create=True)
            self.stem = free_stem(self.dir_fd, time.time() if now is None else now, trigger)
        except (OSError, Refused, TooLarge) as exc:
            self._disable(f"restore log unavailable: {exc}")

    @property
    def name(self) -> str | None:
        return f"{DIR}/{self.stem}.log" if self.stem else None

    def _disable(self, why: str) -> None:
        if not self.failed:
            sys.__stderr__.write(f"[omasession] {why}\n")
            sys.__stderr__.flush()
        self.failed = True
        self.close()

    def publish(self, text: str) -> None:
        if self.failed or self.state_fd is None:
            return
        data = text.encode()
        try:
            if self.dir_fd is not None and self.stem:
                write_bytes(self.dir_fd, f"{self.stem}.log", data, mode=0o600)
                if not self.pruned:
                    # After the first write, so the new log counts as one kept.
                    # Outside what disables the log: a failed cleanup must not
                    # cost the restore its log.
                    self.pruned = True
                    try:
                        prune(self.dir_fd, self.keep)
                    except OSError as exc:
                        sys.__stderr__.write(f"[omasession] restore log cleanup skipped: {exc}\n")
            write_bytes(self.state_fd, LAST, data, mode=0o600)
        except (OSError, Refused, TooLarge) as exc:
            self._disable(f"restore log disabled: {exc}")

    def keep_session(self, toml: Path, sidecar: Path | None) -> None:
        """Copy the session this restore replayed, so the log can be read
        against what it was asked to do even after `last` moves on."""
        if self.failed or self.dir_fd is None or not self.stem:
            return
        for src, suffix in ((toml, "session.toml"), (sidecar, "titles.json")):
            if src is None:
                continue
            try:
                write_bytes(self.dir_fd, f"{self.stem}.{suffix}",
                            read_capped_path(src), mode=0o600)
            except (OSError, Refused, TooLarge) as exc:
                print(f"  ! could not keep a copy of {src.name}: {exc}")

    def close(self) -> None:
        for attr in ("dir_fd", "state_fd"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)


def _first_journal_line(args: list[str]):
    """The newest matching journal line: "" if nothing matches, None if the
    journal cannot be read, TIMED_OUT if it did not answer in time."""
    try:
        out = subprocess.run(["journalctl", "--no-pager", "-q", "-r", "-n", "1", *args],
                             capture_output=True, text=True, timeout=JOURNAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return TIMED_OUT
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.strip().splitlines()
    if out.returncode == 0:
        return lines[0] if lines else ""
    # `journalctl -g` exits 1 when nothing matches; anything else, or 1 with
    # an error and no output, is a journal we could not read.
    if out.returncode == 1 and not lines and not out.stderr.strip():
        return ""
    return None


def _show(found, missing: str) -> str:
    if found is TIMED_OUT:
        return f"gave up after {JOURNAL_TIMEOUT}s"
    if found is None:
        return "journal unreadable"
    return found or missing


def previous_boot_lines(journal=_first_journal_line) -> list[str]:
    """How the previous boot ended, from the journal.

    Omarchy's menu shutdown closes windows one by one first, and that is what
    leaves Chrome with a single window (docs/plans/008); `systemctl poweroff`
    does not, and a crash leaves neither record.
    """
    last = journal(["-b", "-1", "-o", "short-unix"])
    if last is TIMED_OUT or last is None or not last:
        return [f"previous boot: {_show(last, 'no entries')}"]
    try:
        end = float(last.split(" ", 1)[0])
    except ValueError:
        return [f"previous boot: unexpected journal line {last[:60]!r}"]
    since = ["--since", f"@{int(end) - SHUTDOWN_WINDOW}", "-o", "short-iso"]
    menu = journal(["--user", "-b", "-1", *since, "-g",
                    "omarchy-system-(shutdown|reboot|logout)"])
    logind = journal(["-b", "-1", *since, "-u", "systemd-logind", "-g",
                      "(poweroff|reboot|halt) requested|Power key"])
    if logind == "" and journal(["-b", "-1", "-u", "systemd-logind"]) == "":
        # Every boot logs logind starting. `-q` hides the warning about a
        # system journal this user cannot read, so seeing none at all means
        # the search above was blind, not that nobody asked for a shutdown.
        logind = None
    return [f"previous boot: last entry {_iso(end)}",
            f"  omarchy menu: {_show(menu, 'no record')}",
            f"  logind: {_show(logind, 'no shutdown request (crash or power loss?)')}"]


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return "?"


def _version() -> str:
    try:
        return json.loads((ROOT / "manifest.json").read_text()).get("version", "?")
    except (OSError, ValueError):
        return "?"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(ts))


def context_lines(trigger: str, session: Path, windows: list[dict], state_dir: Path,
                  log_name: str | None = None, asked: Path | None = None) -> list[str]:
    """The header that lets a log be read without the machine it came from.

    Window classes, workspaces and counts only: no titles, no URLs.
    """
    lines = [f"trigger: {trigger}", f"omasession: {_version()}"]
    if log_name:
        lines.append(f"log: {log_name}")
    uptime = _read("/proc/uptime").split(" ", 1)[0]
    try:
        up = f"{float(uptime):.0f}s"
    except ValueError:
        up = "?"
    lines.append(f"boot: {_read('/proc/sys/kernel/random/boot_id')}, up {up}")
    lines.append(f"hyprland: {os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '?')}")
    try:
        saved = _iso(session.stat().st_mtime)
    except OSError:
        saved = "?"
    classes = Counter(str(w.get("app_id", "?")) for w in windows)
    summary = ", ".join(f"{n} {app}" for app, n in classes.most_common())
    lines.append(f"session: {session} (saved {saved}; {len(windows)} window(s): {summary})")
    if asked is not None and asked != session:
        lines.append(f"  fallback: {asked.name} was not a consistent pair")
    try:
        last = json.loads((state_dir / "last-save.json").read_text())
        lines.append(f"last save: {last.get('when', '?')} rc={last.get('rc', '?')} "
                     f"{last.get('message', '')}".rstrip())
    except (OSError, ValueError, AttributeError):
        lines.append("last save: unknown")
    env = sorted((k, v) for k, v in os.environ.items() if k.startswith("OMASESSION_"))
    if env:
        lines.append("settings: " + " ".join(f"{k[len('OMASESSION_'):].lower()}={v}"
                                             for k, v in env))
    return lines


def profile_lines(prefs: Path, sessions: Path, window_count) -> list[str]:
    """What a browser profile holds before the restore touches it.

    exit_type is the field arm_browser_profile rewrites, and the session files
    are what repair_sessions edits; seeing both as they were is what tells a
    browser that never got its session apart from one we failed to repair.
    """
    lines = []
    try:
        profile = json.loads(prefs.read_text()).get("profile", {})
        lines.append(f"exit_type={profile.get('exit_type', '?')} "
                     f"exited_cleanly={profile.get('exited_cleanly', '?')}")
    except (OSError, ValueError, AttributeError) as exc:
        lines.append(f"Preferences unreadable: {type(exc).__name__}")
    try:
        files = sorted(p for p in sessions.iterdir() if p.is_file())
    except OSError:
        files = []
    described = []
    for p in files:
        # The browser may rotate a file between the listing and this stat.
        try:
            st = p.stat()
        except OSError:
            described.append(f"{p.name} (gone)")
            continue
        described.append(f"{p.name} ({st.st_size} B, "
                         f"{time.strftime('%m-%d %H:%M:%S', time.localtime(st.st_mtime))})")
    lines.append("Sessions: " + (", ".join(described) if described else "none"))
    try:
        lines.append(f"windows in the newest session: {window_count(sessions)}")
    except Exception as exc:  # a parse error is a finding, not a failure
        lines.append(f"windows in the newest session: unreadable ({type(exc).__name__})")
    return lines
