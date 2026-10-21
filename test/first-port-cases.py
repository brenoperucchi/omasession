#!/usr/bin/env python3
"""Focused checks for the first Ghostty/browser restore port."""

from __future__ import annotations

import shlex
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import replay  # noqa: E402
import resolve  # noqa: E402
import capture  # noqa: E402


class FirstPortCases(unittest.TestCase):
    def test_ghostty_flags_stay_before_child_and_preserve_title(self) -> None:
        argv = [
            "env", "TERM=xterm", "ghostty", "--title=keep-me",
            "--gtk-single-instance", "true", "--working-directory", "/old",
            "-e", "bash", "-lc", "printf hi",
        ]
        result = resolve.ghostty_argv(argv, "/home/breno/project")
        self.assertEqual(result[:2], ["env", "TERM=xterm"])
        self.assertEqual(result[2:5], [
            "ghostty", "--gtk-single-instance=false",
            "--working-directory=/home/breno/project",
        ])
        self.assertIn("--title=keep-me", result)
        self.assertEqual(result[result.index("-e"):], ["-e", "bash", "-lc", "printf hi"])
        self.assertNotIn("true", result)
        self.assertNotIn("/old", result)

    def test_ghostty_rewrite_is_idempotent(self) -> None:
        argv = ["ghostty", "--gtk-single-instance=false", "--working-directory=/tmp", "--", "zsh"]
        once = resolve.ghostty_argv(argv, "/work")
        self.assertEqual(resolve.ghostty_argv(once, "/work"), once)

    def test_ghostty_keeps_saved_cwd_when_live_cwd_is_unknown(self) -> None:
        argv = resolve.ghostty_argv(
            ["ghostty", "--working-directory=/saved", "-e", "btop"], None
        )
        self.assertIn("--working-directory=/saved", argv)
        self.assertEqual(argv[argv.index("-e"):], ["-e", "btop"])

    def test_browser_command_replaces_restore_flags_once(self) -> None:
        command = "google-chrome --no-first-run --restore-last-session --profile-directory=Default"
        result = shlex.split(replay.browser_command(command))
        self.assertEqual(result.count("--restore-last-session"), 1)
        self.assertEqual(result.count("--no-first-run"), 1)
        self.assertEqual(result.count("--no-default-browser-check"), 1)
        self.assertNotIn("--new-window", result)

        replacement = shlex.split(replay.browser_command(command, restore_session=False, new_window=True))
        self.assertNotIn("--restore-last-session", replacement)
        self.assertEqual(replacement[-1], "--new-window")

    def test_browser_flags_stay_before_a_child_separator(self) -> None:
        result = shlex.split(replay.browser_command("google-chrome -- --child", new_window=True))
        self.assertEqual(result[:4], [
            "google-chrome", "--restore-last-session", "--no-first-run",
            "--no-default-browser-check",
        ])
        self.assertEqual(result[4:], ["--new-window", "--", "--child"])

    def test_browser_wrapper_stays_before_the_effective_binary(self) -> None:
        result = shlex.split(replay.browser_command("env -u HTTP_PROXY -- chromium -- --child"))
        self.assertEqual(result[:7], [
            "env", "-u", "HTTP_PROXY", "--", "chromium",
            "--restore-last-session", "--no-first-run",
        ])
        self.assertEqual(result[7:10], ["--no-default-browser-check", "--", "--child"])

    def test_existing_browser_wait_returns_latest_title(self) -> None:
        old = [{"address": "0x1", "class": "chromium", "title": "New Tab"}]
        new = [{"address": "0x1", "class": "chromium", "title": "Saved title"}]
        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(_seconds: float) -> None:
            clock[0] += 1

        with patch.object(replay, "mapped", side_effect=[old, new]), \
             patch.object(replay.time, "monotonic", side_effect=monotonic), \
             patch.object(replay.time, "sleep", side_effect=sleep), \
             patch.object(replay, "BROWSER_TIMEOUT", 3), \
             patch.object(replay, "BROWSER_QUIET", 1):
            found = replay.wait_for_existing_browser("chromium")

        self.assertEqual(found[0]["title"], "Saved title")

    def test_new_browser_wait_returns_latest_title_with_same_address(self) -> None:
        old = [{"address": "0x1", "class": "chromium", "title": "New Tab"}]
        new = [{"address": "0x1", "class": "chromium", "title": "Saved title"}]
        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(_seconds: float) -> None:
            clock[0] += 1

        with patch.object(replay, "mapped", side_effect=[old, new]), \
             patch.object(replay.time, "monotonic", side_effect=monotonic), \
             patch.object(replay.time, "sleep", side_effect=sleep), \
             patch.object(replay, "BROWSER_TIMEOUT", 3), \
             patch.object(replay, "BROWSER_QUIET", 1):
            found = replay.wait_for_browser("chromium", set(), 2)

        self.assertEqual(found[0]["title"], "Saved title")

    def test_capture_escapes_del_in_window_titles(self) -> None:
        parsed = tomllib.loads(capture.window_toml({"title": "Title\x7f"}))
        self.assertEqual(parsed["window"][0]["title"], "Title\x7f")

    def test_find_new_can_ignore_windows_from_other_apps(self) -> None:
        windows = [
            {"address": "0xwrong", "class": "org.gnome.Nautilus"},
            {"address": "0xright", "class": "google-chrome"},
        ]
        with patch.object(replay, "mapped", return_value=windows):
            self.assertEqual(
                replay.find_new(set(), 0.1, "google-chrome")["address"],
                "0xright",
            )

    def test_existing_browser_is_never_relaunched_for_a_missing_title(self) -> None:
        existing = [{
            "address": "0x1", "class": "google-chrome", "title": "Other",
            "workspace": {"id": 2},
        }]
        spec = [{"app_id": "google-chrome", "launch_cmd": "google-chrome",
                 "workspace": "5"}]
        with patch.object(replay, "mapped", return_value=existing), \
             patch.object(replay, "wait_for_existing_browser", return_value=existing), \
             patch.object(replay, "move_to") as move, \
             patch.object(replay, "dispatch") as dispatch:
            placed = replay.restore_browser(
                "google-chrome", spec,
                [{"class": "google-chrome", "title": "Saved", "workspace": "5"}],
                set(),
            )

        self.assertEqual(placed, 0)
        move.assert_not_called()
        dispatch.assert_not_called()

    def test_cold_browser_does_not_close_an_unmatched_blank_window(self) -> None:
        blank = [{"address": "0x1", "class": "chromium", "title": "New Tab"}]
        spec = [{"app_id": "chromium", "launch_cmd": "chromium", "workspace": "5"}]
        with patch.object(replay, "mapped", return_value=[]), \
             patch.object(replay, "arm_browser_profile", return_value=True), \
             patch.object(replay, "repair_browser_session") as repair, \
             patch.object(replay, "wait_for_browser", return_value=blank), \
             patch.object(replay, "dispatch") as dispatch, \
             patch.object(replay, "move_to") as move:
            placed = replay.restore_browser(
                "chromium", spec,
                [{"class": "chromium", "title": "Saved", "workspace": "5"}],
                set(),
            )

        self.assertEqual(placed, 1)
        self.assertFalse(any(call.args[0] == "window.close" for call in dispatch.call_args_list))
        move.assert_called_once_with("0x1", "5")
        repair.assert_called_once_with("chromium", 1)

    def test_app_filter_only_replays_the_requested_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last.toml"
            path.write_text(
                '[session]\nname = "last"\n\n'
                '[[window]]\napp_id = "com.mitchellh.ghostty"\n'
                'launch_cmd = "ghostty"\nworkspace = "2"\n\n'
                '[[window]]\napp_id = "foot"\n'
                'launch_cmd = "foot"\nworkspace = "1"\n',
                encoding="utf-8",
            )
            with patch.object(sys, "argv", ["replay.py", "--app",
                                             "com.mitchellh.ghostty", str(path)]), \
                 patch.object(replay, "restore", return_value=True) as restore, \
                 patch.object(replay, "mapped", return_value=[]):
                self.assertEqual(replay.main(), 0)
            self.assertEqual(restore.call_count, 1)
            self.assertEqual(restore.call_args.args[0]["app_id"],
                             "com.mitchellh.ghostty")

    def test_disabled_ghostty_only_session_is_an_intentional_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last.toml"
            path.write_text(
                '[session]\nname = "last"\n\n'
                '[[window]]\napp_id = "com.mitchellh.ghostty"\n'
                'launch_cmd = "ghostty"\nworkspace = "2"\n',
                encoding="utf-8",
            )
            with patch.object(sys, "argv", ["replay.py", str(path)]), \
                 patch.dict(replay.os.environ,
                            {"OMASESSION_APP_RESTORE_GHOSTTY": "false"}), \
                 patch.object(replay, "mapped", return_value=[]):
                self.assertEqual(replay.main(), 0)


if __name__ == "__main__":
    unittest.main()
