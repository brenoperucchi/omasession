#!/usr/bin/env python3
"""Layout and replay integration checks; never access a live compositor."""
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import replay as P
import tiled_layout as T


class Geometry(unittest.TestCase):
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
        # The same ratios should scale onto a differently sized monitor.
        self.assertEqual(T.projected_rects(tree, (100, 200, 300, 500))["b"],
                         [220, 200, 300, 290])

    def test_invalid_partitions_are_rejected(self):
        for rectangles in [
            [(0, 0, 100, 100), (10, 10, 110, 110)],  # overlap
            [(0, 0, 50, 50), (50, 50, 100, 100)],    # diagonal holes
            [(0, 0, 40, 100), (60, 0, 100, 100)],    # wide gap
            [(0, 0, 60, 100), (60, 0, 100, 40)],     # missing corner
            [(0, 0, 0, 100)],
            [(0, 0, float("nan"), 100)],
            [(0, 0, float("inf"), 100)],
        ]:
            with self.subTest(rectangles=rectangles), self.assertRaises(ValueError):
                T.split_tree([{"address": str(i), "rect": r} for i, r in enumerate(rectangles)])

    def test_rounding_is_allowed(self):
        tree = T.split_tree([{"address": "a", "rect": (0, 0, 51, 100)},
                             {"address": "b", "rect": (50, 0, 100, 100)}])
        self.assertAlmostEqual(tree["ratio"], 1.01)

    def test_empty_and_excessive_trees_are_rejected(self):
        for items in ([], [{"address": str(i), "rect": (i, 0, i + 1, 1)} for i in range(65)]):
            with self.assertRaises(ValueError):
                T.split_tree(items)


class Reconstruction(unittest.TestCase):
    def setUp(self):
        self.specs = [
            {"workspace": "9", "position": [7, 7], "size": [46, 86],
             "monitor": "DP-1", "_restored_address": "0xa"},
            {"workspace": "9", "position": [67, 7], "size": [26, 86],
             "monitor": "DP-1", "_restored_address": "0xb"},
        ]
        self.data = {
            "clients": [{"address": s["_restored_address"], "workspace": {"id": 9},
                         "mapped": True, "floating": False, "at": s["position"],
                         "size": s["size"]} for s in self.specs],
            "workspaces": [{"id": 9, "tiledLayout": "dwindle", "monitor": "DP-1"}],
            "monitors": [{"name": "DP-1"}],
            "activewindow": {"address": "0xf"},
            "activeworkspace": {"id": 1},
            "cursorpos": {"x": 20, "y": 30},
        }
        self.options = {"general:gaps_in": {"css": "5 5 5 5"},
                        "general:border_size": {"int": 2},
                        "dwindle:preserve_split": {"bool": True},
                        "dwindle:use_active_for_splits": {"bool": True}}
        self.query = self.enterContext(patch.object(T, "query", side_effect=self.data.__getitem__))
        self.enterContext(patch.object(T, "option", side_effect=self.options.__getitem__))
        self.hypr = Mock(return_value=T.SUCCESS)

    def run_restore(self):
        return T.restore_tiled(self.specs, self.hypr, P.lua)

    def test_complete_workspace_is_verified(self):
        result = self.run_restore()
        self.assertEqual((result.restored, result.skipped, result.failed), (2, 0, 0))
        self.assertGreaterEqual(self.query.call_args_list.count(unittest.mock.call("clients")), 2)
        script = self.hypr.call_args.args[0]
        self.assertIn(P.lua("address:0xf"), script)
        self.assertIn('hl.dsp.cursor.move({x=20,y=30})', script)

    def test_timeout_and_reported_error_are_failures_with_cleanup(self):
        for response in ("timeout", "error: rejected", "ok", ""):
            with self.subTest(response=response):
                self.hypr.reset_mock()
                self.hypr.side_effect = [response, T.SUCCESS]
                result = self.run_restore()
                self.assertEqual((result.restored, result.failed), (0, 1))
                cleanup = self.hypr.call_args.args[0]
                self.assertNotIn('action="on"', cleanup)
                self.assertIn('preselect none', cleanup)
                for addr in ("0xa", "0xb", "0xf"):
                    self.assertIn(P.lua("address:" + addr), cleanup)

    def test_raised_timeout_and_cleanup_failure_do_not_abort_other_work(self):
        self.hypr.side_effect = [subprocess.TimeoutExpired("hyprctl", 15), OSError("disconnected")]
        result = self.run_restore()
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.hypr.call_count, 2)

    def test_success_response_without_correct_geometry_is_not_success(self):
        self.data["clients"][0]["size"] = [25, 86]
        with patch.object(T.time, "monotonic", side_effect=[0, 2]):
            result = self.run_restore()
        self.assertEqual((result.restored, result.failed), (0, 1))

    def test_missing_or_extra_windows_are_untouched(self):
        for mode in ("missing-address", "duplicate-address", "missing-live", "extra-live"):
            with self.subTest(mode=mode):
                saved, live = copy.deepcopy(self.specs), copy.deepcopy(self.data["clients"])
                if mode == "missing-address":
                    del self.specs[0]["_restored_address"]
                elif mode == "duplicate-address":
                    self.specs[1]["_restored_address"] = "0xa"
                elif mode == "missing-live":
                    self.data["clients"].pop()
                else:
                    self.data["clients"].append({**live[0], "address": "0xextra"})
                result = self.run_restore()
                self.assertEqual(result.skipped, 1)
                self.hypr.assert_not_called()
                self.specs, self.data["clients"] = saved, live

    def test_unsupported_settings_are_untouched(self):
        for key, value in (("general:gaps_in", {"css": "5 6 5 6"}),
                           ("dwindle:preserve_split", {"bool": False}),
                           ("dwindle:use_active_for_splits", {"bool": False})):
            with self.subTest(key=key):
                previous = self.options[key]
                self.options[key] = value
                self.assertEqual(self.run_restore().skipped, 1)
                self.hypr.assert_not_called()
                self.options[key] = previous

    def test_non_dwindle_workspace_is_untouched(self):
        self.data["workspaces"][0]["tiledLayout"] = "master"
        self.assertEqual(self.run_restore().skipped, 1)
        self.hypr.assert_not_called()

    def test_unsupported_window_states_are_untouched(self):
        for key, value in (("fullscreen", 2), ("hidden", True), ("grouped", ["0xa"]), ("pinned", True)):
            with self.subTest(key=key):
                self.data["clients"][0][key] = value
                self.assertEqual(self.run_restore().skipped, 1)
                self.hypr.assert_not_called()
                del self.data["clients"][0][key]

    def test_invalid_saved_geometry_is_untouched(self):
        self.specs[1]["position"] = [67, 50]
        self.assertEqual(self.run_restore().skipped, 1)
        self.hypr.assert_not_called()

    def test_missing_monitor_uses_current_monitor(self):
        for spec in self.specs:
            spec["monitor"] = "unplugged"
        self.assertEqual(self.run_restore().restored, 2)
        self.assertNotIn("workspace.move", self.hypr.call_args.args[0])

    def test_monitor_move_does_not_displace_unrelated_floating_windows(self):
        self.data["workspaces"][0]["monitor"] = "DP-2"
        self.data["clients"].append({**self.data["clients"][0], "address": "0xextra", "floating": True})
        self.assertEqual(self.run_restore().skipped, 1)
        self.hypr.assert_not_called()

    def test_empty_focused_workspace_is_restored(self):
        self.data["activewindow"] = {}
        self.assertEqual(self.run_restore().restored, 2)
        self.assertIn("hl.dsp.focus({workspace=1})", self.hypr.call_args.args[0])

    def test_query_failure_leaves_desktop_untouched(self):
        self.query.side_effect = OSError("no compositor")
        self.assertEqual(self.run_restore().skipped, 1)
        self.hypr.assert_not_called()


class ReplayAssociation(unittest.TestCase):
    def test_terminal_exact_titles_reserved_before_class_fallback(self):
        specs = [{"app_id": "foot", "title": "changed"}, {"app_id": "foot", "title": "project"}]
        live = [{"class": "foot", "title": "project", "address": "0xa"},
                {"class": "foot", "title": "other", "address": "0xb"}]
        P.reserve_existing(specs, live)
        self.assertEqual([s["_adopt_address"] for s in specs], ["0xb", "0xa"])
        with patch.object(P, "mapped", return_value=live), patch.object(P, "place"):
            for i, spec in enumerate(specs):
                self.assertTrue(P.restore(spec, set(), i, 2))
        self.assertEqual([s["_restored_address"] for s in specs], ["0xb", "0xa"])

    def test_new_window_address_is_recorded(self):
        spec = {"app_id": "foot", "launch_cmd": "foot"}
        with patch.object(P, "mapped", return_value=[]), patch.object(P, "dispatch"), \
             patch.object(P, "find_new", return_value={"address": "0xa"}), patch.object(P, "place"):
            self.assertTrue(P.restore(spec, set(), 1, 1))
        self.assertEqual(spec["_restored_address"], "0xa")

    def test_browser_matching_retains_geometry_and_exact_match_priority(self):
        specs = [{"app_id": "chromium", "title": title, "workspace": "9", "size": [i + 10, 20]}
                 for i, title in enumerate(("Report", "Report draft copy"))]
        titles = [{"class": "chromium", "title": s["title"], "workspace": "9"} for s in specs]
        live = [{"class": "chromium", "title": "Report draft", "address": "0xa"},
                {"class": "chromium", "title": "Report", "address": "0xb"}]
        with patch.object(P, "mapped", return_value=live), \
             patch.object(P, "wait_for_existing_browser", return_value=live), patch.object(P, "move_to"), \
             patch.object(P, "dispatch") as dispatch:
            self.assertEqual(P.restore_browser("chromium", specs, titles, set()), 2)
        dispatch.assert_not_called()
        self.assertEqual([s["_restored_address"] for s in specs], ["0xb", "0xa"])

    def test_duplicate_titles_link_distinct_specs(self):
        specs = [{"app_id": "chromium", "title": "New Tab", "workspace": "9"} for _ in range(2)]
        titles = [{"class": "chromium", "title": "New Tab", "workspace": 9} for _ in range(2)]
        targets = P.browser_targets("chromium", specs, titles)
        self.assertIs(targets[0]["_spec"], specs[0])
        self.assertIs(targets[1]["_spec"], specs[1])

    def test_legacy_sidecar_links_by_workspace_without_reordering(self):
        specs = [{"app_id": "chromium", "workspace": "2"}, {"app_id": "chromium", "workspace": "3"}]
        titles = [{"class": "chromium", "title": "A", "workspace": 3},
                  {"class": "chromium", "title": "B", "workspace": 2}]
        targets = P.browser_targets("chromium", specs, titles)
        self.assertIs(targets[0]["_spec"], specs[1])
        self.assertIs(targets[1]["_spec"], specs[0])

    def test_inconsistent_sidecar_cannot_authorize_geometry(self):
        specs = [{"app_id": "chromium", "workspace": "2", "title": "A"}]
        targets = P.browser_targets("chromium", specs, [{"class": "chromium", "workspace": 2, "title": "B"}])
        self.assertNotIn("_spec", targets[0])

    def test_layout_failure_sets_partial_restore_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "last.toml"
            path.touch()
            specs = [{"app_id": "foot", "workspace": "9"}]
            with patch.object(sys, "argv", ["replay.py", str(path)]), \
                 patch.object(P, "load_pair", return_value=(specs, [], path)), \
                 patch.object(P, "mapped", return_value=[]), \
                 patch.object(P, "restore", return_value=True), \
                 patch.object(P, "restore_tiled", return_value=T.LayoutResult(failed=1)):
                self.assertEqual(P.main(), 2)

    def test_app_filter_retains_other_saved_tiles_for_completeness(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "last.toml"
            path.touch()
            specs = [{"app_id": "foot", "workspace": "9"},
                     {"app_id": "chromium", "workspace": "9"},
                     {"app_id": "chromium", "workspace": "8"}]
            with patch.object(sys, "argv", ["replay.py", "--app", "foot", str(path)]), \
                 patch.object(P, "load_pair", return_value=(specs, [], path)), \
                 patch.object(P, "mapped", return_value=[]), \
                 patch.object(P, "restore", return_value=True) as restore, \
                 patch.object(P, "restore_tiled", return_value=T.LayoutResult(skipped=1)) as layout:
                self.assertEqual(P.main(), 0)
                self.assertEqual(restore.call_count, 1)
                self.assertEqual(layout.call_args.args[0], specs[:2])


if __name__ == "__main__":
    unittest.main()
