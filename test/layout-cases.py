"""Behavioral contracts for the layout preview's geometry."""

from __future__ import annotations

import json
import subprocess
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


class MalformedCases(unittest.TestCase):
    """A hand-edited or damaged save draws what it can, or answers an error
    the panel can show; it never ends in a traceback with no JSON."""

    def run_main(self, toml: str, sidecar: object = None) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "s.toml").write_text(toml)
            if sidecar is not None:
                (d / "s.titles.json").write_text(json.dumps(sidecar))
            out = subprocess.run([sys.executable, str(ROOT / "lib" / "layout.py"), tmp, "s"],
                                 capture_output=True, text=True)
        return out.returncode, json.loads(out.stdout)

    def test_bad_window_geometry_draws_the_window_without_a_box(self) -> None:
        for position in ("[10]", '"a"', '["x", "y"]'):
            with self.subTest(position=position):
                rc, data = self.run_main(
                    f'[[window]]\napp_id = "foot"\nworkspace = "1"\nposition = {position}\nsize = [5, 5]\n')
                self.assertEqual(rc, 0)
                self.assertEqual(data["windows"][0]["box"], {"x": 0, "y": 0, "w": 5, "h": 5})

    def test_a_launch_command_ending_in_its_exec_flag_keeps_the_app_name(self) -> None:
        for cmd in ("ghostty -e ", "foot -- "):
            with self.subTest(cmd=cmd):
                rc, data = self.run_main(f'[[window]]\napp_id = "foot"\nlaunch_cmd = "{cmd}"\n')
                self.assertEqual(rc, 0)
                self.assertEqual(data["windows"][0]["label"], "Foot")

    def test_odd_monitor_data_is_skipped(self) -> None:
        good = {"name": "DP-1", "x": 0, "y": 0, "width": 100, "height": 50}
        for monitors in ([good, "x"], [{**good, "transform": "flip"}], "DP-1"):
            with self.subTest(monitors=monitors):
                rc, data = self.run_main('[[window]]\napp_id = "foot"\n', {"monitors": monitors})
                self.assertEqual(rc, 0)
                self.assertIn("windows", data)

    def test_a_non_text_app_id_is_drawn(self) -> None:
        rc, data = self.run_main('[[window]]\napp_id = 7\n')
        self.assertEqual(rc, 0)
        self.assertEqual(len(data["windows"]), 1)

    def test_anything_else_is_an_error_answer(self) -> None:
        rc, data = self.run_main('window = "not a table list"\n')
        self.assertEqual(rc, 1)
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
