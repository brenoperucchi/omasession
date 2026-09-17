#!/usr/bin/env python3
"""Regression checks that never open or change desktop windows."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import resolve as R
import replay as P
import tiled_layout as T


class Folders(unittest.TestCase):
    def resolve_cwd(self, title, folders):
        children = [str(i + 10) for i in range(len(folders))]
        with patch.object(R, "_children", side_effect=lambda p: children if str(p) == "1" else []), \
             patch.object(R, "_comm", return_value="bash"), \
             patch.object(R.os, "readlink", side_effect=lambda p: folders[int(p.split('/')[2]) - 10]):
            return R.child_cwd(1, title)

    def test_distinct_window_folders(self):
        self.assertEqual(self.resolve_cwd("Task | api", ["/work/api", "/work/site"])[0], "/work/api")

    def test_ambiguous_basename_is_not_guessed(self):
        self.assertIsNone(self.resolve_cwd("Task | api", ["/one/api", "/two/api"])[0])

    def test_unknown_title_is_not_guessed(self):
        self.assertIsNone(self.resolve_cwd("Untitled", ["/one", "/two"])[0])

    def test_same_directory_is_valid_for_shared_process(self):
        self.assertEqual(self.resolve_cwd("Untitled", ["/work", "/work"])[0], "/work")

    def test_ghostty_restore_uses_independent_process_and_safe_folder_argument(self):
        win = {"class": "com.mitchellh.ghostty", "pid": 1, "title": "Task"}
        index = {win["class"]: {"Exec": "/usr/bin/ghostty --gtk-single-instance=true", "_path": "test.desktop"}}
        with patch.object(R, "child_cwd", return_value=("/work/a space;literal", "shell")):
            result = R.resolve(win, index)
        self.assertIn("--gtk-single-instance=false", result["argv"])
        self.assertNotIn("--gtk-single-instance=true", result["argv"])
        self.assertIn("--working-directory=/work/a space;literal", result["argv"])

    def test_ghostty_preserves_env_wrapper_and_child_arguments(self):
        argv = ['env', 'TEST=1', 'ghostty', '--gtk-single-instance=true',
                '--working-directory=/old', '-e', 'tool', '--working-directory=/child']
        self.assertEqual(R.ghostty_argv(argv, '/new'),
                         ['env', 'TEST=1', 'ghostty', '--gtk-single-instance=false',
                          '--working-directory=/new', '-e', 'tool', '--working-directory=/child'])


class Browser(unittest.TestCase):
    def test_requests_last_session_and_removes_blank_window_flag(self):
        command = P.browser_command("chromium --new-window --profile-directory='Profile 2'")
        self.assertIn("--restore-last-session", command)
        self.assertNotIn("--new-window", command)
        self.assertIn("Profile 2", command)

    def test_live_browser_is_neither_closed_nor_relaunched(self):
        spec = {"app_id": "chromium", "workspace": "2", "title": "Old page"}
        live = {"class": "chromium", "address": "0x12", "title": "New Tab"}
        with patch.object(P, "mapped", return_value=[live]), \
             patch.object(P, "arm_browser_profile") as arm, \
             patch.object(P, "dispatch") as dispatch, \
             patch.object(P, "place"):
            self.assertEqual(P.restore_browser("chromium", [spec], [], set()), 1)
        arm.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(spec["_restored_address"], "0x12")

    def test_missing_browser_windows_do_not_create_empty_replacements(self):
        spec = {"app_id": "chromium", "workspace": "2", "title": "Old page"}
        with patch.object(P, "mapped", return_value=[]), \
             patch.object(P, "arm_browser_profile", return_value=True), \
             patch.object(P, "wait_for_browser", return_value=[]), \
             patch.object(P, "dispatch") as dispatch:
            self.assertEqual(P.restore_browser("chromium", [spec], [], set()), 0)
        self.assertEqual(dispatch.call_count, 1)
        self.assertIn("--restore-last-session", dispatch.call_args.args[1])
        self.assertNotIn("--new-window", dispatch.call_args.args[1])


class Layout(unittest.TestCase):
    def test_nested_unequal_split(self):
        tree = T.split_tree([
            {"address": "a", "rect": (0, 0, 60, 100)},
            {"address": "b", "rect": (60, 0, 100, 30)},
            {"address": "c", "rect": (60, 30, 100, 100)},
        ])
        self.assertEqual(tree["axis"], 0)
        self.assertAlmostEqual(tree["ratio"], 1.2)
        self.assertEqual(tree["right"]["axis"], 1)
        self.assertAlmostEqual(tree["right"]["ratio"], 0.6)

    def test_overlapping_rectangles_are_rejected(self):
        with self.assertRaises(ValueError):
            T.split_tree([{"address": "a", "rect": (0, 0, 100, 100)},
                          {"address": "b", "rect": (10, 10, 110, 110)}])

    def test_rounding_tolerance(self):
        tree = T.split_tree([{"address": "a", "rect": (0, 0, 51, 100)},
                             {"address": "b", "rect": (50, 0, 100, 100)}])
        self.assertAlmostEqual(tree["ratio"], 1.01)


if __name__ == "__main__":
    unittest.main()
