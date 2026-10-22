"""Behavioral contracts for restoring windows in their saved slot order."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import capture  # noqa: E402
import replay  # noqa: E402


def client(address: str, workspace: int) -> dict:
    return {"address": address, "workspace": {"id": workspace}}


class CaptureOrderCases(unittest.TestCase):
    def test_layout_order_wins_over_client_list_order(self) -> None:
        # A swap leaves the client list in creation order; the layout knows better.
        windows = [client("0xa", 1), client("0xb", 1), client("0xc", 1)]
        orders = capture.slot_orders(windows, {"1": ["0xb", "0xa", "0xc"]})
        self.assertEqual(orders, {"0xb": 0, "0xa": 1, "0xc": 2})

    def test_client_list_order_without_a_layout_hook(self) -> None:
        windows = [client("0xa", 1), client("0xb", 2), client("0xc", 1)]
        orders = capture.slot_orders(windows, {})
        self.assertEqual(orders, {"0xa": 0, "0xc": 1, "0xb": 0})

    def test_windows_the_layout_did_not_report_go_last(self) -> None:
        # A floating window is not a layout target, and stale entries are ignored.
        windows = [client("0xa", 1), client("0xf", 1), client("0xb", 1)]
        orders = capture.slot_orders(windows, {"1": ["0xb", "0xgone", "0xa"]})
        self.assertEqual(orders, {"0xb": 0, "0xa": 1, "0xf": 2})

    def test_missing_or_bad_hook_output_means_no_layout_order(self) -> None:
        for stdout in ("", "not json", "[1, 2]"):
            with patch.object(capture.subprocess, "run") as run:
                run.return_value.stdout = stdout
                self.assertEqual(capture.layout_slot_order(), {})


class ReplayOrderCases(unittest.TestCase):
    def tearDown(self) -> None:
        replay._staged = None

    def test_staged_windows_are_placed_in_slot_order(self) -> None:
        placed: list[str] = []
        replay._staged = []
        with patch.object(replay, "dispatch"), patch.object(replay.time, "sleep"):
            replay.place("0xa", {"workspace": "1", "order": 1})
            replay.place_browser("0xb", {"workspace": "1", "_spec": {"order": 0}})
            replay.place("0xc", {"workspace": "1", "order": 2})
        self.assertEqual(len(replay._staged), 3)

        with patch.object(replay, "place", side_effect=lambda a, s: placed.append(a)), \
                patch.object(replay, "move_to", side_effect=lambda a, w: placed.append(a)), \
                patch.object(replay, "release_parked"):
            replay.place_staged()
        self.assertEqual(placed, ["0xb", "0xa", "0xc"])
        self.assertIsNone(replay._staged)

    def _place_with_signal(self, signum: int, expected: type[BaseException]) -> tuple[list, list]:
        import os
        import signal
        placed: list[str] = []
        released: list[dict] = []

        def place(addr, spec):
            if not placed:
                os.kill(os.getpid(), signum)
            placed.append(addr)

        replay._staged = [("0xa", {"workspace": "1", "order": 0}, False),
                          ("0xb", {"workspace": "2", "order": 1}, False)]
        old = signal.signal(signal.SIGTERM, replay.exit_on_signal)
        try:
            with patch.object(replay, "place", side_effect=place), \
                    patch.object(replay, "release_parked", side_effect=released.append), \
                    self.assertRaises(expected):
                replay.place_staged()
        finally:
            signal.signal(signal.SIGTERM, old)
        return placed, released

    def test_sigterm_during_placement_ends_the_ordered_pass_and_releases_all(self) -> None:
        import signal
        placed, released = self._place_with_signal(signal.SIGTERM, SystemExit)
        # The stop request is not held for the whole pass: placement stops after
        # the window in progress, and release_parked puts every window back.
        self.assertEqual(placed, ["0xa"])
        self.assertEqual(released, [{"0xa": "1", "0xb": "2"}])

    def test_ctrl_c_during_placement_still_releases_parked_windows(self) -> None:
        import signal
        placed, released = self._place_with_signal(signal.SIGINT, KeyboardInterrupt)
        self.assertEqual(placed, ["0xa"])
        self.assertEqual(released, [{"0xa": "1", "0xb": "2"}])

    def test_second_ctrl_c_does_not_interrupt_the_release(self) -> None:
        import os
        import signal
        released: list[str] = []

        def release(targets):
            os.kill(os.getpid(), signal.SIGINT)   # a second Ctrl-C mid-release
            released.append("done")

        replay._staged = [("0xa", {"workspace": "1"}, False)]
        with patch.object(replay, "place", side_effect=lambda a, s: os.kill(os.getpid(), signal.SIGINT)), \
                patch.object(replay, "release_parked", side_effect=release), \
                self.assertRaises(KeyboardInterrupt):
            replay.place_staged()
        self.assertEqual(released, ["done"])

    def test_release_runs_even_when_placement_raises(self) -> None:
        released: list[dict] = []
        replay._staged = [("0xa", {"workspace": "1"}, False)]
        with patch.object(replay, "place", side_effect=KeyboardInterrupt), \
                patch.object(replay, "release_parked", side_effect=released.append), \
                self.assertRaises(KeyboardInterrupt):
            replay.place_staged()
        self.assertEqual(released, [{"0xa": "1"}])

    def test_signals_are_not_blocked_while_placing(self) -> None:
        # A blocked mask is inherited by every hyprctl child the placement runs.
        import signal
        masks: list[set] = []
        replay._staged = [("0xa", {"workspace": "1"}, False)]
        with patch.object(replay, "place",
                          side_effect=lambda a, s: masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))), \
                patch.object(replay, "release_parked"):
            replay.place_staged()
        self.assertEqual(len(masks), 1)
        self.assertFalse(masks[0] & {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})

    def test_handlers_are_put_back_after_placement(self) -> None:
        import signal
        before = signal.getsignal(signal.SIGTERM)
        replay._staged = []
        with patch.object(replay, "release_parked"):
            replay.place_staged()
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_windows_without_order_keep_restore_order_after_ordered_ones(self) -> None:
        items = [("0xa", {}, False), ("0xb", {"order": 0}, False), ("0xc", {}, False)]
        ranked = sorted(items, key=lambda item: replay.slot_order(item[1]))
        self.assertEqual([a for a, _, _ in ranked], ["0xb", "0xa", "0xc"])

    def test_staging_parks_on_the_hidden_workspace_without_focus(self) -> None:
        replay._staged = []
        with patch.object(replay, "dispatch") as dispatch, patch.object(replay.time, "sleep"):
            replay.place("0xa", {"workspace": "3", "order": 0})
        dispatch.assert_called_once_with(
            "window.move", window="address:0xa",
            workspace=replay.STAGING_WORKSPACE, follow=False)

    def test_place_moves_directly_when_not_staging(self) -> None:
        with patch.object(replay, "dispatch") as dispatch, patch.object(replay.time, "sleep"):
            replay.place("0xa", {"workspace": "3"})
        dispatch.assert_called_once_with("window.move", window="address:0xa", workspace=3)


class RestoreOrderSettingCases(unittest.TestCase):
    def test_true_and_false_win_over_the_layout(self) -> None:
        with patch.object(replay, "layout_reports_order", return_value=False):
            self.assertTrue(replay.restore_order_enabled("true"))
        with patch.object(replay, "layout_reports_order", return_value=True):
            self.assertFalse(replay.restore_order_enabled("false"))

    def test_auto_follows_whether_the_config_reports_an_order(self) -> None:
        # Dwindle and master users have no window_slot_order(): parking their
        # windows during restore would hide them for nothing.
        for reported in (True, False):
            with patch.object(replay, "layout_reports_order", return_value=reported):
                self.assertEqual(replay.restore_order_enabled("auto"), reported)

    def test_layout_check_reads_the_hook_from_hyprctl(self) -> None:
        for stdout, expected in (("yes\n", True), ("no\n", False), ("", False), ("error", False)):
            with patch.object(replay.subprocess, "run") as run:
                run.return_value.stdout = stdout
                self.assertEqual(replay.layout_reports_order(), expected)

    def test_layout_check_survives_a_hung_compositor(self) -> None:
        with patch.object(replay.subprocess, "run",
                          side_effect=replay.subprocess.TimeoutExpired("hyprctl", 15)):
            self.assertFalse(replay.layout_reports_order())

    def test_restore_and_restore_app_both_pass_the_setting(self) -> None:
        import re
        source = (ROOT / "bin" / "omasession").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_RESTORE_ORDER=auto", source)
        for name in ("cmd_restore", "cmd_restore_app"):
            body = re.search(rf"^{name}\(\) \{{(?P<body>.*?)^\}}\n", source,
                             re.DOTALL | re.MULTILINE).group("body")
            self.assertIn('config_get restoreOrder "$DEFAULT_RESTORE_ORDER"', body, name)
            self.assertIn('OMASESSION_RESTORE_ORDER="$restore_order"', body, name)


class SaveWhileParkedCases(unittest.TestCase):
    def test_save_skips_a_tick_while_windows_are_parked(self) -> None:
        source = (ROOT / "lib" / "session-save.sh").read_text(encoding="utf-8")
        self.assertIn(replay.STAGING_WORKSPACE, source)
        skip = source.index(replay.STAGING_WORKSPACE)
        # Checked before anything is captured or published.
        self.assertLess(skip, source.index("count_screen="))


if __name__ == "__main__":
    unittest.main()
