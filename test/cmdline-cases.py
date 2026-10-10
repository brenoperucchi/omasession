#!/usr/bin/env python3
"""The /proc cmdline fallback, including browsers that rewrite their title."""

from __future__ import annotations

import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import resolve as R  # noqa: E402


def argv_for(raw: bytes) -> list[str] | None:
    with patch.object(Path, "read_bytes", return_value=raw):
        return R.from_cmdline(1)


class CmdlineCases(unittest.TestCase):
    def test_nul_separated_argv_is_kept_as_is(self) -> None:
        raw = b"/usr/bin/foot\0--app-id=my tile\0--\0btop\0"
        self.assertEqual(
            argv_for(raw), ["/usr/bin/foot", "--app-id=my tile", "--", "btop"]
        )

    def test_rewritten_process_title_is_split_back_into_argv(self) -> None:
        # Chromium/Electron: one space-joined string, no NUL separators.
        raw = (b"/opt/google/chrome/chrome --ozone-platform=wayland "
               b"--no-first-run")
        argv = argv_for(raw)
        self.assertEqual(argv, ["/opt/google/chrome/chrome",
                                "--ozone-platform=wayland", "--no-first-run"])
        command = R.command({"argv": argv})
        self.assertEqual(shlex.split(command)[0], "/opt/google/chrome/chrome")

    def test_single_existing_path_with_a_space_is_not_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "my app"
            binary.touch()
            self.assertEqual(argv_for(str(binary).encode() + b"\0"),
                             [str(binary)])

    def test_rewritten_title_with_nul_padding_is_split(self) -> None:
        # setproctitle pads the old argv area with NULs after the new title.
        raw = b"/opt/google/chrome/chrome --type=zygote\0\0\0\0"
        self.assertEqual(argv_for(raw),
                         ["/opt/google/chrome/chrome", "--type=zygote"])

    def test_single_word_without_spaces_is_kept(self) -> None:
        self.assertEqual(argv_for(b"Telegram\0"), ["Telegram"])

    def test_resolve_fallback_yields_an_unquoted_launch_command(self) -> None:
        # The path that produced the uwsm error: no .desktop match, so the
        # window resolves via cmdline and its command is saved as launch_cmd.
        raw = (b"/opt/google/chrome/chrome --ozone-platform=wayland "
               b"--load-extension=/a,/b --no-first-run")
        with patch.object(Path, "read_bytes", return_value=raw), \
                patch.object(R, "from_cgroup", return_value=None):
            result = R.resolve({"class": "google-chrome", "pid": 1}, {})
        self.assertEqual(result["via"], "cmdline")
        self.assertEqual(
            R.command(result),
            "/opt/google/chrome/chrome --ozone-platform=wayland "
            "--load-extension=/a,/b --no-first-run",
        )

    def test_empty_cmdline_is_unresolved(self) -> None:
        self.assertIsNone(argv_for(b""))


if __name__ == "__main__":
    unittest.main()
