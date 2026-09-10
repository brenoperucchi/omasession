# OmaSession

**Reopens what you had open, where you had it, after a reboot or a crash.**

The thing macOS does with *"Reopen windows when logging back in"* — on
Omarchy 4.

![OmaSession panel](screenshots/panel-healthy.png)

> **Status: usable end to end.** Save, restore, the guard, the resolver and the
> panel all read and write through the real CLI now — no mock, no fabricated
> data. The bar widget above is a live screenshot: `status --json` produces the
> contract, `Process` in `Panel.qml` reads it, the buttons and the login toggle
> call back into the same CLI. See
> [Where it actually is](#where-it-actually-is) for what is verified where.

## Why a new plugin

The tools that predate Omarchy 4 drive the compositor with the pre-Lua
`hyprctl dispatch exec` / `keyword windowrule` syntax. On a Lua-configured
Hyprland 0.56 — the Omarchy 4 default — that syntax is rejected, and rejected
*silently*: the tool reports success against an empty desktop. Measured on a
6-window session, hyprresume 0.5.0 restores **0 of 6** in 150 seconds and prints
`restore complete: 6/6 apps (0 failed)`.

That is no longer the whole story, and this README used to claim it was.
`dimef.omaresume`, published to the plugin marketplace on 2026-09-08, speaks the
same Lua dispatcher API. Speaking the API the compositor accepts is table
stakes, not a differentiator. What separates this plugin is below: it resolves
arbitrary applications instead of a fixed list, and it knows what the browser
actually needs in order to give its tabs back.

| | hyprresume 0.5.0 | OmaSession |
|---|---|---|
| 6 mixed windows | 0/6 in 150s | **6/6 in 7s** |
| single-instance Chromium windows | does not launch | **4/4 in 3s** |
| after a real reboot | 0 restored | **4/4 in 5s**, plus the browser's own tabs |

The middle row was measured on a desktop where Chromium had reopened four
windows, not three; the reboot row counts a later run of its own. Both used to
be written against a denominator from a different measurement, which is exactly
the kind of number this README criticises elsewhere.

Geometry, floating state and a terminal's working directory come back identical.
## Any application, not a list of eight

A session restorer has to answer one question per window: what command brings
this back? The usual answer is a table of known applications — four terminals,
four browsers — and anything else needs the user to write out an `argv` by hand,
or it simply does not come back.

`lib/resolve.py` answers it from the system instead: the `.desktop` index
(matched on `StartupWMClass` or entry id, with `Exec=` field codes stripped),
the systemd scope for Flatpaks, `/proc/cmdline` as a last resort, and the
terminal's real working directory. Measured by closing everything and
relaunching **only** from the resolved command: 4/4 in the lab, and 19/19
against a real 19-window desktop. `omasession resolve` prints which path each
window took, because a resolver that is wrong looks exactly like an application
that failed to open.

A window it cannot resolve is reported as such — before the reboot, in the
panel, not discovered afterwards.

## Browser tabs come back too, and the reason is not what we assumed

The open question was why browser tabs never survived a reboot. The answer,
measured across two reboots with three URL sentinels per browser and the tabs
the browser *actually reopens* as the oracle:

**Nothing is lost at shutdown.** The session file crosses the reboot
byte-for-byte — same name, same size, all sentinels present in a copy taken
before any GUI or browser started. What blocks the restore is one line in the
profile: `profile.exit_type = "Crashed"`. The browser is killed by the session
teardown, marks the profile crashed, and refuses to auto-restore.

Same snapshot, Chromium 151 and Chrome 152: **3/3 with the profile armed, 0/3
without**. No pre-shutdown hook is needed — which also means it works for the
power loss and GPU lockups no hook could ever cover.

Full result and the six hypotheses it eliminated:
[`docs/plans/001-RESULTADO.md`](docs/plans/001-RESULTADO.md).

## Never trade a good session for a worse one

A session saver's worst failure is not missing a save — it is overwriting a good
session with a bad one. Both happened during development:

- hyprresume's daemon replaced a five-window `last.toml` with a one-window one
  and left it beside a five-window sidecar. Nothing was zero, and the session
  was ruined anyway.
- An earlier guard here printed `refusing to overwrite 3 saved windows with 0`,
  exited **0**, and destroyed the file regardless — because it ran *after* the
  write it was meant to prevent.

So `lib/session-save.sh` stages every save into its own generation, validates it
against the screen it was taken from, fsyncs it, and only then publishes it —
never editing the file a restore might read mid-write. A save that cannot prove
itself never overwrites one that could. `test/guard-cases.sh` covers the cases
(25 assertions, run against a live Hyprland, including killing the save at
random and at the exact instant between the two publish renames).

## Where it actually is

| Piece | State |
|---|---|
| `lib/replay.py` — the replay engine | **works**, validated across four real reboots |
| `lib/session-save.sh` — save + guard | **works**, 25/25 in `test/guard-cases.sh` |
| `lib/resolve.py` — arbitrary application resolver | **works**, 19/19 against a real desktop |
| `bin/browser-setup` — per-vendor policy | **works** |
| Browser tab restore | **understood and measured** — see the `exit_type` finding above |
| `bin/omasession` — CLI (save/restore/status/resolve/install/uninstall/config) | **works**, exercised end to end in the lab |
| `systemd/` snapshot timer | **works**, written and enabled by `omasession install` |
| `Panel.qml` — bar widget | **works**, reads `status --json` live and calls back into the CLI |

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — what was measured, what it forced, and
  why hyprresume is no longer in the execution path at all.
- [`docs/PANEL.md`](docs/PANEL.md) — the panel's two states, and the Omarchy
  shell traps that cost the most (a bar widget with no `implicitWidth` renders
  nothing and reports nothing).
- [`docs/plans/`](docs/plans/) — open questions and closed ones.

Every number in these documents is an observation from a QEMU guest running
Omarchy 4.0.1 with Hyprland 0.56.2, not an estimate.

## License

MIT.
