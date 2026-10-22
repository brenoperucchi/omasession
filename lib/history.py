#!/usr/bin/env python3
"""Earlier sessions: a thinned-out history of `last`, and the list command.

Every published save of `last` is copied to history-<UTC time> when the
desktop's shape changed -- which windows, on which workspace, in which slot,
launched how. Titles and geometry are left out of the comparison: a song or a
page title changes every few seconds and would turn the history into a log of
nothing. Old copies are thinned so the history reaches back without growing:

    last 15 minutes   every change
    to 2 hours        one per 15 minutes
    to 1 day          one per hour
    to 7 days         one per day
    older             removed

`omasession restore history-<time>` brings one back; so does any named session
saved with `omasession save <name>`. `list` shows both.

    history.py snapshot <session-dir>
    history.py list <session-dir> [--json]
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from safe_fs import Refused, copy_within, open_dir_chain, read_capped_path  # noqa: E402

import os  # noqa: E402

PREFIX = "history-"
STAMP = re.compile(r"^history-(\d{8}-\d{6})\.toml$")
TITLES = re.compile(r"^(history-\d{8}-\d{6})\.titles\.json$")
# (age limit in seconds, bucket size in seconds); 0 keeps every snapshot.
TIERS = ((15 * 60, 0), (2 * 3600, 15 * 60), (24 * 3600, 3600), (7 * 86400, 86400))
RESERVED = re.compile(r"(^last$|\.prev$|^history-|^omasession\.)")
# A snapshot stamped further ahead than this was written under a wrong clock
# (RTC skew, NTP stepping back). It is dropped: compared against it, every
# later save would look new and add a file.
CLOCK_SLACK = 3600


def load(path: Path) -> list[dict]:
    try:
        return tomllib.loads(read_capped_path(path).decode()).get("window", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []


def shape(windows: list[dict]) -> str:
    """What makes two saves the same desktop, ignoring titles and geometry."""
    rows = sorted(
        (str(w.get("workspace")), w.get("order", -1), w.get("app_id", ""), w.get("launch_cmd") or "")
        for w in windows
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def stamp_time(name: str) -> float | None:
    match = STAMP.match(name)
    if not match:
        return None
    try:
        return float(calendar.timegm(time.strptime(match.group(1), "%Y%m%d-%H%M%S")))
    except ValueError:  # matches the pattern but is no date (month 13)
        return None


def snapshots(session_dir: Path) -> list[tuple[float, str]]:
    """(time, stem) of every history snapshot, newest first."""
    found = []
    for path in session_dir.glob(PREFIX + "*.toml"):
        when = stamp_time(path.name)
        if when is not None:
            found.append((when, path.stem))
    return sorted(found, reverse=True)


def keep(snaps: list[tuple[float, str]], now: float,
         sizes: dict[str, int] | None = None) -> set[str]:
    """Which snapshots survive thinning: the newest in each tier bucket, and
    the one with the most windows when that is a different one.

    The fullest is kept because the newest can be what was left after a crash
    or a bad restore -- the state worth going back to is the one before it.
    """
    sizes = sizes or {}
    kept: set[str] = set()
    newest: set[tuple[int, int]] = set()
    fullest: dict[tuple[int, int], str] = {}
    for when, stem in snaps:  # newest first
        age = now - when
        for tier, (limit, bucket) in enumerate(TIERS):
            if age > limit:
                continue
            if bucket == 0:
                kept.add(stem)
            else:
                key = (tier, int(when // bucket))
                if key not in newest:
                    newest.add(key)
                    kept.add(stem)
                if sizes.get(stem, 0) > sizes.get(fullest.get(key, ""), 0):
                    fullest[key] = stem
            break
    return kept | set(fullest.values())


def snapshot(session_dir: Path, now: float | None = None) -> str | None:
    """Copy `last` into the history if its shape changed; then thin it out."""
    now = time.time() if now is None else now
    last = session_dir / "last.toml"
    windows = load(last)
    if not windows:
        return None
    snaps = snapshots(session_dir)
    ahead = [stem for when, stem in snaps if when > now + CLOCK_SLACK]
    snaps = [(when, stem) for when, stem in snaps if when <= now + CLOCK_SLACK]
    made = None
    if not snaps or shape(load(session_dir / f"{snaps[0][1]}.toml")) != shape(windows):
        stem = PREFIX + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        if stem not in {s for _, s in snaps}:
            dir_fd = open_dir_chain(str(session_dir), create=False)
            try:
                copy_within(dir_fd, "last.toml", f"{stem}.toml")
                copy_within(dir_fd, "last.titles.json", f"{stem}.titles.json")
            finally:
                os.close(dir_fd)
            snaps.insert(0, (now, stem))
            made = stem
    sizes = {stem: len(load(session_dir / f"{stem}.toml")) for _, stem in snaps}
    survivors = keep(snaps, now, sizes)
    remove(session_dir, ahead + [stem for _, stem in snaps if stem not in survivors])
    return made


def remove(session_dir: Path, stems: list[str]) -> None:
    """Delete these snapshots, and titles whose snapshot is gone (a crash
    between the two copies or the two deletes), by name inside the checked
    directory. One that will not go -- a folder, say -- is left; the rest go.
    """
    dir_fd = open_dir_chain(str(session_dir), create=False)
    try:
        names = set(os.listdir(dir_fd))
        doomed = {f"{stem}{suffix}" for stem in stems for suffix in (".toml", ".titles.json")}
        doomed |= {name for name in names
                   if (m := TITLES.match(name)) and f"{m.group(1)}.toml" not in names}
        for name in sorted(doomed & names):
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
    finally:
        os.close(dir_fd)


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{int(seconds // 86400)}d ago"


def listing(session_dir: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    named = []
    for path in sorted(session_dir.glob("*.toml")):
        stem = path.stem
        if RESERVED.search(stem) or stem.endswith(".staging") or stem.startswith("."):
            continue
        named.append({"name": stem, "windows": len(load(path)),
                      "agoSec": int(now - path.stat().st_mtime)})
    history = [{"name": stem, "windows": len(load(session_dir / f"{stem}.toml")),
                "agoSec": int(now - when)} for when, stem in snapshots(session_dir)]
    return {"named": named, "history": history}


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("snapshot", "list"):
        print(__doc__.strip().splitlines()[-2:][0].strip(), file=sys.stderr)
        return 2
    session_dir = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        try:
            made = snapshot(session_dir)
        except (OSError, Refused) as exc:
            print(f"history: not saved: {exc}", file=sys.stderr)
            return 1
        if made:
            print(f"history: kept {made}")
        return 0
    data = listing(session_dir)
    if "--json" in sys.argv[3:]:
        print(json.dumps(data))
        return 0
    print("Saved sessions (omasession restore <name>):")
    for row in data["named"] or [{"name": "(none -- omasession save <name>)", "windows": "", "agoSec": None}]:
        when = ago(row["agoSec"]) if row["agoSec"] is not None else ""
        print(f"  {row['name']:28} {row['windows']!s:>3} {'windows' if row['windows'] != '' else ''}  {when}")
    print("History:")
    for row in data["history"] or [{"name": "(empty)", "windows": "", "agoSec": None}]:
        when = ago(row["agoSec"]) if row["agoSec"] is not None else ""
        print(f"  {row['name']:28} {row['windows']!s:>3} {'windows' if row['windows'] != '' else ''}  {when}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
