"""Behavioral contracts for the layout preview's geometry."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import layout  # noqa: E402


class MonitorBoxCases(unittest.TestCase):
    def test_scale_gives_logical_size(self) -> None:
        box = layout.monitor_box({"x": 0, "y": 0, "width": 7680, "height": 2160, "scale": 1.5, "transform": 0})
        self.assertEqual((box["w"], box["h"]), (5120, 1440))

    def test_quarter_turn_stands_the_screen_up(self) -> None:
        box = layout.monitor_box({"x": 2560, "y": 0, "width": 1920, "height": 1080, "scale": 1, "transform": 1})
        self.assertEqual((box["x"], box["w"], box["h"]), (2560, 1080, 1920))

    def test_half_turn_keeps_the_shape(self) -> None:
        box = layout.monitor_box({"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1, "transform": 2})
        self.assertEqual((box["w"], box["h"]), (1920, 1080))

    def test_no_geometry_means_unknown(self) -> None:
        self.assertIsNone(layout.monitor_box({"name": "DP-1"}))


class LoadCases(unittest.TestCase):
    def test_older_save_gets_a_box_around_its_windows_and_terminal_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "old.toml").write_text(
                '[[window]]\napp_id = "com.mitchellh.ghostty"\nlaunch_cmd = "ghostty -e btop"\n'
                'workspace = "3"\nmonitor = "DP-1"\nposition = [10, 20]\nsize = [100, 50]\n')
            (d / "old.titles.json").write_text(json.dumps({"monitors": [{"id": 0, "name": "DP-1"}]}))
            data = layout.load(d, "old")
        self.assertEqual(data["monitors"][0]["box"], {"x": 10, "y": 20, "w": 100, "h": 50})
        self.assertEqual(data["workspaces"], ["3"])
        self.assertEqual(data["windows"][0]["label"], "Ghostty · btop")


    def test_a_monitor_with_nothing_to_place_it_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "old.toml").write_text('[session]\nname = "old"\n')
            (d / "old.titles.json").write_text(json.dumps({"monitors": [{"id": 0, "name": "DP-1"}]}))
            data = layout.load(d, "old")
        self.assertEqual(data["monitors"], [])

if __name__ == "__main__":
    unittest.main()
