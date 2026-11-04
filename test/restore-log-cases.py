#!/usr/bin/env python3
"""Every restore that really ran leaves a log a later restore cannot erase."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import replay  # noqa: E402
import restore_log  # noqa: E402

NOW = 1793470600.0  # 2026-10-31, a login like the one whose log was lost


_tick = [0]


def stamp_after(path: Path) -> None:
    """Give `path` a modification time later than any stamped before it."""
    _tick[0] += 1
    t = int(NOW * 1e9) + _tick[0] * 1_000_000
    os.utime(path, ns=(t, t))


class Keeping(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state" / "omasession"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, trigger: str, text: str, now: float, keep: int = restore_log.KEEP):
        log = restore_log.RestoreLog(self.state, trigger, now=now, keep=keep)
        log.publish(text)
        log.close()
        # Real restores are seconds apart; back-to-back test writes can land in
        # the same filesystem clock tick, so each one is stamped explicitly.
        stamp_after(self.state / log.name)
        return log

    def test_a_manual_restore_does_not_erase_the_login_one(self) -> None:
        self.write("login", "login restore\n", NOW)
        self.write("manual", "manual restore\n", NOW + 16 * 60)

        logs = sorted((self.state / "restores").glob("*.log"))
        self.assertEqual([p.read_text() for p in logs],
                         ["login restore\n", "manual restore\n"])
        self.assertTrue(logs[0].name.endswith("-login.log"))
        self.assertEqual((self.state / "last-restore.log").read_text(), "manual restore\n")

    def test_two_restores_in_one_second_both_stay(self) -> None:
        self.write("manual", "first\n", NOW)
        second = self.write("manual", "second\n", NOW)

        self.assertTrue(second.name.endswith("-manual-02.log"))
        self.assertEqual(len(list((self.state / "restores").glob("*.log"))), 2)

    def test_eleven_restores_in_one_second_are_pruned_oldest_first(self) -> None:
        for i in range(11):
            self.write("manual", f"restore {i}\n", NOW, keep=3)

        kept = sorted(p.read_text() for p in (self.state / "restores").glob("*.log"))
        self.assertEqual(kept, ["restore 10\n", "restore 8\n", "restore 9\n"])

    def test_only_the_newest_restores_stay_with_their_session_copies(self) -> None:
        session = Path(self.tmp.name) / "last.toml"
        session.write_text("[session]\n")
        for i in range(4):
            log = restore_log.RestoreLog(self.state, "manual", now=NOW + i, keep=2)
            log.publish(f"restore {i}\n")
            log.keep_session(session, None)
            log.close()
            stamp_after(self.state / log.name)

        names = sorted(p.name for p in (self.state / "restores").iterdir())
        self.assertEqual(len(names), 4)  # two logs, two session copies
        self.assertEqual([(self.state / "restores" / n).read_text()
                          for n in names if n.endswith(".log")],
                         ["restore 2\n", "restore 3\n"])

    def test_restores_of_different_triggers_in_one_second_keep_their_order(self) -> None:
        self.write("manual", "manual\n", NOW)
        newer = self.write("app", "app\n", NOW)
        # Names alone would put `-app` before `-manual`.
        stamp_after(self.state / newer.name)
        log = restore_log.RestoreLog(self.state, "login", now=NOW, keep=2)
        log.publish("login\n")
        log.close()

        kept = {p.read_text() for p in (self.state / "restores").glob("*.log")}
        self.assertEqual(kept, {"app\n", "login\n"})
        self.assertTrue((self.state / newer.name).exists())

    def test_files_that_are_not_restore_logs_are_left_alone(self) -> None:
        (self.state / "restores").mkdir(parents=True)
        (self.state / "restores" / "notes.txt").write_text("mine")
        self.write("manual", "x\n", NOW, keep=1)
        self.write("manual", "y\n", NOW + 1, keep=1)

        self.assertTrue((self.state / "restores" / "notes.txt").exists())

    def test_a_cleanup_that_cannot_finish_keeps_the_log(self) -> None:
        stuck = self.state / "restores" / "20260101-000000-manual.log"
        stuck.mkdir(parents=True)  # a directory under a log's name: unlink fails
        log = restore_log.RestoreLog(self.state, "manual", now=NOW, keep=1)
        log.publish("first line\n")
        log.publish("first line\nsecond line\n")
        log.close()
        self.assertEqual((self.state / "last-restore.log").read_text(), "first line\nsecond line\n")
        self.assertEqual((self.state / log.name).read_text(), "first line\nsecond line\n")

    def test_an_unwritable_state_dir_never_fails_the_restore(self) -> None:
        blocker = Path(self.tmp.name) / "state"
        blocker.write_text("a file where the directory should be")
        stderr = io.StringIO()
        with patch.object(sys, "__stderr__", stderr):
            log = restore_log.RestoreLog(self.state, "login", now=NOW)
            log.publish("anything\n")
            log.keep_session(blocker, None)
            log.close()
        self.assertIsNone(log.name)
        self.assertIn("restore log unavailable", stderr.getvalue())


class Context(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.session = self.state / "last.toml"
        self.session.write_text("[session]\n")
        self.windows = [{"app_id": "foot"}, {"app_id": "google-chrome"}, {"app_id": "foot"}]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_header_says_what_was_replayed_and_the_last_save(self) -> None:
        (self.state / "last-save.json").write_text(json.dumps(
            {"when": "2026-10-31T16:50:01Z", "rc": 0, "message": "session-save: last -- 22 window(s)"}))
        with patch.dict(os.environ, {"OMASESSION_RESTORE_ORDER": "auto"}):
            lines = restore_log.context_lines("manual", self.session, self.windows, self.state,
                                              "restores/x.log")
        text = "\n".join(lines)
        self.assertIn("trigger: manual", text)
        self.assertIn("3 window(s): 2 foot, 1 google-chrome", text)
        self.assertIn("last save: 2026-10-31T16:50:01Z rc=0", text)
        self.assertIn("restore_order=auto", text)
        self.assertNotIn("previous boot", text)

    def test_a_login_after_the_menu_shutdown_says_so(self) -> None:
        calls = []

        def journal(args):
            calls.append(args)
            if "-g" not in args:
                return "1793465420.427000 omarchy systemd[1]: Unmounting /boot..."
            if "--user" in args:
                return ("2026-10-31T13:50:07-0300 omarchy systemd[1450]: Started [systemd-run] "
                        "/usr/share/omarchy/bin/omarchy-system-shutdown --inhibit.")
            return ("2026-10-31T13:50:09-0300 omarchy systemd-logind[1330]: poweroff "
                    "requested from client PID 4103281 ('systemctl')")

        text = "\n".join(restore_log.previous_boot_lines(journal))
        self.assertIn("previous boot: last entry 2026-10-31", text)
        self.assertIn("omarchy-system-shutdown", text)
        self.assertIn("poweroff requested", text)
        # Both pattern searches stay inside the boot's last minutes: a search
        # that finds nothing must not scan an 11-day boot under the lock.
        for args in calls[1:]:
            self.assertIn(f"@{1793465420 - restore_log.SHUTDOWN_WINDOW}", args)

    def test_a_boot_that_ended_without_a_shutdown_is_called_out(self) -> None:
        def journal(args):
            if "-g" in args:
                return ""
            if "systemd-logind" in args:
                return "2026-10-31T09:00:00-0300 omarchy systemd-logind[1]: New seat seat0."
            return "1793465420.4 kernel: x"

        lines = restore_log.previous_boot_lines(journal)
        self.assertIn("crash or power loss", "\n".join(lines))

    def test_an_unreadable_journal_is_reported_not_raised(self) -> None:
        self.assertEqual(restore_log.previous_boot_lines(lambda args: None),
                         ["previous boot: journal unreadable"])

    def test_a_system_journal_this_user_cannot_read_is_not_called_a_crash(self) -> None:
        # Under -q, an unreadable system journal answers like an empty search.
        def journal(args):
            if "--user" in args:
                return ""
            return "1793465420.4 kernel: x" if args == ["-b", "-1", "-o", "short-unix"] else ""

        text = "\n".join(restore_log.previous_boot_lines(journal))
        self.assertIn("logind: journal unreadable", text)
        self.assertNotIn("crash", text)

    def test_a_slow_journal_is_reported_as_such(self) -> None:
        self.assertEqual(restore_log.previous_boot_lines(lambda args: restore_log.TIMED_OUT),
                         [f"previous boot: gave up after {restore_log.JOURNAL_TIMEOUT}s"])

    def test_an_unreadable_logind_is_not_called_a_crash(self) -> None:
        def journal(args):
            if "-g" not in args:
                return "1793465420.4 kernel: x"
            return "" if "--user" in args else None

        text = "\n".join(restore_log.previous_boot_lines(journal))
        self.assertIn("logind: journal unreadable", text)
        self.assertNotIn("crash", text)


class JournalAnswers(unittest.TestCase):
    """What journalctl's exit codes mean, as _first_journal_line reads them."""

    def answer(self, rc=0, stdout="", stderr="", raises=None):
        def run(*_a, **_k):
            if raises:
                raise raises
            return subprocess.CompletedProcess([], rc, stdout, stderr)
        with patch.object(restore_log.subprocess, "run", run):
            return restore_log._first_journal_line(["-g", "x"])

    def test_no_match_is_empty(self) -> None:
        self.assertEqual(self.answer(rc=1), "")

    def test_a_refusal_is_unreadable(self) -> None:
        self.assertIsNone(self.answer(rc=1, stderr="No journal files were opened due to insufficient permissions."))
        self.assertIsNone(self.answer(rc=2))

    def test_a_timeout_is_its_own_answer(self) -> None:
        self.assertIs(self.answer(raises=subprocess.TimeoutExpired("journalctl", 5)),
                      restore_log.TIMED_OUT)

    def test_a_match_is_the_first_line(self) -> None:
        self.assertEqual(self.answer(stdout="a\nb\n"), "a")


class BrowserProfile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "Default"
        (self.profile / "Sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_shows_the_exit_state_and_session_files_before_the_restore_edits_them(self) -> None:
        (self.profile / "Preferences").write_text(json.dumps(
            {"profile": {"exit_type": "SessionEnded", "exited_cleanly": True}}))
        (self.profile / "Sessions" / "Session_1").write_bytes(b"x" * 10)

        lines = restore_log.profile_lines(self.profile / "Preferences",
                                          self.profile / "Sessions", lambda _p: 1)

        self.assertEqual(lines[0], "exit_type=SessionEnded exited_cleanly=True")
        self.assertIn("Session_1 (10 B,", lines[1])
        self.assertEqual(lines[2], "windows in the newest session: 1")

    def test_an_unreadable_profile_is_a_finding_not_a_failure(self) -> None:
        def broken(_p):
            raise ValueError("bad record")

        lines = restore_log.profile_lines(self.profile / "Preferences",
                                          self.profile / "Sessions", broken)

        self.assertEqual(lines, ["Preferences unreadable: FileNotFoundError",
                                 "Sessions: none",
                                 "windows in the newest session: unreadable (ValueError)"])


class ProfileRace(unittest.TestCase):
    def test_a_session_file_rotated_away_mid_listing_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "Sessions"
            sessions.mkdir()
            (sessions / "Session_1").write_bytes(b"x")
            (sessions / "Session_2").write_bytes(b"y")
            real_stat = Path.stat

            def stat(self, *a, **k):
                if self.name == "Session_2" and not k and not a:
                    raise FileNotFoundError(self)
                return real_stat(self, *a, **k)

            with patch.object(Path, "stat", stat):
                lines = restore_log.profile_lines(Path(tmp) / "Preferences", sessions,
                                                  lambda _p: 1)
        self.assertIn("Session_2 (gone)", lines[1])


class TerminalLine(unittest.TestCase):
    """The log tells a bad save from a bad restore for every terminal."""

    def restore(self, spec: dict) -> str:
        out = io.StringIO()
        with patch.object(replay, "mapped", return_value=[]), \
             patch.object(replay, "dispatch"), \
             patch.object(replay, "find_new", return_value={"address": "0x1"}), \
             patch.object(replay, "place"), contextlib.redirect_stdout(out):
            replay.restore(spec, set(), 2, 14)
        return out.getvalue()

    def test_a_saved_folder_and_program(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            line = self.restore({"app_id": "foot", "workspace": "3", "cwd": cwd,
                                 "launch_cmd": "foot -- claude --resume"})
        self.assertIn(f"restored → ws3  (cwd {cwd}; runs claude)", line)

    def test_a_folder_that_is_gone_and_no_program(self) -> None:
        line = self.restore({"app_id": "foot", "workspace": "3", "cwd": "/nonexistent/x",
                             "launch_cmd": "foot"})
        self.assertIn("(cwd /nonexistent/x (gone); no program saved)", line)

    def test_no_folder_saved(self) -> None:
        line = self.restore({"app_id": "foot", "workspace": "3", "launch_cmd": "foot"})
        self.assertIn("(no cwd saved; no program saved)", line)

    def test_other_apps_get_no_terminal_detail(self) -> None:
        line = self.restore({"app_id": "org.gnome.Nautilus", "workspace": "6",
                             "launch_cmd": "nautilus"})
        self.assertTrue(line.rstrip().endswith("restored → ws6"))


class EndToEnd(unittest.TestCase):
    def test_a_restore_that_finds_nothing_still_leaves_its_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "last.toml"
            session.write_text("[session]\n")
            env = {**os.environ, "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "OMASESSION_RESTORE_TRIGGER": "login"}
            env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
            subprocess.run([sys.executable, str(ROOT / "lib" / "replay.py"), str(session)],
                           env=env, capture_output=True, text=True, timeout=60)
            restores = Path(tmp) / "state" / "omasession" / "restores"
            logs = list(restores.glob("*-login.log"))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text()
            self.assertIn("trigger: login", text)
            self.assertIn("nothing to restore", text)
            # A login log ends with how the previous boot went down, read
            # only after the restore so a slow journal never delays it.
            self.assertIn("previous boot:", text.split("nothing to restore", 1)[1])
            self.assertTrue(list(restores.glob("*-login.session.toml")))


    def test_the_log_keeps_the_generation_that_was_replayed_not_the_rejected_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            win = '[[window]]\napp_id = "nosuch.app"\nworkspace = "1"\n'
            (sessions / "last.toml").write_text(f'{win}[omasession]\ngeneration = "B"\n')
            (sessions / "last.titles.json").write_text('{"generation": "A", "windows": []}')
            (sessions / "last.prev.toml").write_text(f'{win}{win}[omasession]\ngeneration = "A"\n')
            (sessions / "last.prev.titles.json").write_text(
                '{"generation": "A", "windows": [{"class": "nosuch.app", "title": "t"}]}')
            env = {**os.environ, "XDG_STATE_HOME": str(sessions / "state"),
                   "OMASESSION_RESTORE_TRIGGER": "app"}
            subprocess.run([sys.executable, str(ROOT / "lib" / "replay.py"), "--app", "other.app",
                            str(sessions / "last.toml")],
                           env=env, capture_output=True, text=True, timeout=60)
            restores = sessions / "state" / "omasession" / "restores"
            log = next(restores.glob("*-app.log")).read_text()
            self.assertIn("last.prev.toml", log.split("\nsession: ", 1)[1].splitlines()[0])
            self.assertIn("fallback: last.toml", log)
            self.assertEqual(next(restores.glob("*.session.toml")).read_text(),
                             (sessions / "last.prev.toml").read_text())
            self.assertEqual(next(restores.glob("*.titles.json")).read_text(),
                             (sessions / "last.prev.titles.json").read_text())


class LoginEpilogue(unittest.TestCase):
    """The previous-boot lookup runs after a finished restore, never on the way out."""

    def run_logged(self, main) -> list:
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"XDG_STATE_HOME": tmp, "OMASESSION_RESTORE_TRIGGER": "login"}), \
             patch.object(replay, "main", main), \
             patch.object(restore_log, "previous_boot_lines",
                          lambda *a: calls.append(1) or ["previous boot: x"]), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                calls.append(("rc", replay.run_logged()))
            except SystemExit as exc:
                calls.append(("exit", exc.code))
        return calls

    def test_a_finished_login_restore_reads_how_the_last_boot_ended(self) -> None:
        self.assertEqual(self.run_logged(lambda: 2), [1, ("rc", 2)])

    def test_a_restore_stopped_by_sigterm_does_not(self) -> None:
        def stopped():
            raise SystemExit(143)

        self.assertEqual(self.run_logged(stopped), [("exit", 143)])


class Report(unittest.TestCase):
    def test_a_boot_that_did_not_go_down_through_the_menu_still_gets_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The CLI pins PATH, so the journal that matches nothing comes in
            # as a function: exit 1 and no output, as `journalctl -g` does.
            hook = Path(tmp) / "journal.sh"
            hook.write_text("journalctl() { return 1; }\n")
            out = Path(tmp) / "report"
            env = {**os.environ, "BASH_ENV": str(hook), "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "XDG_CONFIG_HOME": str(Path(tmp) / "config")}
            result = subprocess.run(["bash", str(ROOT / "bin" / "omasession"), "report", str(out)],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("report written to", result.stdout + result.stderr)
            journal = (out / "journal.txt").read_text()
            self.assertEqual(journal.count("\n== "), 2)
            self.assertTrue(journal.startswith("== this boot"))
            self.assertEqual(journal.count("(no match)"), 4)

    def test_the_report_takes_the_latest_of_two_restores_in_one_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state" / "omasession"
            for trigger, text in (("manual", "first\n"), ("app", "second\n")):
                log = restore_log.RestoreLog(state, trigger, now=NOW)
                log.publish(text)
                log.close()
                stamp_after(state / log.name)
            hook = Path(tmp) / "journal.sh"
            hook.write_text("journalctl() { return 1; }\n")
            out = Path(tmp) / "report"
            env = {**os.environ, "BASH_ENV": str(hook), "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "XDG_CONFIG_HOME": str(Path(tmp) / "config")}
            result = subprocess.run(["bash", str(ROOT / "bin" / "omasession"), "report", str(out)],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            logs = list(out.glob("*.log"))
            self.assertEqual([p.read_text() for p in logs], ["second\n"])


LOG = """Restore started 2026-11-01 15:16:33 -03
trigger: login
restoring 4 window(s) from /x/last.toml

[browser] google-chrome: 2 window(s) in 1 profile(s) -- requesting their saved sessions
  Default: exit_type=Crashed exited_cleanly=False
  Default: Sessions: Session_1 (979000 B, 10-31 13:50:09)
  Default: windows in the newest session: 1
  ! extra blank browser window: 0x1
[3/4] foot: restored → ws2  (cwd {home}/Devs/x; no program saved)
[4/4] foot: no window after 30s  (no cwd saved; runs btop)

3/4 placed in 12s
windows actually on screen: 3

previous boot: last entry 2026-10-31 13:50:09 -0300
  omarchy menu: 2026-10-31T13:50:07 omarchy-system-shutdown --inhibit
  logind: 2026-10-31T13:50:09 The system will power off now!
"""


class Summary(unittest.TestCase):
    def summary(self, text=LOG, rc=2):
        return restore_log.summarize(text.format(home=Path.home()), rc, "login", NOW, NOW + 40)

    def test_the_outcome_and_the_lines_worth_reading_first(self) -> None:
        got = self.summary()
        self.assertEqual((got["result"], got["placed"], got["total"]), ("partial", 3, 4))
        self.assertEqual(got["problems"], ["! extra blank browser window: 0x1",
                                           "[4/4] foot: no window after 30s  (no cwd saved; runs btop)"])
        self.assertIn("Default: windows in the newest session: 1", got["notes"])
        self.assertIn("omarchy menu: 2026-10-31T13:50:07 omarchy-system-shutdown --inhibit",
                      got["notes"])
        self.assertIn("terminals: 2, 1 with no program saved, 0 whose folder is gone, "
                      "1 with no folder saved", got["notes"])
        # The session file listing is long and lives in the log.
        self.assertFalse(any("Sessions:" in n for n in got["notes"]))

    def test_each_way_a_restore_can_end(self) -> None:
        self.assertEqual(self.summary(rc=None)["result"], "interrupted")
        self.assertEqual(self.summary(rc=1)["result"], "failed")
        self.assertEqual(self.summary(rc=0)["result"], "ok")
        self.assertEqual(self.summary("trigger: login\nnothing to restore\n", rc=1)["result"],
                         "nothing")

    def test_a_second_chrome_profile_is_read_too(self) -> None:
        # Review omasession-51: "Profile 1" has a space and was skipped.
        got = self.summary("  Profile 1: exit_type=Crashed exited_cleanly=False\n", rc=0)
        self.assertEqual(got["notes"], ["Profile 1: exit_type=Crashed exited_cleanly=False"])

    def test_a_capped_note_list_is_not_called_a_title_leak(self) -> None:
        notes = self.summary("".join(f"  P{i}: exit_type=Normal exited_cleanly=True\n"
                                     for i in range(20)), rc=0)["notes"]
        _problems, kept, left_out = restore_log._shareable([], notes)
        self.assertEqual((left_out, kept[-1]), (0, "… and 9 more in the log"))

    def test_a_session_that_could_not_be_read_failed(self) -> None:
        # Review omasession-50 (rev-1): a broken last.toml with no usable
        # previous generation also prints "nothing to restore" and exits 1.
        text = "  ! last.toml: TOML does not parse\nnothing to restore\n"
        got = self.summary(text, rc=1)
        self.assertEqual(got["result"], "failed")
        self.assertEqual(got["problems"], ["! last.toml: TOML does not parse"])
        self.assertEqual(self.summary("nothing to restore\n", rc=1)["result"], "nothing")

    def test_a_crash_names_the_exception(self) -> None:
        text = "Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'workspace'\n"
        self.assertEqual(self.summary(text, rc=1)["problems"], ["crashed: KeyError: 'workspace'"])

    def test_a_flood_of_problems_is_capped(self) -> None:
        text = "".join(f"[{i}/40] foot: no window after 30s\n" for i in range(1, 41))
        problems = self.summary(text, rc=2)["problems"]
        self.assertEqual(len(problems), restore_log.MAX_LINES)
        self.assertEqual(problems[-1], "… and 29 more in the log")


class Results(unittest.TestCase):
    def test_a_finished_restore_leaves_its_result_and_pruning_takes_it_along(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = restore_log.RestoreLog(state, "login", now=NOW, keep=1)
            first.publish("3/4 placed in 2s\n")
            first.finish(2, "3/4 placed in 2s\n")
            first.close()
            stamp_after(state / first.name)
            result = json.loads((state / "restores" / f"{first.stem}.result.json").read_text())
            self.assertEqual((result["result"], result["placed"]), ("partial", 3))
            second = restore_log.RestoreLog(state, "manual", now=NOW + 60, keep=1)
            second.publish("x\n")
            second.close()
            self.assertEqual(sorted(p.name for p in (state / "restores").iterdir()),
                             [f"{second.stem}.log"])

    def run_logged(self, main) -> dict:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"XDG_STATE_HOME": tmp, "OMASESSION_RESTORE_TRIGGER": "manual"}), \
             patch.object(replay, "main", main), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                replay.run_logged()
            except SystemExit:
                pass
            found = restore_log.entries(Path(tmp) / "omasession")
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_restore_stopped_by_a_signal_is_interrupted_not_failed(self) -> None:
        def stopped():
            print("[1/2] foot: restored → ws1")
            raise SystemExit(143)

        self.assertEqual(self.run_logged(stopped)["result"], "interrupted")

    def test_a_restore_that_crashed_says_how(self) -> None:
        def crashed():
            raise KeyError("workspace")

        entry = self.run_logged(crashed)
        self.assertEqual(entry["result"], "failed")
        self.assertEqual(entry["problems"], ["crashed: KeyError: 'workspace'"])

    def test_the_list_is_newest_first_and_names_unfinished_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state" / "omasession"
            done = restore_log.RestoreLog(state, "login", now=NOW)
            done.publish("4/4 placed in 3s\n")
            done.finish(0, "4/4 placed in 3s\n")
            done.close()
            stamp_after(state / done.name)
            killed = restore_log.RestoreLog(state, "manual", now=NOW + 60)
            killed.publish("restoring 2 window(s)\n")
            killed.close()
            stamp_after(state / killed.name)
            env = {**os.environ, "XDG_STATE_HOME": str(Path(tmp) / "state"),
                   "XDG_CONFIG_HOME": str(Path(tmp) / "config")}
            out = subprocess.run(["bash", str(ROOT / "bin" / "omasession"), "restores", "--json"],
                                 env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            found = json.loads(out.stdout)["restores"]
            self.assertEqual([(e["trigger"], e["result"]) for e in found],
                             [("manual", "unfinished"), ("login", "ok")])
            self.assertEqual(found[1]["log"], str(state / done.name))

    def test_no_restore_yet_is_an_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(restore_log.entries(Path(tmp)), [])


class Issue(unittest.TestCase):
    ENTRY = {"trigger": "login", "result": "partial", "placed": 3, "total": 4, "started": NOW,
             "problems": [f"[4/4] foot: no window after 30s  (cwd {Path.home()}/Devs/x; runs btop)"],
             "notes": ["Default: windows in the newest session: 1"]}

    def test_the_link_carries_the_outcome_with_the_home_folder_hidden(self) -> None:
        url, body = restore_log.issue(self.ENTRY, "0.6.0",
                                      f"{Path.home()}/.local/state/omasession/reports/r")
        self.assertTrue(url.startswith(restore_log.ISSUES_URL + "?title=Login%20restore"))
        self.assertIn("omasession 0.6.0, login restore at", body)
        self.assertIn("result: partial (3/4 placed)", body)
        self.assertIn("[4/4] foot: no window after 30s  (folder left out; program left out)", body)
        self.assertIn("`~/.local/state/omasession/reports/r`", body)
        self.assertNotIn(str(Path.home()), body)

    def test_a_long_report_still_fits_in_a_link(self) -> None:
        entry = {**self.ENTRY,
                 "problems": [f"[{i}/300] foot: no window after 30s  (cwd ~/x; runs btop)"
                              for i in range(300)],
                 "notes": ["terminals: " + "y" * 600] * 12}
        url, body = restore_log.issue(entry, "0.6.0", "/r")
        self.assertLessEqual(len(url), restore_log.MAX_URL)
        self.assertIn("more in the log", body)

    def test_window_titles_never_reach_the_issue(self) -> None:
        # Review omasession-50: four replay lines end with a browser window's
        # title, and the whole problem list used to go into the issue.
        titled = ["! leaving unmatched blank browser window SECRET-TITLE",
                  "! extra blank browser window: SECRET-TITLE",
                  "! leaving unmatched live browser window SECRET-TITLE - Gmail",
                  "! extra window the browser reopened: SECRET-TITLE - Google Docs",
                  "! some line the replay prints in a later version SECRET-TITLE"]
        entry = {**self.ENTRY, "problems": [*titled, "! google-chrome reopened nothing",
                                            "crashed: KeyError: 'SECRET-TITLE'"]}
        url, body = restore_log.issue(entry, "0.6.0", "/r")
        self.assertNotIn("SECRET-TITLE", body)
        self.assertNotIn("SECRET-TITLE", unquote(url))
        self.assertIn("! google-chrome reopened nothing", body)
        self.assertIn("crashed: KeyError\n", body)
        # The diagnosis stays; a line the issue does not know is only counted.
        self.assertIn("! extra window the browser reopened: <title left out>", body)
        self.assertIn("! leaving unmatched live browser window <title left out>", body)
        self.assertIn("1 other flagged line(s) are only in the log", body)

    def test_folders_never_reach_the_issue(self) -> None:
        # Marketplace review (omarchy-plugin-marketplace#13759): a failed
        # terminal's line ends with the folder it was saved in, and the issue
        # link used to carry it with only the home folder written as ~.
        private = "acme-private-x"
        home = str(Path.home())
        entry = {**self.ENTRY, "problems": [
            f"[1/5] foot: no window after 30s  (cwd {home}/Devs/{private}; runs btop)",
            f"[2/5] ghostty: no window after 30s  (cwd /srv/{private} (gone); no program saved)",
            f"[3/5] kitty: no window after 30s  (no cwd saved; runs {home}/{private}/bin/tool)",
            f"[5/5] foot: no window after 30s  (cwd ~/x; runs deploy-{private})",
            "[4/5] alacritty: no window after 30s",
            f"! could not arm google-chrome profile: [Errno 13] Permission denied: '{home}/{private}/x'",
            f"! could not keep a copy of {private}.toml: [Errno 2] No such file: '/{private}'",
            "! could not keep a copy of last.titles.json: [Errno 2] No such file",
        ], "notes": [f"terminals: 3, 1 with no program saved, 1 whose folder is gone, 1 with no folder saved",
                     f"terminals: 1 in {private}",
                     f"Default: windows in the newest session: 1 {private}",
                     # Review omasession-53: a session the person named, and
                     # the free text after a journal line's unit.
                     f"fallback: {private}.toml was not a consistent pair",
                     "fallback: last.prev.toml was not a consistent pair",
                     f"omarchy menu: 2026-11-01T10:00:00-03:00 host app[1]: ran /home/u/{private}/omarchy-system-shutdown",
                     f"logind: 2026-11-01T10:00:02-03:00 host systemd-logind[9]: poweroff requested from client PID 7 ('{private}') (unit user@1000.service)..."]}
        url, body = restore_log.issue(entry, "0.6.1", f"{home}/.local/state/omasession/reports/r")
        self.assertNotIn(private, body)
        self.assertNotIn(private, unquote(url))
        # The diagnosis stays: which terminal, whether its folder was saved or
        # gone, and whether a program was saved, without naming either.
        self.assertIn("[1/5] foot: no window after 30s  (folder left out; program left out)", body)
        self.assertIn("[2/5] ghostty: no window after 30s  (folder left out, gone; no program saved)", body)
        self.assertIn("[3/5] kitty: no window after 30s  (no cwd saved; program left out)", body)
        self.assertIn("[4/5] alacritty: no window after 30s\n", body)
        self.assertIn("! could not arm google-chrome profile: <details left out>", body)
        self.assertIn("! could not keep a copy of <session file>: <details left out>", body)
        self.assertIn("! could not keep a copy of last.titles.json: <details left out>", body)
        self.assertIn("fallback: <session file> was not a consistent pair", body)
        self.assertIn("fallback: last.prev.toml was not a consistent pair", body)
        self.assertIn("omarchy menu: 2026-11-01T10:00:00-03:00 omarchy-system-shutdown\n", body)
        self.assertIn("logind: 2026-11-01T10:00:02-03:00 poweroff requested\n", body)
        self.assertIn("terminals: 3, 1 with no program saved, 1 whose folder is gone, 1 with no folder saved", body)
        self.assertIn("2 other flagged line(s)", body)

    def test_notes_keep_their_facts_without_the_machine_name(self) -> None:
        entry = {**self.ENTRY, "problems": [], "notes": [
            "Profile 1: exit_type=Crashed exited_cleanly=False",
            "previous boot: last entry 2026-10-31 13:50:09 -0300",
            "omarchy menu: 2026-10-31T13:50:07-0300 SECRET-HOST omarchy-system-shutdown[812]: --inhibit",
            "logind: no shutdown request (crash or power loss?)",
            "previous boot: unexpected journal line 'SECRET-HOST x'"]}
        _url, body = restore_log.issue(entry, "0.6.0", "/r")
        self.assertNotIn("SECRET-HOST", body)
        self.assertIn("Profile 1: exit_type=Crashed exited_cleanly=False", body)
        self.assertIn("omarchy menu: 2026-10-31T13:50:07-0300 omarchy-system-shutdown\n", body)
        self.assertIn("logind: no shutdown request (crash or power loss?)", body)
        self.assertIn("1 other flagged line(s)", body)

class ReportIssue(unittest.TestCase):
    def run_report(self, tmp: Path, *args: str, hook_text: str = "") -> subprocess.CompletedProcess:
        hook = tmp / "journal.sh"
        hook.write_text("journalctl() { return 1; }\n" + hook_text)
        env = {**os.environ, "BASH_ENV": str(hook), "XDG_STATE_HOME": str(tmp / "state"),
               "XDG_CONFIG_HOME": str(tmp / "config")}
        return subprocess.run(["bash", str(ROOT / "bin" / "omasession"), "report", *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_the_chosen_restore_is_bundled_with_a_bug_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / "state" / "omasession"
            stems = []
            for trigger, rc in (("login", 2), ("manual", 0)):
                log = restore_log.RestoreLog(state, trigger, now=NOW)
                log.publish(f"{trigger}\n")
                log.finish(rc, f"{trigger}\n")
                log.close()
                stamp_after(state / log.name)
                stems.append(log.stem)
            out = tmp / "report"
            result = self.run_report(tmp, "--restore", stems[0], "--issue", "--json", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            answer = json.loads(result.stdout)
            self.assertEqual((answer["dir"], answer["restore"]), (str(out), stems[0]))
            self.assertIn("title=Login%20restore%3A%20partial", answer["url"])
            self.assertEqual([p.read_text() for p in out.glob("*.log")], ["login\n"])
            self.assertIn("result: partial", (out / "issue.md").read_text())
            self.assertTrue((out / f"{stems[0]}.result.json").exists())
            # The panel shows this draft before anything opens the link, so it
            # must be exactly the text the link carries.
            self.assertEqual(answer["body"], (out / "issue.md").read_text())
            self.assertIn("&body=" + quote(answer["body"], safe=""), answer["url"])

    def test_a_bug_report_that_cannot_be_prepared_says_so(self) -> None:
        # Review omasession-50: the panel showed "Report written" and no issue
        # opened, because the failure only went to stderr with exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = tmp / "state" / "omasession"
            log = restore_log.RestoreLog(state, "login", now=NOW)
            log.publish("x\n")
            log.finish(2, "x\n")
            log.close()
            failing = ('python3() { if [[ "$2" == issue ]]; then echo "boom: disk full" >&2; return 1; fi; '
                       'command python3 "$@"; }\n')
            out = tmp / "report"
            result = self.run_report(tmp, "--restore", log.stem, "--issue", "--json", str(out),
                                     hook_text=failing)
            self.assertEqual(result.returncode, 0, result.stderr)
            answer = json.loads(result.stdout)
            self.assertEqual((answer["url"], answer["issueError"]), ("", "boom: disk full"))
            self.assertFalse((out / "issue-error.txt").exists())

    def test_no_restore_yet_is_an_issue_error_not_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_report(Path(tmp), "--issue", "--json", str(Path(tmp) / "r"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["issueError"], "no restore log kept yet")

    def test_a_name_that_is_a_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_report(Path(tmp), "--restore", "../../x", str(Path(tmp) / "r"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a restore name", result.stderr)
            self.assertFalse((Path(tmp) / "r").exists())


if __name__ == "__main__":
    unittest.main()
