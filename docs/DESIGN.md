# OmaSession — design

Reopen the windows you had, on the workspaces they were on, after a reboot or
a crash. What macOS does with *"Reopen windows when logging back in"*, on
Omarchy 4.

Everything below was measured in a QEMU guest running Omarchy 4.0.1 with
Hyprland 0.56.2 (`efb50993`) — the same Hyprland commit as the host — over
four real reboots on 2026-09-08. Numbers in this document are observations,
not estimates.

## 1. Why a new plugin, when six tools already exist

hyprflow, hypr-session-restore, hyprland-session-saver, hyprsession, hyprdrover
and hyprresume all save and restore Hyprland sessions. **None of them works on
Omarchy 4**, for one shared reason.

With a Lua config — the Omarchy 4 default — `hyprctl` evaluates its argument as
Lua, and the pre-Lua forms are rejected:

```
hyprctl dispatch exec foot            → error: ')' expected near 'foot'
hyprctl dispatch workspace 3          → error: ')' expected near '3'
hyprctl keyword general:gaps_out 10   → "keyword can't work with non-legacy parsers. Use eval."
```

That is exactly how every one of those tools relaunches and places windows.
`strings /usr/bin/hyprresume` shows it emitting `exec [workspace `, `keyword `,
`windowrule[`, `movetoworkspacesilent` and `focuswindow address:0x`.

Hyprland returns no error through that path, so the tool believes it worked.
Measured on a 6-window session:

| | hyprresume 0.5.0 | this replay, same session file |
|---|---|---|
| 6 mixed windows | **0/6** in 150s, reporting `6/6 apps (0 failed)` | **6/6** in 7s |
| 3 single-instance Chromium windows | does not launch | 4/4 in 3s |
| real reboot | 0/6 | 5/5 in 5s |

The gap is not quality — it is a compositor API that moved.

## 2. Hyprresume: out of the execution path since 2026-09-10

This section used to describe a boundary: hyprresume's **save** was kept
because it resolved a window to a command and walked a terminal's process tree
for its real cwd, and only its **restore** was replaced (§1). That boundary is
now crossed — `lib/capture.py` (a `hyprctl`-based capture) and `lib/resolve.py`
(§ below, measured in `docs/plans/003-command-resolver.md`) do the whole job,
and hyprresume is not invoked, checked for, or disarmed anywhere in this
plugin. `bin/omasession install` no longer requires it, and there is nothing
left in `cmd_install`/`cmd_uninstall` that knows it exists.

```
observe + resolve command + cwd   →  lib/capture.py + lib/resolve.py  (ours)
session format (last.toml)        →  ours, same shape hyprresume wrote
window titles                     →  our sidecar          (§4)
replay into the compositor        →  ours                 (replaced in §1)
browser content                   →  the browser itself   (orchestrated, §5)
panel / autostart / timers        →  ours
```

Why it had to go, not just be worked around: hyprresume resolves a window with
no `.desktop` entry — a custom `--class`/`--app-id` set by the launcher itself
— through `/proc/<pid>/exe`, the binary, discarding every command-line
argument. Measured on a real case, not a synthetic one: the Herdr
terminal-workspace-manager runs its TUI as `foot --app-id=TUI.tile herdr`, and

    $ hyprresume resolve TUI.tile
    TUI.tile → foot

so after a reboot the window came back as an empty terminal, herdr never
started. `omasession resolve` — `lib/resolve.py`, reading `/proc/<pid>/cmdline`
— already resolved this same window correctly before this change; the gap was
that `last.toml` came from `hyprresume save`, not from that resolver.
`lib/capture.py` is what puts the resolver on the path the replay actually
reads. Confirmed on 2026-09-10 with a real reboot in the lab: the window now
comes back running herdr, not a bare `foot`, and without the duplication that
appeared alongside the wrong command.

Two failure modes of hyprresume that this plugin does not inherit — kept for
the record, since the second still shapes `session-save.sh` today:

1. **It reports success it never verified** — `6/6 apps (0 failed)` against an
   empty desktop. Count `hyprctl clients` after the pass and report that.
2. **A failed restore destroys the saved session.** With `restore_on_start` and
   a 120s autosave, hyprresume's daemon starts, restores nothing, and two
   minutes later writes the empty desktop over a good `last.toml`. A real
   6-window session was lost this way during testing.

   That specific race is gone by construction now: hyprresume's daemon, if
   installed, writes its own directory
   (`~/.local/share/hyprresume/sessions`); we write ours
   (`~/.local/share/omasession/sessions`). Two processes writing two different
   files cannot tear the same pair. But the invariant it forced is still the
   right one, because our own capture can still fail on its own — a `hyprctl`
   timeout, a save killed mid-write — so `session-save.sh` still enforces
   **never replace a saved session with a worse one, and never publish a pair
   whose two halves came from different saves**:

   Each save stages into a generation of its own, is validated against the
   screen it was taken from, gets an fsync (capture.py writes to stdout, not to
   disk — this script decides if and when the bytes are durable), and only
   then becomes `last`, with the generation it replaces kept as `last.prev`.
   Both halves carry the same generation stamp, because two renames are not a
   transaction: rather than pretend otherwise, the replay detects a torn pair
   and falls back to the previous generation instead of matching browser
   windows against titles from a different capture.

   Measured: 25 assertions in `test/guard-cases.sh`, run against a live
   Hyprland with the capture.py-based pipeline, including killing the save at
   random and at the exact instant between the two publish renames — 25/25.

## 3. The Hyprland 0.56 Lua IPC

`hyprctl repl` prints return values; `hyprctl eval` only prints `ok`. Verified:

```bash
hyprctl repl 'hl.dispatch(hl.dsp.exec_cmd("uwsm app -- foot"))'
hyprctl repl 'hl.dispatch(hl.dsp.focus{workspace=3})'
hyprctl repl 'hl.dispatch(hl.dsp.window.move{window="address:0x…", workspace=5})'
hyprctl repl 'hl.dispatch(hl.dsp.window.float{window="address:0x…"})'
hyprctl repl 'hl.dispatch(hl.dsp.window.resize{window="address:0x…", x=800, y=500, exact=true})'
hyprctl repl 'hl.dispatch(hl.dsp.window.close{window="address:0x…"})'
```

- Dispatchers only run inside `hl.dispatch(...)`; alone they return an
  `HL.Dispatcher` object.
- `hl.dsp.window.move` accepts: `direction`, `x`+`y`(+`relative`), `workspace`,
  `into_group`, `out_of_group`.
- `hl.dsp.focus` is a function taking a table (`{workspace=N}`,
  `{direction="left"}`), not a namespace.
- `hyprctl` reports addresses **with** the `0x` prefix. Do not add another.
- Omarchy 4 autostart is `o.launch_on_start("…")` in `~/.config/hypr/autostart.lua`.
  The `exec-once = …` that every upstream README suggests does not exist here.

## 4. Placement rules that the measurements forced

- **Place by address after the window maps.** Relying on the workspace focused
  at launch time fails for single-instance apps: the window is created by the
  pre-existing process and lands arbitrarily. A window saved on ws5 came back
  on ws3 until this changed.
- **Adopt a surviving window** of the same class instead of counting it a
  failure — a restore can run when part of the session lived.
- **Title is the only key that separates browser windows** (they share a PID, a
  class and a command line), and it is a soft key: a page caught mid-load
  reports its bare domain. Match exact first, then on titles normalized to
  alphanumerics (`wiki.hypr.land` ↔ `Hyprland Wiki` share the block
  `hyprland`), then fall back to unfilled slots.
- **Wait adaptively, not on a fixed timeout.** Browsers reopen windows in a
  burst; stop after ~4s of no new window. Two browsers × 40s fixed turned a 3s
  restore into 82s.

## 5. Browsers: the app restores content, we restore placement

Tabs and URLs cannot be reconstructed from outside — the windows share one PID
and one `/proc/cmdline` that does not even contain the URLs. So let the browser
reopen its own windows, then place them. Requirements, per vendor:

| | Chromium | Chrome |
|---|---|---|
| `profile.exit_type = "Normal"` (crashed profiles wait for a click) | needed | needed |
| `RestoreOnStartup: 1` **via policy** | needed | needed |
| `PromotionalTabsEnabled: false` | harmless | **needed** |
| `browser.last_whats_new_version` | harmless | **needed** |

Two traps, both discovered the hard way:

- `restore_on_startup` written into `Preferences` **is ignored**. Chrome guards
  it with an HMAC in `Secure Preferences` and treats hand-editing as tampering.
  Forging it appeared to work only after a hard kill, because crash recovery
  takes another path; it never survived a clean reboot. It has to come from the
  policy file.
- Chrome replaces session restore with its onboarding pages on the first launch
  after every version milestone — and Chrome updates itself. `--no-first-run`
  does not stop it. Without the policy, the first restore after each Chrome
  update silently loses every browser window.

### The open problem

After a reboot the session files under `~/.config/<browser>/Default/Sessions/`
contain **zero** of the URLs that were open — including the file written while
the pages were up. Browser content survived only in tests that did *not* cross
a reboot.

The browser does not finish writing its session when systemd tears the
graphical session down. **This plugin needs a pre-shutdown hook** that signals
the browsers and waits for them to persist, before shutdown proceeds. That is
what macOS does at logout, and it is the last missing piece of the parity.
Until it exists, placement is restored and tabs are not.

## 6. Layout

```
bin/omasession        CLI: save, restore, status, resolve, install, config
lib/replay.py         the replay engine (validated)
lib/capture.py        our own hyprctl-based capture (§2)
lib/resolve.py        the command resolver capture.py calls (docs/plans/003)
lib/session-save.sh   capture + title sidecar + generation guard
bin/browser-setup     lists per-vendor policy paths; the write is a manual
                       `sudo install`/`sudo rm` in the README, not a script
systemd/              snapshot timer, pre-shutdown hook (pending)
Panel.qml             bar widget
```

Heavy lifting stays outside QML: a crash inside quickshell takes down the bar,
the dock and the menu at once, and restoring has to work when there is no shell.

## 7. Lab

`~/VMs/hyprsession-lab/` — `boot.sh` (`--reset` returns to the golden image),
`probe.sh` (builds scenarios, snapshots), plus the scripts above. It boots a
copy-on-write overlay of the omabackup golden image; the golden is never
written. Guest has Chromium and Chrome installed, policies applied.

A full cycle — build a scenario, save, reboot, verify — takes about 4 minutes.
