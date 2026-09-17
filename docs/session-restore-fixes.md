# Terminal folders, browser tabs, and tiled placement

This branch fixes three failures found during a real reboot on Omarchy with
Hyprland 0.56.2, Ghostty 1.3.1, and Chromium 152.

## Terminal folders

Ghostty can spawn shells from worker threads. Reading only the main thread's
`children` file misses those shells, so the resolver now visits every thread.

Existing Ghostty windows may share a process. When its shells have different
folders, a window title with an explicit folder suffix can identify one distinct
live directory. Ambiguous folder names remain unresolved. This fallback is a
heuristic for existing shared-process windows, not a general window-to-shell API.

Saved Ghostty launch commands use `--gtk-single-instance=false` and the current
working directory. This prevents a shared instance from ignoring the folder and
makes subsequent snapshots of those restored windows unambiguous. Environment
wrappers and arguments to programs running inside the terminal are preserved.

For independently launched windows to have the same behavior, users can also set
`gtk-single-instance = false` in their Ghostty configuration. Desktop entries
that explicitly pass `--gtk-single-instance=true` override that setting; a user
desktop override should use `false` and `DBusActivatable=false`. This branch does
not automatically change terminal configuration or desktop entries. Windows
created inside an existing Ghostty process can still share its process.

## Browser tabs

The previous launch path cleared the browser's crash marker but did not request
session restoration. It then opened blank replacement windows when the browser
did not return its tabs.

The launch command now explicitly includes `--restore-last-session`. An already
running browser keeps its current tabs and profile. A window is never closed
merely because its active tab is titled `New Tab`, and missing restored windows
are reported without creating empty replacements.

## Window placement

Snapshots now include window titles alongside their geometry. Restored windows
are associated with their saved entries, and newly launched windows must match
the expected application class before they are placed.

For Dwindle workspaces, the saved rectangles are partitioned into a binary split
tree. Restoration reconstructs the splits with `preselect` and exact split
ratios. Windows temporarily float while the tree is rebuilt and finish tiled.
Focus and cursor position are returned afterward. A saved monitor is used when
it is available. Floating windows use the current Lua dispatcher's
`relative=false` for absolute position and size.

Tiled reconstruction requires `dwindle:preserve_split` and
`dwindle:use_active_for_splits` to be enabled, symmetric inner gaps, and a complete
set of restored windows on the workspace. Workspaces with extra or missing tiled
windows, unsupported layouts, or incompatible geometry are left unchanged and
reported. Small pixel differences from rounding are expected.

## Diagnostics and validation

Restore output is also written to
`${XDG_STATE_HOME:-~/.local/state}/omasession/last-restore.log`, including failures.
This is useful when the login process's stdout is not retained by UWSM.

Run the regression checks without opening or changing desktop windows:

```sh
python3 test/local-fixes.py
```

The 12 checks cover folder disambiguation, argument handling, preservation of
running browsers, avoiding blank replacements, and split-tree reconstruction.
Additional live checks were performed with disposable applications:

- Five tiled windows reproduced a saved arrangement within three pixels.
- Floating position and size matched exactly.
- Two independent Ghostty windows captured distinct folders, including a path
  containing spaces and a semicolon.
- An isolated Chromium profile restored both saved tabs after its exit marker
  was changed to simulate an unclean shutdown.

These checks validate the individual fixes. A full reboot with the combined
changes has not yet been verified. Scratchpad capture, Files folder restoration,
and preserving running terminal programs are not added by this branch.
