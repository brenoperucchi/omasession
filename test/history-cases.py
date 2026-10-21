"""Behavioral contracts for the history of `last` and the session list."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import history  # noqa: E402

NOW = 2_000_000_000.0


def toml(windows: list[tuple[str, str, int, str]]) -> str:
    out = ['[session]\nname = "last"\n']
    for app, ws, order, title in windows:
        out.append(f'[[window]]\napp_id = "{app}"\ntitle = "{title}"\n'
                   f'launch_cmd = "{app}"\nworkspace = "{ws}"\norder = {order}\n')
    return "\n".join(out)


def stem(seconds_ago: float) -> str:
    return "history-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(NOW - seconds_ago))


class ThinningCases(unittest.TestCase):
    def test_tiers_keep_every_recent_change_then_one_per_bucket(self) -> None:
        ages = [60, 300, 600,            # last 15 min: all kept
                1000, 1100,              # same 15-min bucket: one kept
                3 * 3600, 3 * 3600 + 60,  # same hour: one kept
                2 * 86400, 2 * 86400 + 60,  # same day: one kept
                8 * 86400]               # older than a week: dropped
        snaps = sorted(((NOW - a, stem(a)) for a in ages), reverse=True)
        kept = history.keep(snaps, NOW)
        self.assertEqual(len(kept), 3 + 1 + 1 + 1)
        self.assertIn(stem(60), kept)
        self.assertNotIn(stem(8 * 86400), kept)
        self.assertIn(stem(1000), kept)          # newest of its bucket
        self.assertNotIn(stem(1100), kept)

    def test_the_fullest_snapshot_of_a_bucket_survives_a_drop(self) -> None:
        # Within one hour bucket: 20 windows, then a crash left 3. The newest
        # (3) is kept as always; the 20 is kept too.
        full, after = stem(3 * 3600 + 60), stem(3 * 3600)
        snaps = [(NOW - 3 * 3600, after), (NOW - 3 * 3600 - 60, full)]
        kept = history.keep(snaps, NOW, {full: 20, after: 3})
        self.assertEqual(kept, {full, after})
        self.assertEqual(history.keep(snaps, NOW, {full: 3, after: 20}), {after})

    def test_stamp_round_trips(self) -> None:
        self.assertAlmostEqual(history.stamp_time(stem(120) + ".toml"), NOW - 120, delta=1)
        self.assertIsNone(history.stamp_time("work.toml"))

    def test_a_stamp_that_is_no_date_is_ignored(self) -> None:
        self.assertIsNone(history.stamp_time("history-20261301-000000.toml"))


class SnapshotCases(unittest.TestCase):
    def setUp(self) -> None:
        base = Path.home() / ".cache"
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.dir = Path(self.tmp.name)
        (self.dir / "last.titles.json").write_text("[]")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_last(self, windows) -> None:
        (self.dir / "last.toml").write_text(toml(windows))

    def test_title_changes_do_not_add_history_but_layout_changes_do(self) -> None:
        self.write_last([("ghostty", "1", 0, "song A")])
        self.assertIsNotNone(history.snapshot(self.dir, NOW))
        self.write_last([("ghostty", "1", 0, "song B")])          # title only
        self.assertIsNone(history.snapshot(self.dir, NOW + 30))
        self.write_last([("ghostty", "2", 0, "song B")])          # moved workspace
        self.assertIsNotNone(history.snapshot(self.dir, NOW + 60))
        self.assertEqual(len(history.snapshots(self.dir)), 2)

    def test_empty_last_keeps_no_history(self) -> None:
        (self.dir / "last.toml").write_text('[session]\nname = "last"\n')
        self.assertIsNone(history.snapshot(self.dir, NOW))

    def test_listing_separates_named_sessions_from_internal_files(self) -> None:
        self.write_last([("ghostty", "1", 0, "t")])
        history.snapshot(self.dir, NOW)
        (self.dir / "work.toml").write_text(toml([("brave", "1", 0, "t"), ("foot", "2", 0, "t")]))
        (self.dir / "last.prev.toml").write_text(toml([]))
        data = history.listing(self.dir, NOW + 10)
        self.assertEqual([r["name"] for r in data["named"]], ["work"])
        self.assertEqual(data["named"][0]["windows"], 2)
        self.assertEqual(len(data["history"]), 1)


    def test_a_bad_history_name_does_not_break_list_or_snapshot(self) -> None:
        (self.dir / "history-20261301-000000.toml").write_text(toml([]))
        self.write_last([("ghostty", "1", 0, "t")])
        self.assertIsNotNone(history.snapshot(self.dir, NOW))
        self.assertEqual(len(history.listing(self.dir, NOW)["history"]), 1)

if __name__ == "__main__":
    unittest.main()
