#!/usr/bin/env python3
"""Opt-in live check using disposable foot windows on an unused workspace.

Run `python3 tools/test-tiled-layout.py --live` from a Hyprland session.
Temporarily pauses the snapshot timer; never replays the user's saved session.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import capture
import replay as P
import tiled_layout as T


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for disposable windows")


def dispatch(commands, cleanup=()):
    response = P.hypr(T.checked_script(commands, cleanup))
    if response != T.SUCCESS:
        raise RuntimeError(response)


def main():
    if sys.argv[1:] != ["--live"]:
        print(__doc__)
        return 1
    if not shutil.which("foot"):
        raise RuntimeError("foot is required for the disposable test windows")
    initial = T.query("clients")
    focused = T.query("activewindow").get("address")
    original_ws = T.query("activeworkspace")["id"]
    cursor = T.query("cursorpos")
    used = {w["id"] for w in T.query("workspaces")}
    workspace = next(i for i in range(90, 1000) if i not in used)
    app = f"omasession-layout-test-{os.getpid()}"
    timer = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "omasession-snapshot.timer"]).returncode == 0
    processes = []

    def own():
        return [w for w in T.query("clients") if w.get("class", "").startswith(app) and w.get("mapped")]

    def close_own():
        for window in own():
            P.dispatch("window.close", window="address:" + window["address"])
        wait_for(lambda: not own())

    def launch(title, suffix=""):
        argv = ["foot", "--app-id=" + app + suffix, "--title=" + title, "sleep", "300"]
        processes.append(subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        window = wait_for(lambda: next((w for w in own() if w["title"] == title), None))
        if window["workspace"]["id"] != workspace:
            raise RuntimeError("test window opened on an unexpected workspace")
        return window, argv

    try:
        if timer:
            subprocess.run(["systemctl", "--user", "stop", "omasession-snapshot.timer"], check=True)
        wait_for(lambda: subprocess.run(["systemctl", "--user", "is-active", "--quiet", "omasession-snapshot.service"]).returncode != 0)
        dispatch([f"hl.dsp.focus({{workspace={workspace}}})"])
        windows, commands = [], {}
        for letter in "ABCDE":
            window, argv = launch("OmaSession probe " + letter)
            windows.append(window)
            commands[window["title"]] = shlex.join(argv)
        leaves = [{"leaf": w["address"]} for w in windows]
        tree = {"axis": 0, "ratio": 1.1,
                "left": {"axis": 1, "ratio": 0.7, "left": leaves[0], "right": leaves[1]},
                "right": {"axis": 1, "ratio": 1.2, "left": leaves[2],
                          "right": {"axis": 0, "ratio": 0.8, "left": leaves[3], "right": leaves[4]}}}
        floats = [f'hl.dsp.window.float({{window={P.lua("address:" + w["address"])},action="on"}})' for w in windows]
        first = f'hl.dsp.window.float({{window={P.lua("address:" + windows[0]["address"])},action="off"}})'
        dispatch(floats + [first] + T.tree_commands(tree, P.lua), ['hl.dsp.layout("preselect none")'])
        time.sleep(0.5)
        saved = own()
        monitor = next(m["name"] for m in T.query("monitors") if m["id"] == saved[0]["monitor"])
        specs = [{"app_id": app, "title": w["title"], "workspace": str(workspace),
                  "position": w["at"], "size": w["size"], "floating": False,
                  "fullscreen": False, "monitor": monitor, "launch_cmd": commands[w["title"]]}
                 for w in saved]
        with tempfile.TemporaryDirectory(prefix="omasession-layout-live-") as temp:
            path = Path(temp) / "probe.toml"
            path.write_text('[session]\nname = "probe"\n\n' + '\n\n'.join(capture.window_toml(s) for s in specs))
            env = {**os.environ, "XDG_STATE_HOME": temp}

            def replay_and_compare(label, preserve_focus=False):
                before_focus = T.query("activewindow").get("address")
                before_cursor = T.query("cursorpos")
                result = subprocess.run([sys.executable, "-B", str(ROOT / "lib/replay.py"), str(path)],
                                        env=env, capture_output=True, text=True, timeout=90)
                print(result.stdout, flush=True)
                if result.returncode:
                    raise RuntimeError(f"{label}: replay exited {result.returncode}: {result.stderr}")
                actual = {w["title"]: w for w in own()}
                if len(actual) != len(specs):
                    raise RuntimeError("wrong number of restored windows")
                worst = 0
                for spec in specs:
                    w = actual[spec["title"]]
                    assert not w["floating"] and w["workspace"]["id"] == workspace
                    worst = max(worst, *(abs(a - b) for a, b in zip(w["at"] + w["size"], spec["position"] + spec["size"])))
                assert worst <= T.TOLERANCE, f"{label}: maximum coordinate/size difference {worst}px"
                if preserve_focus:
                    assert T.query("activewindow").get("address") == before_focus
                    assert T.query("cursorpos") == before_cursor
                print(f"PASS {label}: five titled windows, maximum geometry difference {worst}px", flush=True)

            # Reverse insertion scrambles geometry while all windows survive.
            dispatch(floats + [f'hl.dsp.window.float({{window={P.lua("address:" + w["address"])},action="off"}})' for w in reversed(windows)])
            # Rebuilding an inactive desk must return the user's focus/cursor.
            P.dispatch("focus", workspace=original_ws)
            P.dispatch("cursor.move", x=cursor["x"], y=cursor["y"])
            replay_and_compare("adopt existing windows and preserve focus", preserve_focus=True)
            close_own()
            dispatch([f"hl.dsp.focus({{workspace={workspace}}})"])
            replay_and_compare("launch windows from saved commands")

            live = own()
            address_by_title = {w["title"]: w["address"] for w in live}
            for spec in specs:
                spec["_restored_address"] = address_by_title[spec["title"]]
            extra, _ = launch("OmaSession extra window", "-extra")
            before = {w["address"]: (w["at"], w["size"], w["floating"]) for w in own()}
            outcome = T.restore_tiled(specs, P.hypr, P.lua)
            after = {w["address"]: (w["at"], w["size"], w["floating"]) for w in own()}
            assert outcome.skipped == 1 and outcome.restored == 0 and before == after
            print("PASS extra unrelated tile: workspace left unchanged", flush=True)
            P.dispatch("window.close", window="address:" + extra["address"])
            wait_for(lambda: len(own()) == 5)

            # Force an actual dispatch failure after floating a test window;
            # the Lua cleanup must still retile it.
            addr = live[0]["address"]
            fault_commands = [f'hl.dsp.window.float({{window={P.lua("address:" + addr)},action="on"}})',
                              'hl.dsp.focus({window="address:0x0"})']
            cleanup = [f'hl.dsp.window.float({{window={P.lua("address:" + addr)},action="off"}})',
                       'hl.dsp.layout("preselect none")']
            response = P.hypr(T.checked_script(fault_commands, cleanup))
            assert response.startswith("omasession-layout-error:"), response
            assert all(not w["floating"] for w in own())
            print("PASS failed dispatcher: disposable window returned to tiling", flush=True)
    finally:
        try:
            close_own()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if focused:
                P.dispatch("focus", window="address:" + focused)
            else:
                P.dispatch("focus", workspace=original_ws)
            P.dispatch("cursor.move", x=cursor["x"], y=cursor["y"])
            if timer:
                subprocess.run(["systemctl", "--user", "start", "omasession-snapshot.timer"], check=True)
    final = T.query("clients")
    assert {w["address"] for w in initial}.issubset({w["address"] for w in final}), "an original window disappeared"
    print("PASS cleanup: original windows remain; snapshot timer resumed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
