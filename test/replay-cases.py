#!/usr/bin/env python3
"""Behavioral contracts for browser title matching and session repair."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import replay  # noqa: E402


class ReplayCases(unittest.TestCase):
    def test_exact_title_is_reserved_before_approximate_match(self) -> None:
        windows = [
            {"title": "Report draft", "address": "0xa"},
            {"title": "Report", "address": "0xb"},
        ]
        candidates = [
            {"title": "Report", "workspace": "2"},
            {"title": "Report draft copy", "workspace": "3"},
        ]

        matches, remaining = replay.plan_browser_matches(windows, candidates)

        self.assertEqual(matches[1]["workspace"], "2")
        self.assertEqual(matches[0]["workspace"], "3")
        self.assertEqual(remaining, [])


class RepairBrowserSession(unittest.TestCase):
    """The session repair of docs/plans/008 never costs the rest of the restore."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        profile = root / "chrome" / "Default"
        profile.mkdir(parents=True)
        self.browsers = patch.dict(replay.BROWSERS, {"google-chrome": {
            **replay.BROWSERS["google-chrome"],
            "profile": str(profile / "Preferences")}})
        self.browsers.start()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        self.env.start()
        self.sessions = profile / "Sessions"
        self.backups = root / "state" / "omasession" / "browser-sessions" / "google-chrome"

    def tearDown(self) -> None:
        self.env.stop()
        self.browsers.stop()
        self.tmp.cleanup()

    def run_repair(self, app: str = "google-chrome", running: bool = False):
        out = io.StringIO()
        with patch.object(replay, "profile_in_use", return_value=running), \
             patch.object(replay, "repair_sessions", return_value="ok") as repair, \
             contextlib.redirect_stdout(out):
            replay.repair_browser_session(app, 4)
        return repair, out.getvalue()

    def test_repairs_the_profile_sessions_into_the_state_backups(self) -> None:
        repair, out = self.run_repair()

        repair.assert_called_once_with(self.sessions, self.backups, 4)
        self.assertIn("google-chrome session: ok", out)

    def test_unmeasured_browsers_are_not_touched(self) -> None:
        repair, out = self.run_repair("brave")

        repair.assert_not_called()
        self.assertEqual(out, "")

    def test_a_running_browser_is_left_alone(self) -> None:
        repair, out = self.run_repair(running=True)

        repair.assert_not_called()
        self.assertIn("the browser is running", out)

    def test_any_error_becomes_a_log_line(self) -> None:
        out = io.StringIO()
        with patch.object(replay, "profile_in_use", return_value=False), \
             patch.object(replay, "repair_sessions", side_effect=ValueError("boom")), \
             contextlib.redirect_stdout(out):
            replay.repair_browser_session("google-chrome", 4)

        self.assertIn("left as is: ValueError: boom", out.getvalue())


if __name__ == "__main__":
    unittest.main()
