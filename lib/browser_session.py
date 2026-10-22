#!/usr/bin/env python3
"""Undo the window closes Omarchy's close-all writes into a Chrome session.

Omarchy reboots, shuts down and logs out through
`omarchy-hyprland-window-close-all`, which closes windows one at a time.
Chromium records every close but the last as the user closing that window
(`SessionService::WindowClosing`, session_service.cc), so the next start
restores one window. A plain SIGTERM keeps them all. Measured in the lab on
2026-10-20, see docs/plans/008: close-all only *appends* to the session file,
so the closed windows are still described above the tail, and dropping the
close records from the tail brings back every window with its tabs.

SNSS format (components/sessions/core/command_storage_backend.cc): "SNSS",
int32 version, then records of uint16 size, uint8 command id, payload. Only
version 3 (unencrypted) is handled; anything unexpected is left alone.
"""

from __future__ import annotations

import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from safe_fs import open_dir_chain, write_bytes

SIGNATURE = b"SNSS"
VERSION = 3
SET_TAB_WINDOW = 0
TAB_CLOSED = 16
WINDOW_CLOSED = 17
# What close-all leaves after the last real activity: the closes themselves,
# plus the bounds/activation updates of the windows Hyprland re-tiles.
SHUTDOWN_NOISE = frozenset({14, TAB_CLOSED, WINDOW_CLOSED, 20, 21})
MAX_SESSION_BYTES = 64 * 1024 * 1024
KEEP_BACKUPS = 5


@dataclass
class Undo:
    content: bytes | None
    reason: str
    before: int = 0
    after: int = 0


def records(data: bytes, *, partial_tail: bool = False) -> list[tuple[int, int, int]]:
    """Return (start, end, command id) per record; ValueError on anything odd.

    With `partial_tail`, an incomplete last record is dropped instead: Chrome
    replays the commands read before it (SessionFileReader::Read), and a
    browser killed mid-flush right after close-all is when the repair matters.
    """
    if data[:4] != SIGNATURE:
        raise ValueError("not an SNSS session file")
    if len(data) < 8:
        raise ValueError("session header is truncated")
    version = struct.unpack_from("<i", data, 4)[0]
    if version != VERSION:
        raise ValueError(f"session file version {version} is not handled")
    out = []
    offset = 8
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("session file ends inside a record")
        size = struct.unpack_from("<H", data, offset)[0]
        end = offset + 2 + size
        if size == 0:
            raise ValueError("session file has an empty record")
        if end > len(data):
            if partial_tail:
                break
            raise ValueError("session file ends inside a record")
        out.append((offset, end, data[offset + 2]))
        offset = end
    return out


def _first_int(data: bytes, start: int) -> int | None:
    payload = start + 3
    if payload + 4 > len(data):
        return None
    return struct.unpack_from("<i", data, payload)[0]


def _tab_owners(data: bytes, recs: list[tuple[int, int, int]]) -> dict[int, int]:
    owners: dict[int, int] = {}
    for start, end, cid in recs:
        if cid == SET_TAB_WINDOW and end - start - 3 >= 8:
            window, tab = struct.unpack_from("<ii", data, start + 3)
            owners[tab] = window
    return owners


def open_windows(data: bytes) -> dict[int, int]:
    """Window id -> open tab count, replaying the tab/window commands."""
    tabs: dict[int, int] = {}
    for start, end, cid in records(data, partial_tail=True):
        if cid == SET_TAB_WINDOW and end - start - 3 >= 8:
            window, tab = struct.unpack_from("<ii", data, start + 3)
            tabs[tab] = window
        elif cid == TAB_CLOSED:
            tabs.pop(_first_int(data, start), None)
        elif cid == WINDOW_CLOSED:
            window = _first_int(data, start)
            tabs = {t: w for t, w in tabs.items() if w != window}
    counts: dict[int, int] = {}
    for window in tabs.values():
        counts[window] = counts.get(window, 0) + 1
    return counts


def undo_close_all(data: bytes, max_windows: int) -> Undo:
    """Drop the close records close-all left at the end of `data`.

    Only the closes inside the trailing run of shutdown noise count: any real
    activity after them means the browser kept running, so they were the
    user's. A TabClosed is undone only for a tab of a window being reopened,
    so a tab the user closed in the surviving window stays closed.

    Never reopens more windows than the snapshot had. When the run holds more
    closes than that (windows closed by hand right before the reboot, with no
    activity in between), only the latest come back: close-all's are last.
    """
    try:
        recs = records(data, partial_tail=True)
    except ValueError as exc:
        return Undo(None, str(exc))

    tail = len(recs)
    while tail and recs[tail - 1][2] in SHUTDOWN_NOISE:
        tail -= 1
    run = recs[tail:]
    closes = [_first_int(data, start) for start, _, cid in run
              if cid == WINDOW_CLOSED]
    if not closes:
        return Undo(None, "no window closes at the end of the session")

    before = len(open_windows(data))
    room = max_windows - before
    if room <= 0:
        return Undo(None, f"the snapshot had {max_windows} window(s), "
                          f"the session already has {before}", before, before)
    reopened = set(closes[-room:])
    owners = _tab_owners(data, recs[:tail])

    def undone(start: int, cid: int) -> bool:
        target = _first_int(data, start)
        if cid == WINDOW_CLOSED:
            return target in reopened
        return cid == TAB_CLOSED and owners.get(target) in reopened

    kept = recs[:tail] + [r for r in run if not undone(r[0], r[2])]
    content = data[:8] + b"".join(data[start:end] for start, end, _ in kept)

    after = len(open_windows(content))
    if after <= before:
        return Undo(None, "undoing the closes reopens nothing", before, after)
    return Undo(content, "", before, after)


def _session_files(directory: Path) -> list[tuple[int, Path]]:
    found = []
    for path in directory.glob("Session_*"):
        stamp = path.name.removeprefix("Session_")
        if stamp.isascii() and stamp.isdigit() and path.is_file():
            found.append((int(stamp), path))
    return sorted(found)


def _keep_backup(backups: Path, name: str, data: bytes) -> None:
    """Copy the original aside, through the same checked path the state uses."""
    fd = open_dir_chain(str(backups), create=True)
    try:
        write_bytes(fd, name, data, mode=0o600)
        old = sorted((int(n.removeprefix("Session_")), n) for n in os.listdir(fd)
                     if n.startswith("Session_")
                     and n.removeprefix("Session_").isascii()
                     and n.removeprefix("Session_").isdigit())
        for _, stale in old[:-KEEP_BACKUPS]:
            os.unlink(stale, dir_fd=fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".omasession-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def session_window_count(sessions: Path) -> int:
    """Windows open in the session the browser will read next; 0 if unknown."""
    files = _session_files(sessions) if sessions.is_dir() else []
    if not files:
        return 0
    path = files[-1][1]
    try:
        if path.stat().st_size > MAX_SESSION_BYTES:
            return 0
        return len(open_windows(path.read_bytes()))
    except (OSError, ValueError):
        return 0


def repair_sessions(sessions: Path, backups: Path, max_windows: int) -> str:
    """Repair the session Chrome will read next; return one line for the log.

    The caller guarantees the browser is not running (arm_browser_profile
    refuses otherwise). The original goes to `backups` first: Chrome deletes
    every other Session_* file at startup, so a copy inside Sessions/ would
    not survive.
    """
    files = _session_files(sessions) if sessions.is_dir() else []
    if not files:
        return "no session file"
    path = files[-1][1]
    if path.stat().st_size > MAX_SESSION_BYTES:
        return f"left as is: {path.name} is larger than expected"
    data = path.read_bytes()

    result = undo_close_all(data, max_windows)
    if result.content is None:
        return f"left as is: {result.reason}"

    _keep_backup(backups, path.name, data)
    _write_atomic(path, result.content)
    note = (f"{result.before} -> {result.after} window(s), undid the closes "
            f"left by a window-by-window shutdown (original kept in "
            f"{backups / path.name})")
    # Chrome 152 writes an encrypted copy too but still reads this one (lab,
    # 2026-10-21); later stages of kEncryptSessionStorage prefer the
    # encrypted copy, which this cannot repair. Said in the log so empty
    # windows below it explain themselves.
    encrypted = sessions.parent / "Sessions_Encrypted"
    if encrypted.is_dir() and any(encrypted.glob("Session_*")):
        note += "; an encrypted copy exists too and was not changed"
    return note
