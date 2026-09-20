# Restoring Dwindle layouts

After restoring windows, OmaSession associates each saved tile with its live
window address and reconstructs Dwindle's split tree from the saved rectangles.
It uses directional preselection and exact split ratios, temporarily floating
the participating windows. They finish tiled; the focus and cursor from before
reconstruction are restored. The result is checked against the inferred tree,
with a three-pixel allowance for compositor rounding.

This adapts the tiled-layout work from [PR #1](https://github.com/brenoperucchi/omasession/pull/1)
to upstream 0.3.1. The current browser launch, profile handling, title matching,
logging and Ghostty resolver remain in place. Browser title records now retain
their link to saved geometry. Existing non-browser windows reserve exact titles
before class-only adoption; identical or changed titles still require a stable
fallback and cannot prove which identical window is which.

## Requirements and limits

- Dwindle, with `dwindle:preserve_split` and `dwindle:use_active_for_splits` enabled.
- Uniform, symmetric inner gaps and borders compatible with the snapshot.
- A complete rectangular partition of 2–64 saved tiles, with one distinct live
  window per saved tile and no extra live tiles on the workspace.
- No fullscreen, hidden, pinned or grouped participating windows.

The saved monitor is used when available. If it is disconnected, the current
monitor is retained and the proportions scale to the available area. Monitor
movement is skipped if it would displace unrelated floating windows. A changed
gap/border configuration or per-window geometry rules can make a snapshot
incompatible; the geometry checks refuse invalid partitions before mutation
and report any mismatch after reconstruction.

App-only restore and disabled-app policies retain the other saved tiles in
their completeness checks. They skip reconstruction of mixed-app workspaces
when any saved tile was excluded, including when that app is not running.
Unrelated workspaces are not reconstructed. One-window and floating-only
workspaces need no split reconstruction.

Logs use the existing `~/.local/state/omasession/last-restore.log`. `[layout]`
lines distinguish a verified reconstruction, a skipped workspace, and a failed
attempt. A failed attempt makes restore exit with status 2 even when all
applications were placed. Skipping an unsupported layout does not by itself
fail an otherwise successful app restore.

Every layout dispatcher is checked. Cleanup attempts to return all participating
windows to tiling, clear split preselection and restore focus/cursor even if an
intermediate dispatcher fails. IPC timeouts are failures, and trigger another
cleanup attempt. Cleanup cannot guarantee recovery while the compositor is
unavailable; its failure is logged. A failed reconstruction may leave a different
split arrangement, so it is not described as an atomic rollback.

## Testing

The automated checks use mocks and do not access the compositor:

```sh
python3 -B test/tiled-layout-cases.py
```

The opt-in live check requires Hyprland and `foot`:

```sh
python3 -B tools/test-tiled-layout.py --live
```

It pauses the snapshot timer, creates disposable terminals on an unused
workspace, and checks both adoption and relaunch of an unequal five-window
layout. It also checks preservation of focus/cursor, rejection of extra tiles,
and cleanup after a failed dispatcher. Its session files and restore log are
temporary; the normal saved session is never replayed. The test closes its own
windows and resumes the timer afterward.

For manual testing, save a named snapshot so the automatic `last` snapshot does
not overwrite the arrangement you are testing:

```sh
OMASESSION=~/.config/omarchy/plugins/brenoperucchi.omasession/bin/omasession
$OMASESSION save layout-test
# Rearrange the same windows, then restore the named snapshot:
$OMASESSION restore layout-test
```

Live checks on Hyprland 0.56.2 reproduced the five-window layout within two
pixels for both existing windows and relaunched windows. A full reboot and
physical multi-monitor restore remain to be tested.
