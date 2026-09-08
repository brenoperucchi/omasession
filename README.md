# OmaSession

**Reopens what you had open, where you had it, after a reboot or a crash.**

The thing macOS does with *"Reopen windows when logging back in"* — on
Omarchy 4.

Every session-restore tool for Hyprland today drives the compositor with the
pre-Lua `hyprctl dispatch exec` / `keyword windowrule` syntax. On a
Lua-configured Hyprland 0.56 that syntax is rejected, and rejected *silently*:
the tool reports success against an empty desktop. Measured on a 6-window
session, hyprresume 0.5.0 restores **0 of 6** in 150 seconds and prints
`restore complete: 6/6 apps (0 failed)`.

OmaSession speaks the API the compositor actually accepts.

| | hyprresume 0.5.0 | OmaSession |
|---|---|---|
| 6 mixed windows | 0/6 in 150s | **6/6 in 7s** |
| 3 single-instance Chromium windows | does not launch | **4/4 in 3s** |
| after a real reboot | 0/6 | **5/5 in 5s** |

Geometry, floating state and a terminal's working directory come back
identical.

## Status

Early. The replay engine is validated across four real reboots; the plugin
around it is being assembled. Browser tabs survive a kill but not yet a reboot
— see `docs/plans/001-pre-shutdown-hook.md`.

## Design

`docs/DESIGN.md` — what was measured, what it forced, and where the boundary
with hyprresume sits.

## License

MIT.
