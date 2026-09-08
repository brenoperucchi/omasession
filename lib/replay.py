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
import re
import subprocess
import sys
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
BROWSERS = {
    "chromium": {
        "profile": "~/.config/chromium/Default/Preferences",
        "binary": "chromium",
        "policy_dir": "/etc/chromium/policies/managed",
    },
    "google-chrome": {
        "profile": "~/.config/google-chrome/Default/Preferences",
        "binary": "google-chrome-stable",
        "policy_dir": "/etc/opt/chrome/policies/managed",
    },
    "brave-browser": {
        "profile": "~/.config/BraveSoftware/Brave-Browser/Default/Preferences",
        "binary": "brave",
        "policy_dir": "/etc/brave/policies/managed",
    },
    "vivaldi-stable": {
        "profile": "~/.config/vivaldi/Default/Preferences",
        "binary": "vivaldi-stable",
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
    sel = f'window="address:{addr}"'
    ws = spec.get("workspace")
    if ws is not None:
        hypr(f"hl.dispatch(hl.dsp.window.move{{{sel}, workspace={ws}}})")
        time.sleep(SETTLE)

    if not spec.get("floating"):
        return

    hypr(f"hl.dispatch(hl.dsp.window.float{{{sel}}})")
    time.sleep(SETTLE)
    size = spec.get("size") or [None, None]
    pos = spec.get("position") or [None, None]
    if size[0] and size[1]:
        hypr(
            f"hl.dispatch(hl.dsp.window.resize{{{sel}, "
            f"x={size[0]}, y={size[1]}, exact=true}})"
        )
        time.sleep(SETTLE)
    if pos[0] is not None and pos[1] is not None:
        hypr(f"hl.dispatch(hl.dsp.window.move{{{sel}, x={pos[0]}, y={pos[1]}}})")
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
    cwd = spec.get("cwd")
    if cwd and app == "foot" and "--working-directory" not in cmd:
        cmd = f"{cmd} --working-directory={cwd}"

    known = {c["address"] for c in mapped()} | claimed
    hypr(f'hl.dispatch(hl.dsp.exec_cmd("uwsm app -- {cmd}"))')

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
    hypr(f'hl.dispatch(hl.dsp.window.move{{window="address:{addr}", '
         f"workspace={workspace}}})")
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


def arm_browser_profile(app: str) -> bool:
    """Make the profile restore silently on the next launch.

    Everything here is user-owned; the root-owned half (the promo policy) is
    installed once by browser-setup.sh. If that half is missing we say so
    rather than fail quietly, because the symptom -- an onboarding tab where
    the session should be -- looks nothing like its cause.
    """
    spec = BROWSERS[app]
    prefs = Path(spec["profile"]).expanduser()
    if not prefs.is_file():
        return False

    policy = Path(spec["policy_dir"]) / "omasession-no-promo.json"
    if app == "google-chrome" and not policy.is_file():
        print(f"  ! {policy} missing -- Chrome may show onboarding instead of "
              f"restoring (run browser-setup.sh once, as root)")

    try:
        data = json.loads(prefs.read_text())
        # Restore the previous session, and let it happen without a click.
        data.setdefault("session", {})["restore_on_startup"] = 1
        data.setdefault("profile", {})["exit_type"] = "Normal"
        data["profile"]["exited_cleanly"] = True
        # Mark the current milestone as already seen. Re-read every time: the
        # browser updates itself, and a stale value brings the promo tab back.
        major = browser_major_version(spec["binary"])
        if major is not None:
            data.setdefault("browser", {})["last_whats_new_version"] = major
        prefs.write_text(json.dumps(data))
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
    hypr(f'hl.dispatch(hl.dsp.exec_cmd("uwsm app -- {cmd}"))')

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
        hypr(f'hl.dispatch(hl.dsp.exec_cmd("uwsm app -- {cmd} --new-window"))')
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
