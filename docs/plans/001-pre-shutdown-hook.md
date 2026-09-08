# 001 — Pre-shutdown hook: make browsers persist before the session dies

**Status:** open. This is the last gap between "windows come back" and the
macOS-parity the project is for.

## The observation

After a real reboot, `~/.config/<browser>/Default/Sessions/` contains **zero**
of the URLs that were open — including the file written while the pages were
still up. Placement restores; tabs do not.

Browser content survived only in tests that did not cross a reboot (kill the
browser, restore in the same Hyprland session: 3/3 tabs back). Every test that
crossed a reboot lost it, across three attempts, with Chromium and Chrome, with
the session files preserved and with the `RestoreOnStartup` policy in place.

The browser does not finish writing its session when systemd tears the
graphical session down.

## What to build

A unit ordered before the graphical session stops, that:

1. sends SIGTERM to the Chromium-family processes it knows about,
2. waits for them to exit (or for a deadline), and only then lets shutdown
   proceed,
3. is bounded — a browser that refuses to die must not hang the machine.

Roughly `Before=` the user session teardown, `TimeoutStopSec` short enough that
a stuck browser costs seconds, not a hung shutdown.

## How to verify it worked

Do not measure the number of windows. Measure the URLs in the session file:

```bash
find ~/.config/chromium/Default/Sessions -type f \
  -exec sh -c 'strings "$1" | grep -c "archlinux\.org"' _ {} \;
```

Non-zero after a reboot means the hook did its job. Zero means it did not,
regardless of how many windows came back — empty windows on the right
workspaces is the current behaviour and is not the goal here.

## Traps

- A power loss gives no chance to run anything. This hook helps clean reboots
  and shutdowns, which is most of the cases, and does nothing for the
  `amdgpu` lockups that motivated the short snapshot interval. Both matter;
  neither substitutes the other.
- Do not kill browsers with `pkill -f <pattern>` from a script: the pattern
  matches the script's own command line and kills the session running it. That
  mistake invalidated two measurements during the investigation. Resolve PIDs
  and signal them individually.
