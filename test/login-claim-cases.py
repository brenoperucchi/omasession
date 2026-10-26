#!/usr/bin/env python3
"""The login restore runs once per Hyprland instance.

`restore --if-enabled` is the automatic login path. More than one caller can
reach it in the same login: the autostart.lua line, the plugin itself once it
sets itself up, and a restarted shell. Only the first may restore; the rest
must leave the desktop alone. The claim is a file created with O_EXCL under
$XDG_RUNTIME_DIR, named after the instance signature, so the next login (a new
signature, and a fresh tmpfs after a reboot) restores again.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "omasession"
SAFE_FS = ROOT / "lib" / "safe_fs.py"
SIG = "abc123_1700000000_42"


def claim(directory: Path, name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SAFE_FS), "claim", "--create", str(directory), name],
        capture_output=True, text=True,
    )


class SafeFsClaim(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "omasession"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_claim_wins_and_the_second_is_told_apart(self) -> None:
        self.assertEqual(claim(self.dir, "login-restore.x").returncode, 0)
        target = self.dir / "login-restore.x"
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(claim(self.dir, "login-restore.x").returncode, 6)

    def test_concurrent_claims_have_exactly_one_winner(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: claim(self.dir, "login-restore.y").returncode,
                                  range(8)))
        self.assertEqual(sorted(codes), [0] + [6] * 7)

    def test_losing_the_race_to_create_the_directory_is_not_an_error(self) -> None:
        # Two login callers start together and both find the directory
        # missing; the second mkdir then fails with EEXIST.
        sys.path.insert(0, str(ROOT / "lib"))
        import safe_fs
        from unittest import mock

        real_mkdir = os.mkdir

        def mkdir_after_another_process(path, mode=0o777, *, dir_fd=None):
            real_mkdir(path, mode, dir_fd=dir_fd)
            real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch.object(safe_fs.os, "mkdir", mkdir_after_another_process):
            fd = safe_fs.open_dir_chain(str(self.dir), create=True)
        os.close(fd)
        self.assertTrue(self.dir.is_dir())

    def test_a_symlink_in_place_of_the_claim_is_refused(self) -> None:
        self.dir.mkdir(mode=0o700)
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("keep")
        (self.dir / "login-restore.z").symlink_to(victim)
        self.assertNotIn(claim(self.dir, "login-restore.z").returncode, (0, 6))
        self.assertEqual(victim.read_text(), "keep")


class LoginRestoreOnce(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / "run"
        self.runtime.mkdir(mode=0o700)
        self.sessions = base / "sessions"
        self.sessions.mkdir()
        self.env = {
            "HOME": str(base),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_STATE_HOME": str(base / "state"),
            "OMASESSION_SESSION_DIR": str(self.sessions),
            "HYPRLAND_INSTANCE_SIGNATURE": SIG,
            "PATH": "/usr/bin:/bin",
        }
        self.marker = self.runtime / "omasession" / f"login-restore.{SIG}"
        # The login restore runs only while the plugin is enabled (setup-cases.py).
        shell = base / "config" / "omarchy" / "shell.json"
        shell.parent.mkdir(parents=True)
        shell.write_text('{"version": 1, "plugins": [{"id": "brenoperucchi.omasession"}]}\n')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str, sig: str = SIG) -> subprocess.CompletedProcess:
        env = dict(self.env, HYPRLAND_INSTANCE_SIGNATURE=sig)
        return subprocess.run([str(BIN), *args], env=env, capture_output=True,
                              text=True, timeout=30)

    def claim_now(self) -> None:
        self.assertEqual(claim(self.runtime / "omasession", self.marker.name).returncode, 0)

    def test_a_second_login_restore_in_the_same_instance_does_nothing(self) -> None:
        self.claim_now()
        result = self.run_cli("restore", "--if-enabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already restored for this login", result.stdout)

    def test_a_new_instance_is_not_blocked_by_the_old_claim(self) -> None:
        self.claim_now()
        result = self.run_cli("restore", "--if-enabled", sig="def456_1700000500_7")
        self.assertNotIn("already restored", result.stdout)
        self.assertIn("no saved session", result.stderr)

    def test_a_login_restore_that_cannot_start_leaves_the_claim_free(self) -> None:
        result = self.run_cli("restore", "--if-enabled")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no saved session", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_a_manual_restore_ignores_the_claim(self) -> None:
        self.claim_now()
        result = self.run_cli("restore")
        self.assertNotIn("already restored", result.stdout)
        self.assertIn("no saved session", result.stderr)

    def test_restore_on_login_off_claims_nothing(self) -> None:
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omasession"
        config.mkdir(parents=True)
        (config / "config.json").write_text('{"restoreOnLogin": false}\n')
        result = self.run_cli("restore", "--if-enabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
