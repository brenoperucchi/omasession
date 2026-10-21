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

    def test_sigterm_during_placement_waits_until_every_window_is_out(self) -> None:
        import os
        import signal
        placed: list[str] = []
        released: list[dict] = []

        def place(addr, spec):
            if not placed:
                os.kill(os.getpid(), signal.SIGTERM)
            placed.append(addr)

        replay._staged = [("0xa", {"workspace": "1", "order": 0}, False),
                          ("0xb", {"workspace": "2", "order": 1}, False)]
        old = signal.signal(signal.SIGTERM, replay.exit_on_signal)
        try:
            with patch.object(replay, "place", side_effect=place), \
                    patch.object(replay, "release_parked", side_effect=released.append), \
                    self.assertRaises(SystemExit):
                replay.place_staged()
        finally:
            signal.signal(signal.SIGTERM, old)
        self.assertEqual(placed, ["0xa", "0xb"])
        self.assertEqual(released, [{"0xa": "1", "0xb": "2"}])

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


if __name__ == "__main__":
    unittest.main()
