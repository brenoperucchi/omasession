"""Behavioral contracts for relaunching the program a terminal was running."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import capture  # noqa: E402
import captured  # noqa: E402
import resolve  # noqa: E402


def window(address: str, title: str, pid: int = 100) -> dict:
    return {"address": address, "title": title, "pid": pid, "workspace": {"id": 1}}


def program(name: str, shell: str, shell_name: str | None = "bash") -> dict:
    return {"shell": shell, "shell_name": shell_name, "name": name,
            "argv": [f"/usr/bin/{name}"], "cwd": "/work"}


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
                patch.object(resolve, "_tpgid", create=True, side_effect=lambda pid: {"2": 4}.get(str(pid))), \
                patch.object(resolve.os, "readlink", return_value="/work"):
            found = resolve.terminal_programs(1, {"flockr", "btop"})
        self.assertEqual(sorted(p["name"] for p in found), ["btop", "flockr"])
        self.assertEqual({p["name"]: p["shell"] for p in found}, {"btop": "2", "flockr": None})

    def find(self, tree: dict, comm: dict, tpgid: dict, allowed: set) -> list[dict]:
        with patch.object(resolve, "_children", side_effect=lambda pid: tree.get(str(pid), [])), \
                patch.object(resolve, "_comm", side_effect=lambda pid: comm[str(pid)]), \
                patch.object(resolve, "from_cmdline", side_effect=lambda pid: [comm[str(pid)]]), \
                patch.object(resolve, "_tpgid", create=True, side_effect=lambda pid: tpgid.get(str(pid))), \
                patch.object(resolve.os, "readlink", return_value="/work"):
            return resolve.terminal_programs(1, allowed)

    def test_the_foreground_job_is_the_program_not_the_first_child(self) -> None:
        # nvim suspended with Ctrl-Z, then btop: both are children of the
        # shell, and the window shows btop.
        found = self.find({"1": ["2"], "2": ["3", "4"]},
                          {"2": "bash", "3": "nvim", "4": "btop"}, {"2": 4}, {"nvim", "btop"})
        self.assertEqual([p["name"] for p in found], ["btop"])
        self.assertEqual(found[0]["shell_name"], "bash")

    def test_a_shell_at_its_prompt_runs_no_program(self) -> None:
        # A suspended or background job is still a child; at the prompt the
        # shell is its own foreground group.
        found = self.find({"1": ["2"], "2": ["3"]}, {"2": "bash", "3": "nvim"}, {"2": 2}, {"nvim"})
        self.assertEqual(found, [])

    def test_nothing_allowed_means_nothing_found(self) -> None:
        self.assertEqual(resolve.terminal_programs(1, set()), [])

    def test_allowed_programs_parse_the_config_list(self) -> None:
        with patch.dict(capture.os.environ, {"OMASESSION_TERMINAL_PROGRAMS": " flockr, btop ,,"}):
            self.assertEqual(capture.allowed_programs(), {"flockr", "btop"})


# A program comes back inside the user's shell: the rc files set PATH (mise,
# ~/.local/bin), and quitting the program leaves a prompt, not a closed window.
BTOP = ["bash", "-ic", "/usr/bin/btop; exec bash"]


class WithProgramCases(unittest.TestCase):
    def setUp(self) -> None:
        isdir = patch.object(resolve.os.path, "isdir", return_value=True)
        isdir.start()
        self.addCleanup(isdir.stop)

    def test_a_program_folder_that_is_gone_keeps_the_terminals_own(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/gone"}
        with patch.object(resolve.os.path, "isdir", return_value=False):
            self.assertEqual(resolve.with_program(["foot", "--working-directory=/work"], moved),
                             ["foot", "--working-directory=/work", "--", *BTOP])

    def test_ghostty_gets_dash_e_after_its_own_flags(self) -> None:
        argv = resolve.with_program(["ghostty"], program("flockr", "11"))
        self.assertEqual(argv, ["ghostty", "--gtk-single-instance=false",
                                "--working-directory=/work", "-e",
                                "bash", "-ic", "/usr/bin/flockr; exec bash"])

    def test_foot_gets_the_program_after_double_dash(self) -> None:
        argv = resolve.with_program(["foot", "--working-directory=/work"], program("btop", "11"))
        self.assertEqual(argv, ["foot", "--working-directory=/work", "--", *BTOP])

    def test_foot_and_kitty_start_in_the_programs_directory(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/elsewhere"}
        self.assertEqual(resolve.with_program(["foot", "-D", "/work", "--title", "t"], moved),
                         ["foot", "--title", "t", "--working-directory=/elsewhere", "--", *BTOP])
        self.assertEqual(resolve.with_program(["kitty", "--directory=/work"], moved),
                         ["kitty", "--directory=/elsewhere", "--", *BTOP])

    def test_another_options_value_is_never_taken_for_the_directory(self) -> None:
        moved = {**program("btop", "11"), "cwd": "/elsewhere"}
        self.assertEqual(resolve.with_program(["foot", "--title", "-Dashboard"], moved),
                         ["foot", "--title", "-Dashboard", "--working-directory=/elsewhere",
                          "--", *BTOP])

    def test_a_launch_that_already_runs_a_command_is_left_alone(self) -> None:
        self.assertIsNone(resolve.with_program(["foot", "--", "nvim"], program("btop", "11")))
        self.assertIsNone(resolve.with_program(["ghostty", "-e", "nvim"], program("btop", "11")))

    def test_arguments_are_quoted_for_the_shell(self) -> None:
        notes = {**program("nvim", "11", "zsh"), "argv": ["nvim", "my notes.md"]}
        self.assertEqual(resolve.with_program(["foot"], notes)[-3:],
                         ["zsh", "-ic", "nvim 'my notes.md'; exec zsh"])

    def test_a_shell_it_cannot_drive_gets_the_program_directly(self) -> None:
        self.assertEqual(resolve.with_program(["foot"], program("btop", "11", "nu")),
                         ["foot", "--working-directory=/work", "--", "/usr/bin/btop"])

    def test_a_program_with_no_shell_above_it_uses_the_login_shell(self) -> None:
        with patch.dict(resolve.os.environ, {"SHELL": "/usr/bin/fish"}):
            argv = resolve.with_program(["foot"], program("btop", "11", None))
        self.assertEqual(argv[-3:], ["fish", "-ic", "/usr/bin/btop; exec fish"])

    def test_unknown_terminal_is_left_alone(self) -> None:
        self.assertIsNone(resolve.with_program(["wezterm"], program("btop", "11")))


class LabelCases(unittest.TestCase):
    """The panel names the program, not the shell that relaunches it."""

    def test_a_program_run_through_the_shell_is_named(self) -> None:
        cmd = resolve.command({"argv": ["foot", "--working-directory=/w", "--", *BTOP]})
        self.assertEqual(captured.terminal_program(cmd), "btop")
        cmd = resolve.command({"argv": ["ghostty", "-e", "zsh", "-ic", "nvim 'my notes.md'; exec zsh"]})
        self.assertEqual(captured.terminal_program(cmd), "nvim")

    def test_a_shell_the_user_launched_is_still_the_shell(self) -> None:
        self.assertEqual(captured.terminal_program("foot -- bash"), "bash")
        self.assertEqual(captured.terminal_program("foot -- tmux new -A -s 0"), "tmux")


if __name__ == "__main__":
    unittest.main()
