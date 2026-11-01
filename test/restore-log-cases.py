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


if __name__ == "__main__":
    unittest.main()
