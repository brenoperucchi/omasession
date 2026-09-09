#!/usr/bin/env python3
"""Restore a hyprresume session through the Lua IPC of Hyprland 0.56+.

hyprresume writes a good session file and then cannot replay it: it drives the
compositor with the pre-Lua syntax (`keyword windowrule[...]`, `exec [workspace
N] ...`, `movetoworkspacesilent`), which a Lua-configured Hyprland rejects
without ever returning an error -- so it reports 6/6 restored against an empty
screen. This reads the same file and replays it with hl.dispatch(hl.dsp.*).

Three things this does that the naive version did not:

  1. Places each window by address AFTER it maps, instead of trusting the
     workspace that was focused at launch time. With single-instance apps
     (Chromium) the window is created by the pre-existing process, so the
     focused-workspace trick lands windows on arbitrary workspaces.
  2. Re-applies floating/geometry by address, with the right selector
     (hyprctl already reports the address with its 0x prefix).
  3. Adopts a window that is already on screen rather than counting it as a
     failure -- a restore may run when part of the session survived.

Proof-of-concept for the Omarchy session plugin, not the plugin itself.
"""

import difflib
import json
import shlex
import re
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

SESSION = Path.home() / ".local/share/hyprresume/sessions/last.toml"
WINDOW_TIMEOUT = 15.0
BROWSER_TIMEOUT = 40.0
BROWSER_QUIET = 4.0     # no new window for this long = the browser is done
SETTLE = 0.4
POLL = 0.25

# Chromium-family browsers restore their own windows, tabs and history -- the
# one thing no amount of /proc reading can reconstruct, because the URLs were
# arguments to an invocation that already exited and every window shares a PID.
# Let the browser do it, then place what it opened.
#
# Two obstacles, and they differ per vendor:
#   * after a hard kill the profile is marked "Crashed", and a crashed profile
#     waits for a click on the "Restore pages?" bar -- even with
#     --restore-last-session. Affects every vendor.
#   * Chrome (not Chromium) shows its onboarding pages *instead of* restoring
#     on the first launch after each version milestone. --no-first-run does not
#     stop it; the policy from browser-setup.sh plus last_whats_new_version do.
#
# Keyed by the window class Hyprland reports.
# Terminals take their working directory as an argument; hyprresume records the
# cwd but never replays it. Keyed by the class Hyprland reports.
TERMINAL_CWD_FLAG = {
    "foot": "--working-directory",
    "Alacritty": "--working-directory",
    "kitty": "--directory",
    "com.mitchellh.ghostty": "--working-directory",
}

BROWSERS = {
    "chromium": {
        "profile": "~/.config/chromium/Default/Preferences",
        "binary": "chromium",
        "comm": "chromium",
        "policy_dir": "/etc/chromium/policies/managed",
    },
    "google-chrome": {
        "profile": "~/.config/google-chrome/Default/Preferences",
        "binary": "google-chrome-stable",
        # O processo se chama `chrome`, nao `google-chrome-stable`.
        "comm": "chrome",
        "policy_dir": "/etc/opt/chrome/policies/managed",
    },
    "brave-browser": {
        "profile": "~/.config/BraveSoftware/Brave-Browser/Default/Preferences",
        "binary": "brave",
        "comm": "brave",
        "policy_dir": "/etc/brave/policies/managed",
    },
    "vivaldi-stable": {
        "profile": "~/.config/vivaldi/Default/Preferences",
        "binary": "vivaldi-stable",
        "comm": "vivaldi-bin",
        "policy_dir": "/etc/vivaldi/policies/managed",
    },
}


def hypr(lua: str) -> str:
    """Run one Lua statement in the compositor."""
    try:
        out = subprocess.run(
            ["hyprctl", "repl", lua], capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return (out.stdout + out.stderr).strip()


def lua(value) -> str:
    """Encode a Python value as a Lua literal.

    Every dispatcher argument reaches the compositor through this. The previous
    version interpolated straight into an f-string, which broke on the first
    directory containing a space and, worse, let a quote or a backslash close
    the Lua string early -- a rejection Hyprland does not report back, which is
    the exact failure mode this project exists to catch.

    Strings become decimal escapes, three digits per UTF-8 byte. That is
    unambiguous by construction: no quote, backslash, newline or `]]` inside the
    value can terminate the literal, so there is nothing left to get wrong.
    Long-bracket syntax (`[[...]]`) would not do -- it can be closed by a `]]`
    in the value, it swallows a leading newline, and it offers no protection at
    all on the shell side of exec_cmd.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + "".join(f"\\{b:03d}" for b in value.encode()) + '"'
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua(k)}]={lua(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"cannot encode {value!r} as Lua")


def dispatch(method: str, *positional, **args) -> str:
    """hl.dispatch(hl.dsp.<method>(...)) with every argument encoded.

    Two calling conventions, and getting them backwards fails silently:
    `hl.dsp.exec_cmd` takes ONE positional string, while the window and focus
    dispatchers take a table. Passing a table to exec_cmd launches nothing and
    Hyprland reports no error -- which is how a refactor of this function
    disabled every relaunch here without a single failing line of output.
    """
    if positional:
        if args:
            raise ValueError("dispatcher takes positional or table args, not both")
        body = ",".join(lua(v) for v in positional)
    else:
        body = "{" + ",".join(f"{k}={lua(v)}" for k, v in args.items()) + "}"
    return hypr(f"hl.dispatch(hl.dsp.{method}({body}))")


def workspace_arg(workspace):
    """What hl.dsp.*.move accepts for a workspace.

    hyprresume records the workspace as a string ("3"), so the old
    `workspace={ws}` happened to emit a bare number and worked -- by accident.
    A named workspace would have emitted `workspace=my-notes`, which is a Lua
    syntax error, silently. Numbers stay numbers; anything else is a name, and
    special workspaces keep their `special:` prefix.
    """
    text = str(workspace)
    if text.lstrip("-").isdigit():
        return int(text)
    return text if text.startswith("special:") else f"name:{text}"


def clients() -> list[dict]:
    try:
        out = subprocess.run(
            ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=15
        )
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def mapped() -> list[dict]:
    return [c for c in clients() if c.get("mapped") and c["workspace"]["id"] > 0]


def find_new(known: set[str], timeout: float):
    """First mapped window whose address is not in `known`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for c in mapped():
            if c["address"] not in known:
                return c
        time.sleep(POLL)
    return None


def place(addr: str, spec: dict) -> None:
    """Put an already-mapped window where the session file says it belongs."""
    sel = f"address:{addr}"
    ws = spec.get("workspace")
    if ws is not None:
        dispatch("window.move", window=sel, workspace=workspace_arg(ws))
        time.sleep(SETTLE)

    # Recorded by hyprresume and, until now, thrown away -- while the README
    # promised geometry came back identical.
    if spec.get("fullscreen"):
        dispatch("window.fullscreen_state", window=sel, internal=1, client=0)
        time.sleep(SETTLE)

    if not spec.get("floating"):
        return

    dispatch("window.float", window=sel, action="on")
    time.sleep(SETTLE)
    size = spec.get("size") or [None, None]
    pos = spec.get("position") or [None, None]
    if size[0] and size[1]:
        dispatch("window.resize", window=sel, x=size[0], y=size[1], exact=True)
        time.sleep(SETTLE)
    if pos[0] is not None and pos[1] is not None:
        # exact=true here too: probe.sh sets positions with it and resize above
        # already used it, so without it the saved coordinates were being
        # applied under a different meaning than the one they were measured in.
        dispatch("window.move", window=sel, x=pos[0], y=pos[1], exact=True)
        time.sleep(SETTLE)


def restore(spec: dict, claimed: set[str], index: int, total: int) -> bool:
    app = spec.get("app_id", "?")
    cmd = spec.get("launch_cmd")
    ws = spec.get("workspace", "1")

    # 3. Adopt a surviving window of the same class before launching another.
    for c in mapped():
        if c["address"] not in claimed and c.get("class") == app:
            claimed.add(c["address"])
            place(c["address"], spec)
            print(f"[{index}/{total}] {app}: adopted existing window → ws{ws}")
            return True

    if not cmd:
        print(f"[{index}/{total}] {app}: no launch_cmd, skipped")
        return False

    # A terminal's cwd is recorded but never replayed by hyprresume; foot takes
    # it as an argument, so put it back on the command line.
    # Two layers, and the Lua encoder only covers one of them: exec_cmd hands
    # the string to `sh -c`, so a directory containing `;` or `$(...)` runs
    # regardless of how the Lua literal was written. shlex.quote is what makes
    # the shell side safe -- and it is also what makes a plain space work, which
    # is the failure anyone hits first with a folder named "my project".
    #
    # The option goes BEFORE any `--`: hyprresume records terminals as
    # `foot -- btop`, and appending after the separator hands the flag to btop.
    cwd = spec.get("cwd")
    if cwd and app in TERMINAL_CWD_FLAG and TERMINAL_CWD_FLAG[app] not in cmd:
        flag = f"{TERMINAL_CWD_FLAG[app]}={shlex.quote(cwd)}"
        head, sep, tail = cmd.partition(" -- ")
        cmd = f"{head} {flag}{sep}{tail}"

    known = {c["address"] for c in mapped()} | claimed
    dispatch("exec_cmd", f"uwsm app -- {cmd}")

    win = find_new(known, WINDOW_TIMEOUT)
    if win is None:
        print(f"[{index}/{total}] {app}: no window after {WINDOW_TIMEOUT:.0f}s")
        return False

    claimed.add(win["address"])
    place(win["address"], spec)
    print(f"[{index}/{total}] {app}: restored → ws{ws}")
    return True


def wait_for_browser(app: str, known: set[str], want: int) -> list[dict]:
    """Windows of `app` that appeared, waiting only as long as it takes.

    Returns as soon as `want` windows are up, or once the count has stopped
    changing for BROWSER_QUIET seconds -- the browser reopens its windows in a
    burst, so a quiet gap means it is done, however many it managed.
    """
    deadline = time.monotonic() + BROWSER_TIMEOUT
    found: list[dict] = []
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        current = [c for c in mapped()
                   if c["address"] not in known and c.get("class") == app]
        if len(current) != len(found):
            found = current
            last_change = time.monotonic()
        if len(found) >= want:
            return found
        if found and time.monotonic() - last_change >= BROWSER_QUIET:
            return found
        time.sleep(POLL)
    return found


def strip_suffix(title: str) -> str:
    """Drop the vendor suffix browsers append: 'Foo - Google Chrome' → 'Foo'."""
    for suffix in (" - Google Chrome", " - Chromium", " - Brave", " - Vivaldi"):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def normalize(title: str) -> str:
    """Suffix-stripped, lowercased, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", strip_suffix(title).lower())


def move_to(addr: str, workspace) -> None:
    dispatch("window.move", window=f"address:{addr}",
             workspace=workspace_arg(workspace))
    time.sleep(SETTLE)


def take_best_match(title: str, candidates: list[dict]) -> dict | None:
    """Pop the saved window whose title best matches, or None if none is close.

    An exact hit wins outright. Otherwise compare the vendor suffix-stripped
    titles: a page caught mid-load reports its domain, so 'wiki.hypr.land' has
    to still find 'Hyprland Wiki'. 0.6 is permissive enough for that and tight
    enough not to swap two unrelated pages.
    """
    if not candidates:
        return None
    for i, cand in enumerate(candidates):
        if cand.get("title") == title:
            return candidates.pop(i)

    # Compare with punctuation and spacing removed, so a bare domain still
    # reaches its page title: "wiki.hypr.land" and "Hyprland Wiki" become
    # "wikihyprland" and "hyprlandwiki", which share the block "hyprland".
    bare = normalize(title)
    best_i, best_score = None, 0.0
    for i, cand in enumerate(candidates):
        other = normalize(cand.get("title", ""))
        if not bare or not other:
            continue
        score = difflib.SequenceMatcher(None, bare, other).ratio()
        if bare in other or other in bare:
            score = max(score, 0.8)
        if score > best_score:
            best_i, best_score = i, score
    if best_i is not None and best_score >= 0.6:
        return candidates.pop(best_i)
    return None


def browser_major_version(binary: str) -> int | None:
    """Major version of the installed browser, for the What's New milestone."""
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.\d+", out)
    return int(match.group(1)) if match else None


def profile_in_use(prefs: Path, comm_name: str) -> bool:
    """Is a browser running against the profile we are about to rewrite?

    A live Chromium holds its preferences in memory and writes them out on exit,
    so editing the file underneath it is either lost or interleaved -- and the
    file we would be clobbering is the one every restore depends on.

    Matched on /proc/<pid>/comm, never on the command line. The first version of
    this matched the string anywhere in argv and reported "chromium is running"
    against a stopped browser, because the argv it matched was its own: the
    exact shape of the `pkill -f` mistake this project documents in plan 001.
    """
    root = str(prefs.parent.parent)
    me = os.getpid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
            if pid == me or proc.stat().st_uid != os.getuid():
                continue
            if proc.joinpath("comm").read_text().strip() != comm_name:
                continue
            argv = proc.joinpath("cmdline").read_bytes().decode("utf-8", "replace")
        except (OSError, ValueError):
            continue
        # A browser on another profile is none of our business.
        if "--user-data-dir" not in argv or root in argv:
            return True
    return False


def arm_browser_profile(app: str) -> bool:
    """Make the profile restore its own session on the next launch.

    This is the single most important write in the whole restore, and it took a
    measurement to learn that. With `exit_type` left as the teardown wrote it,
    the same snapshot reopens 0 of 3 tabs; with it set to "Normal", 3 of 3 --
    and `--restore-last-session` does NOT substitute for it (measured
    2026-09-09: 0/3 with the flag alone on a Crashed profile, 3/3 with the flag
    plus this write). See docs/plans/001-RESULTADO.md.

    Because everything depends on it, it is also the most dangerous write: a
    truncated Preferences makes Chromium rebuild the profile from scratch --
    bookmarks, extensions, all of it. Hence tempfile + os.replace, and a
    refusal when the browser is running.
    """
    spec = BROWSERS[app]
    prefs = Path(spec["profile"]).expanduser()
    if not prefs.is_file():
        return False

    if profile_in_use(prefs, spec.get("comm", spec["binary"])):
        print(f"  ! {app} is running; not touching its Preferences. "
              f"Close it first or its tabs will not come back.")
        return False

    policy = Path(spec["policy_dir"]) / "omasession-no-promo.json"
    if app == "google-chrome" and not policy.is_file():
        print(f"  ! {policy} missing -- Chrome may show onboarding instead of "
              f"restoring (run browser-setup once, as root)")

    try:
        data = json.loads(prefs.read_text())
        # `restore_on_startup` is deliberately NOT written: DESIGN.md §5 records
        # it as ignored, and writing into a preference the browser tracks buys
        # nothing while enlarging the blast radius of this write. The policy
        # file carries it. What we do write is what the measurement showed
        # actually decides the outcome.
        data.setdefault("profile", {})["exit_type"] = "Normal"
        data["profile"]["exited_cleanly"] = True
        # Mark the current milestone as seen. Re-read every time: the browser
        # updates itself, and a stale value brings the promo tab back.
        major = browser_major_version(spec["binary"])
        if major is not None:
            data.setdefault("browser", {})["last_whats_new_version"] = major

        # Atomic: same directory, then replace. A crash mid-write used to leave
        # a truncated Preferences behind, and this code runs at login on a
        # machine whose GPU lockups are why the snapshot interval is short.
        fd, tmp = tempfile.mkstemp(dir=str(prefs.parent), prefix=".omasession-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, prefs)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! could not arm {app} profile: {exc}")
        return False


def restore_browser(app: str, specs: list[dict], titles: list[dict],
                    claimed: set[str]) -> int:
    """Launch the browser once, then place the windows it reopens by title."""
    want = len(specs)
    print(f"[browser] {app}: {want} window(s) -- letting the browser restore them")
    if not arm_browser_profile(app):
        print(f"  ! no profile for {app}, falling back to per-window launch")
        return sum(restore(s, claimed, i, want) for i, s in enumerate(specs, 1))

    known = {c["address"] for c in mapped()} | claimed
    cmd = specs[0].get("launch_cmd") or app
    # Google Chrome hijacks the first launch after an install or version bump
    # with its onboarding pages ("What's New", default-browser check) and shows
    # those *instead of* restoring the session. Chromium does not. These flags
    # are what keep the restore from being silently swallowed.
    for flag in ("--no-first-run", "--no-default-browser-check"):
        if flag not in cmd:
            cmd = f"{cmd} {flag}"
    dispatch("exec_cmd", f"uwsm app -- {cmd}")

    # Wait for the browser to reopen what it intends to -- but stop as soon as
    # it settles. A fixed wait burns the whole timeout whenever the browser
    # hands back fewer windows than we saved, which is the common case after an
    # unclean shutdown: two browsers x 40s dominated an 82s restore.
    found = wait_for_browser(app, known, want)
    if not found:
        print(f"  ! {app} reopened nothing")

    # Title is the only key that survives -- the windows share a PID, a class
    # and a command line. But it is a soft key: a page that had not finished
    # loading reports its bare domain ("wiki.hypr.land") where the save
    # recorded the real title ("Hyprland Wiki"). Exact matching alone drops
    # those, so fall back to closest-match, then to leftover slots.
    wanted = [dict(t) for t in titles if t.get("class") == app]
    placed = 0
    leftovers: list[dict] = []

    for win in found:
        claimed.add(win["address"])
        target = take_best_match(win["title"], wanted)
        if target is None:
            leftovers.append(win)
            continue
        move_to(win["address"], target["workspace"])
        print(f"  → {strip_suffix(win['title'])[:40]:42s} ws{target['workspace']}")
        placed += 1

    # The browser reopens what *it* considers the last session, which need not
    # be the session we saved: it can hand back more windows than we asked for
    # (stale profile state) or fewer. Park the extras on the workspaces we
    # still expected to fill, so nothing piles up on the active one.
    for win in leftovers:
        if wanted:
            target = wanted.pop(0)
            move_to(win["address"], target["workspace"])
            print(f"  ~ {strip_suffix(win['title'])[:40]:42s} ws{target['workspace']}"
                  f"  (no title match, filled a saved slot)")
            placed += 1
        else:
            print(f"  ! extra window the browser reopened: "
                  f"{strip_suffix(win['title'])[:40]}")

    # Whatever the browser did not hand back, open ourselves. The tabs of those
    # windows are gone -- only the browser could have restored them, and it
    # did not -- but an empty window on the right workspace preserves the shape
    # of the session, which is what the user navigates by. Leaving a hole would
    # silently shrink the desktop every time a browser exits uncleanly.
    for target in list(wanted):
        known_now = {c["address"] for c in mapped()} | claimed
        dispatch("exec_cmd", f"uwsm app -- {cmd} --new-window")
        win = find_new(known_now, WINDOW_TIMEOUT)
        if win is None:
            print(f"  ! could not open a replacement window for "
                  f"ws{target['workspace']}")
            continue
        claimed.add(win["address"])
        move_to(win["address"], target["workspace"])
        wanted.remove(target)
        print(f"  + empty window → ws{target['workspace']}"
              f"  (browser did not restore \"{strip_suffix(target.get('title',''))[:28]}\")")
        placed += 1

    print(f"[browser] {app}: {placed}/{want} placed "
          f"({len(found)} reopened by the browser)")
    return min(placed, want)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else SESSION
    if not path.is_file():
        print(f"no session file at {path}", file=sys.stderr)
        return 1

    windows = tomllib.loads(path.read_text()).get("window", [])
    sidecar = path.with_suffix("").with_suffix(".titles.json")
    titles: list[dict] = []
    if sidecar.is_file():
        titles = json.loads(sidecar.read_text()).get("windows", [])
        print(f"title sidecar: {len(titles)} window(s) from {sidecar.name}")
    else:
        print("no title sidecar -- browser windows cannot be told apart")

    print(f"restoring {len(windows)} window(s) from {path}\n")

    browser_specs: dict[str, list[dict]] = {}
    plain: list[dict] = []
    for spec in windows:
        app = spec.get("app_id", "")
        if app in BROWSERS and titles:
            browser_specs.setdefault(app, []).append(spec)
        else:
            plain.append(spec)

    claimed: set[str] = set()
    started = time.monotonic()
    ok = 0
    for app, specs in browser_specs.items():
        ok += restore_browser(app, specs, titles, claimed)
    for i, spec in enumerate(plain, start=1):
        ok += restore(spec, claimed, i, len(plain))
    elapsed = time.monotonic() - started

    # Report against the screen, not against our own bookkeeping: reporting
    # success without looking is exactly how hyprresume claims 6/6 on an empty
    # desktop.
    on_screen = mapped()
    print(f"\n{ok}/{len(windows)} placed in {elapsed:.0f}s")
    print(f"windows actually on screen: {len(on_screen)}")
    for c in sorted(on_screen, key=lambda c: (c["workspace"]["id"], c["class"])):
        w, h = c["size"]
        print(f"  ws{c['workspace']['id']}  {c['class']}  {w}x{h}  float={c['floating']}")
    return 0 if ok == len(windows) else 2


if __name__ == "__main__":
    sys.exit(main())
