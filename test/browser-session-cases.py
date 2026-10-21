#!/usr/bin/env python3
"""Undoing the window closes Omarchy's close-all leaves in a Chrome session.

The tails here copy what the lab measured on 2026-10-20 (docs/plans/008): per
closed window, one TabClosed per tab, then its WindowClosed, interleaved with
SetWindowBounds3 for the windows that remain while Hyprland re-tiles.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import browser_session as S  # noqa: E402

SET_TAB_WINDOW, NAVIGATION, BOUNDS, TAB_CLOSED, WINDOW_CLOSED = 0, 6, 14, 16, 17


def record(cid: int, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload) + 1, cid) + payload


def tab_window(window: int, tab: int) -> bytes:
    return record(SET_TAB_WINDOW, struct.pack("<ii", window, tab))


def bounds(window: int) -> bytes:
    return record(BOUNDS, struct.pack("<iiiiii", window, 0, 0, 800, 600, 0))


def closed_payload(target: int) -> bytes:
    # ClosedPayload {int32 id; int64 close_time}, padded to 16 bytes the way
    # Chrome writes it (session_service_commands.cc).
    return struct.pack("<i4xq", target, 0)


def tab_closed(tab: int) -> bytes:
    return record(TAB_CLOSED, closed_payload(tab))


def window_closed(window: int) -> bytes:
    return record(WINDOW_CLOSED, closed_payload(window))


def navigation(tab: int) -> bytes:
    return record(NAVIGATION, struct.pack("<ii", tab, 0) + b"https://x/")


def session(*records: bytes, version: int = 3) -> bytes:
    return b"SNSS" + struct.pack("<i", version) + b"".join(records)


WINDOWS = (10, 20, 30, 40)


def four_windows() -> list[bytes]:
    out = []
    for w in WINDOWS:
        out += [tab_window(w, w + 1), navigation(w + 1),
                tab_window(w, w + 2), navigation(w + 2), bounds(w)]
    return out


def close_all_tail() -> list[bytes]:
    """Close 10, 20, 30 the way close-all does; 40 is the survivor."""
    out = []
    for w in WINDOWS[:-1]:
        out += [tab_closed(w + 2), tab_closed(w + 1), bounds(w), bounds(40),
                window_closed(w), bounds(40)]
    return out


class UndoCloseAll(unittest.TestCase):
    def test_close_all_tail_is_undone_and_every_window_comes_back(self) -> None:
        """Regression: before the fix only the survivor reopened (1 of 4)."""
        data = session(*four_windows(), *close_all_tail())

        self.assertEqual(S.open_windows(data), {40: 2})

        result = S.undo_close_all(data, max_windows=4)

        self.assertIsNotNone(result.content, result.reason)
        self.assertEqual(S.open_windows(result.content),
                         {10: 2, 20: 2, 30: 2, 40: 2})
        self.assertEqual((result.before, result.after), (1, 4))

    def test_only_close_records_are_removed(self) -> None:
        body = four_windows()
        data = session(*body, *close_all_tail())

        result = S.undo_close_all(data, max_windows=4)

        kept = session(*body, *[r for r in close_all_tail()
                                if r[2] not in (TAB_CLOSED, WINDOW_CLOSED)])
        self.assertEqual(result.content, kept)

    def test_activity_after_the_closes_means_they_were_not_a_shutdown(self) -> None:
        data = session(*four_windows(), *close_all_tail(), navigation(41))

        result = S.undo_close_all(data, max_windows=4)

        self.assertIsNone(result.content)

    def test_never_reopens_more_windows_than_the_snapshot_had(self) -> None:
        """Only the latest closes come back: close-all's are always last."""
        data = session(*four_windows(), *close_all_tail())

        result = S.undo_close_all(data, max_windows=2)

        self.assertEqual(S.open_windows(result.content), {30: 2, 40: 2})

    def test_windows_closed_by_hand_before_a_menu_reboot_stay_closed(self) -> None:
        # The user closes 10 by hand, the snapshot catches the 3 left, then
        # close-all closes 20 and 30. Both land in the same trailing run.
        data = session(*four_windows(), *close_all_tail())

        result = S.undo_close_all(data, max_windows=3)

        self.assertEqual(S.open_windows(result.content),
                         {20: 2, 30: 2, 40: 2})

    def test_a_snapshot_with_no_room_left_changes_nothing(self) -> None:
        data = session(*four_windows(), *close_all_tail())

        result = S.undo_close_all(data, max_windows=1)

        self.assertIsNone(result.content)
        self.assertIn("snapshot", result.reason)

    def test_nothing_to_do_when_the_session_already_has_every_window(self) -> None:
        result = S.undo_close_all(session(*four_windows()), max_windows=4)

        self.assertIsNone(result.content)

    def test_a_tab_closed_in_the_surviving_window_stays_closed(self) -> None:
        data = session(*four_windows(), *close_all_tail()[:-1],
                       tab_closed(42), bounds(40))

        result = S.undo_close_all(data, max_windows=4)

        self.assertEqual(S.open_windows(result.content),
                         {10: 2, 20: 2, 30: 2, 40: 1})

    def test_an_incomplete_last_record_is_dropped_like_chrome_does(self) -> None:
        # Browser killed mid-flush: Chrome replays what it read before it.
        data = session(*four_windows(), *close_all_tail())[:-3]

        result = S.undo_close_all(data, max_windows=4)

        self.assertEqual(S.open_windows(result.content),
                         {10: 2, 20: 2, 30: 2, 40: 2})

    def test_unknown_format_is_left_alone(self) -> None:
        tail = close_all_tail()
        cases = {
            "encrypted": session(*four_windows(), *tail, version=5),
            "not snss": b"XXXX" + session(*four_windows(), *tail)[4:],
            "empty record": session(*four_windows(), b"\x00\x00", *tail),
        }
        for name, data in cases.items():
            with self.subTest(name):
                result = S.undo_close_all(data, max_windows=4)
                self.assertIsNone(result.content)
                self.assertTrue(result.reason)


class RepairProfile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sessions = root / "Default" / "Sessions"
        self.sessions.mkdir(parents=True)
        self.backups = root / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_newest_session_is_repaired_and_the_original_kept_aside(self) -> None:
        broken = session(*four_windows(), *close_all_tail())
        old = session(*four_windows())
        (self.sessions / "Session_100").write_bytes(old)
        (self.sessions / "Session_200").write_bytes(broken)
        (self.sessions / "Tabs_200").write_bytes(b"tabs")

        note = S.repair_sessions(self.sessions, self.backups, max_windows=4)

        self.assertIn("4", note)
        repaired = (self.sessions / "Session_200").read_bytes()
        self.assertEqual(len(S.open_windows(repaired)), 4)
        self.assertEqual((self.sessions / "Session_100").read_bytes(), old)
        self.assertEqual((self.sessions / "Tabs_200").read_bytes(), b"tabs")
        self.assertEqual((self.backups / "Session_200").read_bytes(), broken)
        self.assertEqual(sorted(p.name for p in self.sessions.iterdir()),
                         ["Session_100", "Session_200", "Tabs_200"])

    def test_a_session_that_needs_nothing_is_not_touched_or_copied(self) -> None:
        good = session(*four_windows())
        (self.sessions / "Session_200").write_bytes(good)

        S.repair_sessions(self.sessions, self.backups, max_windows=4)

        self.assertEqual((self.sessions / "Session_200").read_bytes(), good)
        self.assertFalse(self.backups.exists())

    def test_an_encrypted_copy_is_reported_but_does_not_block(self) -> None:
        # Chrome 152 writes both and reads the plain one (lab, 2026-10-21): a
        # version of this that refused here brought back 1 of 3 windows.
        (self.sessions / "Session_200").write_bytes(
            session(*four_windows(), *close_all_tail()))
        encrypted = self.sessions.parent / "Sessions_Encrypted"
        encrypted.mkdir()
        (encrypted / "Session_201").write_bytes(b"sealed")

        note = S.repair_sessions(self.sessions, self.backups, 4)

        self.assertIn("1 -> 4", note)
        self.assertIn("encrypted copy exists", note)
        self.assertEqual((encrypted / "Session_201").read_bytes(), b"sealed")

    def test_only_the_last_backups_are_kept(self) -> None:
        self.backups.mkdir(mode=0o700)
        for stamp in range(1, 7):
            (self.backups / f"Session_{stamp}").write_bytes(b"old")
        (self.sessions / "Session_200").write_bytes(
            session(*four_windows(), *close_all_tail()))

        S.repair_sessions(self.sessions, self.backups, 4)

        self.assertEqual(sorted(p.name for p in self.backups.iterdir()),
                         ["Session_200", "Session_3", "Session_4",
                          "Session_5", "Session_6"])

    def test_a_backup_dir_that_is_not_ours_stops_before_any_write(self) -> None:
        broken = session(*four_windows(), *close_all_tail())
        (self.sessions / "Session_200").write_bytes(broken)
        self.backups.mkdir(mode=0o777)
        self.backups.chmod(0o777)

        with self.assertRaises(Exception):
            S.repair_sessions(self.sessions, self.backups, 4)

        self.assertEqual((self.sessions / "Session_200").read_bytes(), broken)

    def test_non_ascii_digits_in_the_name_are_not_a_session(self) -> None:
        (self.sessions / "Session_²").write_bytes(session(*four_windows()))

        note = S.repair_sessions(self.sessions, self.backups, 4)

        self.assertIn("no session file", note)

    def test_missing_sessions_directory_is_not_an_error(self) -> None:
        note = S.repair_sessions(self.sessions / "nope", self.backups, 4)

        self.assertIn("no session file", note)


if __name__ == "__main__":
    unittest.main()
