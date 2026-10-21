"""Behavioral contracts for relaunching the program a terminal was running."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import capture  # noqa: E402
import resolve  # noqa: E402


def window(address: str, title: str, pid: int = 100) -> dict:
    return {"address": address, "title": title, "pid": pid, "workspace": {"id": 1}}


def program(name: str, shell: str) -> dict:
    return {"shell": shell, "name": name, "argv": [f"/usr/bin/{name}"], "cwd": "/work"}


class ProgramWindowCases(unittest.TestCase):
    def test_title_match_then_elimination_in_one_terminal_process(self) -> None:
        # One Ghostty process, two windows: btop names itself, flockr does not.
        windows = [window("0xa", "dk-desktop: Dotfiles"), window("0xb", "btop")]
        found = [program("flockr", "11"), program("btop", "12")]
        with patch.object(capture.R, "terminal_programs", return_value=found):
            result = capture.program_windows(windows, {"flockr", "btop"})
        self.assertEqual(result["0xb"]["name"], "btop")
        self.assertEqual(result["0xa"]["name"], "flockr")

    def test_ambiguous_windows_get_no_program(self) -> None:
        # Two untitled windows and one program: no way to tell, so neither.
        windows = [window("0xa", "bash"), window("0xb", "bash")]
        with patch.object(capture.R, "terminal_programs", return_value=[program("flockr", "11")]):
            self.assertEqual(capture.program_windows(windows, {"flockr"}), {})

    def test_a_title_must_name_the_program_as_a_word(self) -> None:
        # "top" is not in "btop", and "flockr" is not in "flockr-notes".
        windows = [window("0xa", "btop"), window("0xb", "flockr-notes")]
        found = [program("top", "11"), program("flockr", "12")]
        with patch.object(capture.R, "terminal_programs", return_value=found):
            self.assertEqual(capture.program_windows(windows, {"top", "flockr"}), {})

    def test_two_of_the_same_program_are_not_matched_by_title(self) -> None:
        # Only one title names btop; which btop it is cannot be known.
        windows = [window("0xa", "btop"), window("0xb", "bash")]
        found = [program("btop", "11"), program("btop", "12")]
        with patch.object(capture.R, "terminal_programs", return_value=found):
            self.assertEqual(capture.program_windows(windows, {"btop"}), {})

    def test_a_title_naming_two_programs_matches_neither(self) -> None:
        windows = [window("0xa", "nvim btop"), window("0xb", "bash")]
        found = [program("nvim", "11"), program("btop", "12")]
        with patch.object(capture.R, "terminal_programs", return_value=found):
            self.assertEqual(capture.program_windows(windows, {"nvim", "btop"}), {})

    def test_program_started_with_dash_e_has_no_shell_in_between(self) -> None:
        # After a restore, `ghostty -e flockr` runs flockr directly; the next
        # save must still see it, or the program is lost one restore later.
        tree = {"1": ["2", "3"], "2": ["4"], "3": [], "4": []}
        comm = {"2": "bash", "3": "flockr", "4": "btop"}
        with patch.object(resolve, "_children", side_effect=lambda pid: tree[str(pid)]), \
                patch.object(resolve, "_comm", side_effect=lambda pid: comm[str(pid)]), \
                patch.object(resolve, "from_cmdline", side_effect=lambda pid: [comm[str(pid)]]), \
                patch.object(resolve.os, "readlink", return_value="/work"):
            found = resolve.terminal_programs(1, {"flockr", "btop"})
        self.assertEqual(sorted(p["name"] for p in found), ["btop", "flockr"])
        self.assertEqual({p["name"]: p["shell"] for p in found}, {"btop": "2", "flockr": None})

    def test_nothing_allowed_means_nothing_found(self) -> None:
        self.assertEqual(resolve.terminal_programs(1, set()), [])

    def test_allowed_programs_parse_the_config_list(self) -> None:
        with patch.dict(capture.os.environ, {"OMASESSION_TERMINAL_PROGRAMS": " flockr, btop ,,"}):
            self.assertEqual(capture.allowed_programs(), {"flockr", "btop"})


class WithProgramCases(unittest.TestCase):
    def setUp(self) -> None:
        isdir = patch.object(resolve.os.path, "isdir", return_value=True)
        isdir.start()
        self.addCleanup(isdir.stop)

    def test_a_program_folder_that_is_gone_keeps_the_terminals_own(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/gone"}
        with patch.object(resolve.os.path, "isdir", return_value=False):
            self.assertEqual(resolve.with_program(["foot", "--working-directory=/work"], moved),
                             ["foot", "--working-directory=/work", "--", "/usr/bin/btop"])

    def test_ghostty_gets_dash_e_after_its_own_flags(self) -> None:
        argv = resolve.with_program(["ghostty"], program("flockr", "11"))
        self.assertEqual(argv, ["ghostty", "--gtk-single-instance=false",
                                "--working-directory=/work", "-e", "/usr/bin/flockr"])

    def test_foot_gets_the_program_after_double_dash(self) -> None:
        argv = resolve.with_program(["foot", "--working-directory=/work"], program("btop", "11"))
        self.assertEqual(argv, ["foot", "--working-directory=/work", "--", "/usr/bin/btop"])

    def test_foot_and_kitty_start_in_the_programs_directory(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/elsewhere"}
        self.assertEqual(resolve.with_program(["foot", "-D", "/work", "--title", "t"], moved),
                         ["foot", "--title", "t", "--working-directory=/elsewhere", "--", "/usr/bin/btop"])
        self.assertEqual(resolve.with_program(["kitty", "--directory=/work"], moved),
                         ["kitty", "--directory=/elsewhere", "--", "/usr/bin/btop"])

    def test_another_options_value_is_never_taken_for_the_directory(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/elsewhere"}
        self.assertEqual(resolve.with_program(["foot", "--title", "-Dashboard"], moved),
                         ["foot", "--title", "-Dashboard", "--working-directory=/elsewhere",
                          "--", "/usr/bin/btop"])

    def test_a_launch_that_already_runs_a_command_is_left_alone(self) -> None:
        self.assertIsNone(resolve.with_program(["foot", "--", "nvim"], program("btop", "11")))
        self.assertIsNone(resolve.with_program(["ghostty", "-e", "nvim"], program("btop", "11")))

    def test_unknown_terminal_is_left_alone(self) -> None:
        self.assertIsNone(resolve.with_program(["wezterm"], program("btop", "11")))


if __name__ == "__main__":
    unittest.main()
