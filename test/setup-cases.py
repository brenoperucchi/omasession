#!/usr/bin/env python3
"""A standard `omarchy plugin add --enable` is a complete install.

Omarchy runs no hook of a plugin on add, enable, disable or remove; the only
code of ours that runs is the shell loading the plugin's service. So the
service runs `install --if-needed`, which converges the timer and the login
line without fighting the user, and the automatic paths (`save --if-enabled`
from the timer, `restore --if-enabled` at login) do nothing while the plugin
is disabled or removed.

Every case runs against a throwaway XDG tree. XDG_RUNTIME_DIR points into it
too, so `systemctl --user` finds no bus and cannot touch the real manager.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "omasession"
PLUGIN_ID = "brenoperucchi.omasession"
SIG = "abc123_1700000000_42"
USER_AUTOSTART = '-- user lines\no.launch_on_start("waybar")\n'


def shell_config(**overrides) -> dict:
    config = {"version": 1, "bar": {"layout": {"left": [], "center": [],
                                               "right": [{"id": PLUGIN_ID}]}}}
    config.update(overrides)
    return config


class SetupEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / "run"
        self.runtime.mkdir(mode=0o700)
        self.config = base / "config"
        self.state = base / "state"
        self.sessions = base / "sessions"
        self.sessions.mkdir()
        self.units = self.config / "systemd" / "user"
        self.autostart = self.config / "hypr" / "autostart.lua"
        self.autostart.parent.mkdir(parents=True)
        self.autostart.write_text(USER_AUTOSTART)
        self.env = {
            "HOME": str(base),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_STATE_HOME": str(self.state),
            "OMASESSION_SESSION_DIR": str(self.sessions),
            "HYPRLAND_INSTANCE_SIGNATURE": SIG,
            "PATH": "/usr/bin:/bin",
        }
        self.write_shell(shell_config())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_shell(self, config, raw: str | None = None) -> None:
        # The shell reads $HOME/.config/omarchy whatever XDG_CONFIG_HOME says.
        path = Path(self.env["HOME"]) / ".config" / "omarchy" / "shell.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else json.dumps(config))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(BIN), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def login_lines(self) -> list[str]:
        return [l for l in self.autostart.read_text().splitlines()
                if "omasession restore" in l]


class EnabledCheck(SetupEnv):
    def assert_paused(self) -> None:
        result = self.run_cli("save", "--if-enabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not enabled", result.stdout)
        self.assertFalse((self.state / "omasession" / "last-save.json").exists())
        result = self.run_cli("restore", "--if-enabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not enabled", result.stdout)
        self.assertFalse((self.runtime / "omasession" / f"login-restore.{SIG}").exists())

    def assert_running(self) -> None:
        result = self.run_cli("restore", "--if-enabled")
        self.assertNotIn("not enabled", result.stdout)
        self.assertIn("no saved session", result.stderr)

    def test_enabled_in_any_bar_section(self) -> None:
        self.assert_running()
        self.write_shell(shell_config(bar={"layout": {"left": [PLUGIN_ID]}}))
        self.assert_running()

    def test_enabled_as_a_plugin_entry(self) -> None:
        self.write_shell({"version": 1, "plugins": [{"id": PLUGIN_ID}]})
        self.assert_running()

    def test_a_bare_string_in_plugins_is_disabled(self) -> None:
        # The shell reads only `.id` of objects there; strings count in the bar.
        self.write_shell({"version": 1, "plugins": [PLUGIN_ID]})
        self.assert_paused()

    def test_a_shell_config_under_xdg_config_home_is_not_the_shells(self) -> None:
        stray = self.config / "omarchy" / "shell.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(json.dumps(shell_config()))
        (Path(self.env["HOME"]) / ".config" / "omarchy" / "shell.json").unlink()
        self.assert_paused()

    def test_not_in_the_layout_is_disabled(self) -> None:
        self.write_shell({"version": 1, "bar": {"layout": {"right": [{"id": "x.other"}]}}})
        self.assert_paused()

    def test_listed_in_disabled_plugins_is_disabled(self) -> None:
        self.write_shell(shell_config(disabledPlugins=[PLUGIN_ID]))
        self.assert_paused()

    def test_no_usable_shell_config_is_disabled(self) -> None:
        # The shell itself falls back to its defaults then, which never list a
        # third-party plugin.
        (Path(self.env["HOME"]) / ".config" / "omarchy" / "shell.json").unlink()
        self.assert_paused()
        self.write_shell(None, raw="{not json")
        self.assert_paused()
        self.write_shell({"bar": shell_config()["bar"]})
        self.assert_paused()

    def test_manual_commands_ignore_it(self) -> None:
        self.write_shell(shell_config(disabledPlugins=[PLUGIN_ID]))
        result = self.run_cli("restore")
        self.assertNotIn("not enabled", result.stdout)
        self.assertIn("no saved session", result.stderr)


class InstallIfNeeded(SetupEnv):
    def unit(self, name: str) -> str:
        return (self.units / f"omasession-snapshot.{name}").read_text()

    def test_writes_guarded_units_that_save_only_when_enabled(self) -> None:
        self.run_cli("install", "--if-needed")
        cli = f"{ROOT}/bin/omasession"
        for name in ("service", "timer"):
            self.assertIn(f"ConditionPathExists={cli}\n", self.unit(name))
        self.assertIn(f"ExecStart={cli} save --if-enabled\n", self.unit("service"))

    def test_a_second_run_changes_nothing(self) -> None:
        self.run_cli("install", "--if-needed")
        files = [self.units / "omasession-snapshot.service",
                 self.units / "omasession-snapshot.timer", self.autostart]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        result = self.run_cli("install", "--if-needed")
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in files], before,
                         result.stdout + result.stderr)

    def test_adds_one_guarded_login_line(self) -> None:
        self.run_cli("install", "--if-needed")
        self.run_cli("install", "--if-needed")
        lines = self.login_lines()
        self.assertEqual(len(lines), 1)
        cli = f"{ROOT}/bin/omasession"
        self.assertIn(f'io.open("{cli}")', lines[0])
        self.assertIn(f'o.launch_on_start("{cli} restore --if-enabled")', lines[0])

    def test_a_login_line_the_user_removed_stays_removed(self) -> None:
        self.run_cli("install", "--if-needed")
        self.autostart.write_text(USER_AUTOSTART)
        self.run_cli("install", "--if-needed")
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)
        # Asking for it explicitly still puts it back.
        self.run_cli("install")
        self.assertEqual(len(self.login_lines()), 1)

    def test_an_older_unguarded_line_is_guarded_in_place(self) -> None:
        other = "/home/someone/Devs/omasession/bin/omasession"
        self.autostart.write_text(
            USER_AUTOSTART + "\n-- OmaSession: reopen the last session at login\n"
            f'o.launch_on_start("{other} restore --if-enabled")\n')
        self.run_cli("install", "--if-needed")
        lines = self.login_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(f'io.open("{other}")', lines[0])
        self.assertIn(f'o.launch_on_start("{other} restore --if-enabled")', lines[0])
        self.run_cli("uninstall")
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)

    def test_a_failed_rewrite_leaves_the_autostart_file_alone(self) -> None:
        other = "/home/someone/Devs/omasession/bin/omasession"
        original = (USER_AUTOSTART + "\n-- OmaSession: reopen the last session at login\n"
                    f'o.launch_on_start("{other} restore --if-enabled")\n')
        self.autostart.write_text(original)
        # The CLI pins PATH, so the failing awk comes in as a function.
        hook = Path(self.tmp.name) / "failing-awk.sh"
        hook.write_text('awk() { if [[ -n "${OLD:-}" ]]; then echo "-- partial"; return 42; fi\n'
                        '        command awk "$@"; }\n')
        self.env["BASH_ENV"] = str(hook)
        result = self.run_cli("install", "--if-needed")
        self.assertEqual(self.autostart.read_text(), original, result.stdout + result.stderr)
        self.assertNotIn("now checks that the CLI exists", result.stdout)

    def test_a_shell_start_never_replaces_a_session_with_a_legacy_one(self) -> None:
        legacy = Path(self.tmp.name) / ".local" / "share" / "hyprresume" / "sessions"
        legacy.mkdir(parents=True)
        (legacy / "last.toml").write_text("legacy\n")
        (legacy / "last.titles.json").write_text("{}\n")
        (self.sessions / "last.toml").write_text("current\n")
        self.run_cli("install", "--if-needed")
        self.assertEqual((self.sessions / "last.toml").read_text(), "current\n")

    def test_uninstall_round_trips_the_autostart_file(self) -> None:
        self.run_cli("install", "--if-needed")
        self.run_cli("uninstall")
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)

    def test_after_uninstall_the_service_leaves_setup_alone(self) -> None:
        self.run_cli("install", "--if-needed")
        self.run_cli("uninstall")
        self.run_cli("install", "--if-needed")
        self.assertFalse((self.units / "omasession-snapshot.timer").exists())
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)
        self.run_cli("install")
        self.assertTrue((self.units / "omasession-snapshot.timer").exists())
        self.run_cli("uninstall")
        self.run_cli("install")
        self.run_cli("install", "--if-needed")
        self.assertEqual(len(self.login_lines()), 1)

    def test_a_plugin_added_again_after_uninstall_sets_itself_up(self) -> None:
        # uninstall, `omarchy plugin remove`, then add it again: a fresh clone
        # in a new directory. The old refusal was for the old one.
        self.run_cli("install", "--if-needed")
        self.run_cli("uninstall")
        clone = Path(self.tmp.name) / "plugins" / PLUGIN_ID
        shutil.copytree(ROOT, clone, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "screenshots", "__pycache__"))
        subprocess.run([str(clone / "bin" / "omasession"), "install", "--if-needed"],
                       env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn(f"ExecStart={clone}/bin/omasession save --if-enabled\n",
                      self.unit("service"))
        self.assertEqual(len(self.login_lines()), 1)

    def test_a_new_interval_reaches_the_timer(self) -> None:
        self.run_cli("install", "--if-needed")
        self.assertEqual(self.run_cli("config", "set", "saveIntervalSec", "60").returncode, 0)
        self.run_cli("install", "--if-needed")
        self.assertIn("OnUnitActiveSec=60s\n", self.unit("timer"))

    def test_concurrent_runs_add_one_line(self) -> None:
        procs = [subprocess.Popen([str(BIN), "install", "--if-needed"], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for _ in range(4)]
        for proc in procs:
            proc.wait(timeout=60)
        self.assertEqual(len(self.login_lines()), 1)


FAKE_SYSTEMCTL = r'''
systemctl() {
    local verb="" a
    for a in "$@"; do [[ "$a" == -* ]] || { verb="$a"; break; }; done
    local d="$FAKE_SYSTEMD"
    case "$verb" in
        enable) [[ -e "$d/fail-enable" ]] && return 1
                touch "$d/enabled"; [[ " $* " == *" --now "* ]] && touch "$d/active" ;;
        disable) rm -f "$d/enabled"; [[ " $* " == *" --now "* ]] && rm -f "$d/active" ;;
        start) touch "$d/active" ;;
        is-enabled) [[ -e "$d/enabled" ]] ;;
        is-active) [[ -e "$d/active" ]] ;;
        *) return 0 ;;
    esac
}
'''


class UserChoicesSurviveUpgrades(SetupEnv):
    """The user's own choices outlast the plugin's service, including those
    made under 0.5.0, which kept no setup.json."""

    def setUp(self) -> None:
        super().setUp()
        self.systemd = Path(self.tmp.name) / "fake-systemd"
        self.systemd.mkdir()
        hook = Path(self.tmp.name) / "fake-systemctl.sh"
        hook.write_text(FAKE_SYSTEMCTL)
        self.env["BASH_ENV"] = str(hook)
        self.env["FAKE_SYSTEMD"] = str(self.systemd)

    def timer_on(self) -> bool:
        return (self.systemd / "enabled").exists()

    def user_disables_timer(self) -> None:
        for name in ("enabled", "active"):
            (self.systemd / name).unlink(missing_ok=True)

    def forget_setup_state(self) -> None:
        # What a 0.5.0 install leaves: no setup.json.
        (self.state / "omasession" / "setup.json").unlink()

    def test_the_service_turns_the_timer_on(self) -> None:
        self.run_cli("install", "--if-needed")
        self.assertTrue(self.timer_on())

    def test_a_timer_the_user_turned_off_stays_off(self) -> None:
        self.run_cli("install", "--if-needed")
        self.user_disables_timer()
        self.run_cli("install", "--if-needed")
        self.assertFalse(self.timer_on())
        self.assertEqual(self.run_cli("config", "set", "saveIntervalSec", "60").returncode, 0)
        self.run_cli("install", "--if-needed")
        self.assertIn("OnUnitActiveSec=60s\n",
                      (self.units / "omasession-snapshot.timer").read_text())
        self.assertFalse(self.timer_on())
        self.run_cli("install")
        self.assertTrue(self.timer_on())

    def test_a_timer_that_failed_to_come_back_is_retried(self) -> None:
        # Updating the units disables the timer before enabling it again; a
        # failure there is ours, not the user's choice.
        self.run_cli("install", "--if-needed")
        self.assertEqual(self.run_cli("config", "set", "saveIntervalSec", "60").returncode, 0)
        (self.systemd / "fail-enable").touch()
        self.run_cli("install", "--if-needed")
        self.assertFalse(self.timer_on())
        (self.systemd / "fail-enable").unlink()
        self.run_cli("install", "--if-needed")
        self.assertTrue(self.timer_on())

    def test_a_stopped_but_enabled_timer_is_started(self) -> None:
        self.run_cli("install", "--if-needed")
        (self.systemd / "active").unlink()
        self.run_cli("install", "--if-needed")
        self.assertTrue((self.systemd / "active").exists())

    def test_upgrade_keeps_a_login_line_deleted_under_050(self) -> None:
        self.run_cli("install")
        self.forget_setup_state()
        self.autostart.write_text(USER_AUTOSTART)
        self.run_cli("install", "--if-needed")
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)
        self.assertTrue(self.timer_on())
        self.run_cli("install")
        self.assertEqual(len(self.login_lines()), 1)

    def test_upgrade_keeps_an_uninstall_done_under_050(self) -> None:
        self.run_cli("install")
        self.run_cli("uninstall")
        self.forget_setup_state()
        self.run_cli("install", "--if-needed")
        self.assertFalse((self.units / "omasession-snapshot.timer").exists())
        self.assertFalse(self.timer_on())
        self.assertEqual(self.autostart.read_text(), USER_AUTOSTART)
        self.run_cli("install", "--if-needed")
        self.assertFalse((self.units / "omasession-snapshot.timer").exists())

    def test_a_plugin_added_again_after_a_050_uninstall_sets_itself_up(self) -> None:
        # The 0.5.0 README's way out: uninstall, then `omarchy plugin remove`.
        # Adding the plugin back later clones a directory newer than the
        # autostart backup that uninstall kept.
        self.run_cli("install")
        self.run_cli("uninstall")
        self.forget_setup_state()
        clone = Path(self.tmp.name) / "plugins" / PLUGIN_ID
        shutil.copytree(ROOT, clone, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "screenshots", "__pycache__"))
        subprocess.run([str(clone / "bin" / "omasession"), "install", "--if-needed"],
                       env=self.env, capture_output=True, text=True, timeout=60)
        self.assertTrue((self.units / "omasession-snapshot.timer").exists())
        self.assertTrue(self.timer_on())
        self.assertEqual(len(self.login_lines()), 1)

    def test_upgrade_keeps_a_timer_turned_off_under_050(self) -> None:
        self.run_cli("install")
        self.user_disables_timer()
        self.forget_setup_state()
        self.run_cli("install", "--if-needed")
        self.assertFalse(self.timer_on())
        self.assertEqual(len(self.login_lines()), 1)

    def test_a_first_install_still_sets_everything_up(self) -> None:
        self.run_cli("install", "--if-needed")
        self.assertTrue(self.timer_on())
        self.assertEqual(len(self.login_lines()), 1)


class StatusReportsAutomation(SetupEnv):
    def status(self) -> dict:
        result = self.run_cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_status_says_whether_the_plugin_is_enabled(self) -> None:
        self.assertIs(self.status()["pluginEnabled"], True)
        self.write_shell(shell_config(disabledPlugins=[PLUGIN_ID]))
        self.assertIs(self.status()["pluginEnabled"], False)

    def test_the_panel_warns_when_automatic_save_is_off(self) -> None:
        panel = (ROOT / "Panel.qml").read_text()
        self.assertIn("status.pluginEnabled === false", panel)
        self.assertIn('status.timer !== "active"', panel)


class PluginService(unittest.TestCase):
    def test_the_manifest_declares_the_service(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertIn("service", manifest["kinds"])
        self.assertIn("bar-widget", manifest["kinds"])
        self.assertEqual(manifest["entryPoints"]["service"], "Service.qml")

    def test_the_service_runs_setup_detached(self) -> None:
        source = (ROOT / "Service.qml").read_text()
        self.assertIn('"install", "--if-needed"', source)
        # Detached: a shell restart would SIGKILL a child Process mid-write.
        self.assertIn("Quickshell.execDetached", source)
        self.assertNotIn("Process {", source)


if __name__ == "__main__":
    unittest.main()
