"""Behavioral contracts for restoring every browser profile and app window."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import replay  # noqa: E402
import resolve  # noqa: E402


class AppWindowCases(unittest.TestCase):
    def test_app_window_class_rebuilds_profile_and_url(self) -> None:
        self.assertEqual(resolve.app_window_argv("brave-x.com__-Profile_4", {}),
                         ["brave", "--profile-directory=Profile 4", "--app=https://x.com/"])

    def test_path_comes_back_from_underscores(self) -> None:
        self.assertEqual(resolve.app_window_argv("chromium-github.com__dmkenney_omasession-Default", {}),
                         ["chromium", "--profile-directory=Default",
                          "--app=https://github.com/dmkenney/omasession"])

    def test_desktop_entry_url_wins_when_it_encodes_to_the_class(self) -> None:
        index = {"hey": {"Exec": "omarchy-launch-webapp https://app.hey.com/imbox?x=1"}}
        self.assertEqual(resolve.app_window_argv("brave-app.hey.com__imbox-Default", index)[-1],
                         "--app=https://app.hey.com/imbox?x=1")

    def test_ordinary_browser_window_is_not_an_app(self) -> None:
        self.assertIsNone(resolve.app_window_argv("brave-browser", {}))
        self.assertIsNone(resolve.app_window_argv("com.mitchellh.ghostty", {}))

    def test_resolve_uses_the_app_rebuild_before_the_shared_command_line(self) -> None:
        result = resolve.resolve({"class": "brave-x.com__-Profile_4", "pid": 1}, {})
        self.assertEqual(result["via"], "app window")
        self.assertIn("--app=https://x.com/", result["argv"])


class ProfileCases(unittest.TestCase):
    def test_browser_command_names_the_profile_once(self) -> None:
        cmd = replay.browser_command("brave --profile-directory=Default", profile="Profile 3")
        self.assertIn("'--profile-directory=Profile 3'", cmd)
        self.assertNotIn("--profile-directory=Default", cmd)
        self.assertIn("--restore-last-session", cmd)

    def test_last_active_profiles_reads_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Local State").write_text(json.dumps(
                {"profile": {"last_active_profiles": ["Default", "Profile 3", "../evil", 7]}}))
            with patch.object(replay, "profile_root", return_value=root):
                self.assertEqual(replay.last_active_profiles("brave-browser"),
                                 ["Default", "Profile 3"])

    def test_missing_local_state_means_unknown(self) -> None:
        with patch.object(replay, "profile_root", return_value=Path("/nonexistent")):
            self.assertEqual(replay.last_active_profiles("brave-browser"), [])

    def test_arming_writes_the_named_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = root / "Profile 3" / "Preferences"
            prefs.parent.mkdir()
            prefs.write_text(json.dumps({"profile": {"exit_type": "Crashed"}}))
            with patch.object(replay, "profile_root", return_value=root), \
                    patch.object(replay, "profile_in_use", return_value=False), \
                    patch.object(replay, "browser_major_version", return_value=None):
                self.assertTrue(replay.arm_browser_profile("brave-browser", "Profile 3"))
            self.assertEqual(json.loads(prefs.read_text())["profile"]["exit_type"], "Normal")


    def test_a_profile_name_that_is_a_path_is_never_armed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Brave"
            root.mkdir()
            outside = Path(tmp) / "Preferences"
            outside.write_text("{}")
            with patch.object(replay, "profile_root", return_value=root), \
                    patch.object(replay, "profile_in_use", return_value=False):
                for name in ("..", ".", "", "/etc", "a/b"):
                    self.assertFalse(replay.arm_browser_profile("brave-browser", name), name)
            self.assertEqual(outside.read_text(), "{}")

class ColdLaunchCases(unittest.TestCase):
    """How restore_browser launches a stopped browser, one profile or several."""

    def launch(self, profiles: str):
        spec = [{"app_id": "chromium", "launch_cmd": "chromium", "workspace": "1",
                 "browser_profiles": profiles}]
        with patch.object(replay, "mapped", return_value=[]), \
                patch.object(replay, "arm_browser_profile", return_value=True), \
                patch.object(replay, "repair_browser_session") as repair, \
                patch.object(replay, "find_new", return_value=None) as find_new, \
                patch.object(replay, "wait_for_browser", return_value=[]), \
                patch.object(replay, "dispatch") as dispatch, \
                patch.object(replay.time, "sleep") as sleep, \
                patch("sys.stdout"):
            replay.restore_browser("chromium", spec, [], set())
        launches = [c.args[1] for c in dispatch.call_args_list if c.args[0] == "exec_cmd"]
        return repair, find_new, sleep, launches

    def test_one_profile_launches_without_waiting(self) -> None:
        repair, find_new, sleep, launches = self.launch("Default")
        self.assertEqual(len(launches), 1)
        find_new.assert_not_called()
        sleep.assert_not_called()
        repair.assert_called_once_with("chromium", 1, ["Default"])

    def test_more_profiles_wait_once_for_the_browser_then_hand_over(self) -> None:
        repair, find_new, sleep, launches = self.launch("Default,Profile 1,Profile 2")
        self.assertEqual(len(launches), 3)
        self.assertIn("--profile-directory=Profile 2", launches[2])
        self.assertEqual(find_new.call_count, 1)
        sleep.assert_not_called()
        repair.assert_called_once_with("chromium", 1, ["Default", "Profile 1", "Profile 2"])


if __name__ == "__main__":
    unittest.main()
