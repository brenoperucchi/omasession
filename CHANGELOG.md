# Changelog

User-visible changes to OmaSession are recorded here. Marketplace verification
is a separate process tied to the exact repository commit in its verification
request.

## Unreleased

- `omarchy plugin add ... --enable` is now the whole install. The plugin
  declares a service that the shell starts once, and it sets up the snapshot
  timer and the login restore line that used to need a separate `omasession
  install`. It runs at every shell start and changes only what is missing or
  out of date. A login line you deleted stays deleted, and nothing is set up
  again after `omasession uninstall`. Requested in the Omarchy plugin
  marketplace review.
- A disabled plugin no longer saves or restores. The timer now runs `save
  --if-enabled`, and the login restore checks the same thing: whether
  omarchy-shell has the plugin enabled. The manual commands still work.
- After `omarchy plugin remove` without `uninstall`, the timer and the login
  line are inert instead of failing. Both units carry
  `ConditionPathExists`, and the login line checks that the CLI exists. A
  line written by an earlier version gets that check in place, keeping the
  path it names.
- The login restore runs once per Hyprland instance. A second caller in the
  same login, such as the old login line together with the new setup, does
  nothing.
- The panel says when automatic save is off: the plugin is not enabled, or
  the snapshot timer is not running.

## 0.5.0 — 2026-10-22

- The panel has three tabs. **Windows** is the old list. **Layout** draws the
  screens to scale from the saved monitor geometry (position, size, scale,
  rotation) with each workspace's windows where they sit, so an ultrawide, a
  row of monitors or a portrait screen all look like themselves. **Saved**
  lists named sessions and the history with Preview and Restore, plus Save as.
  New `omasession layout [name]` gives the panel that geometry as JSON.
  Contributed by @dmkenney in #5.
- Browser app windows are named for their desktop entry or site ("X", not
  "Com Profile 4"), and a terminal saved with its program shows "runs btop"
  instead of "directory not recoverable". Contributed by @dmkenney in #5.
- A history of earlier sessions. Each save of `last` is also kept as
  `history-<time>` when the desktop's shape changed (windows, workspaces,
  slots, commands; not titles), thinned to every change for 15 minutes, one
  per 15 minutes for 2 hours, one per hour for a day and one per day for a
  week. `omasession list` shows them with named sessions (`omasession save
  <name>`); `omasession restore <name>` brings any back. Contributed by
  @dmkenney in #5.
- Every browser profile that had a window comes back, not only Default. The
  save records the browser's open profiles (Local State
  `last_active_profiles`); restore marks each as cleanly closed and launches
  each with `--profile-directory`. Before, only Default reopened and the other
  profiles' windows were lost or came back empty. The Chrome and Chromium
  close-all repair runs on each profile's session, and all of them together
  never reopen more windows than the save had. A single profile starts at
  once; with several, the rest are handed over as soon as the browser shows
  its first window. Contributed by @dmkenney in #9.
- Browser app windows (`--app=URL`, including Omarchy's web apps) reopen on
  their own profile and URL, rebuilt from the window class
  (`brave-x.com__-Profile_4`, or `chrome-x.com__-Default` for both Chrome and
  Chromium). The browser to start comes from the window's process. Before,
  they were saved with the browser's shared command line and reopened as a
  normal browser window. Contributed by @dmkenney in #9.
- A terminal can come back running the program it had open instead of a bare
  shell. List programs with `terminalPrograms` (for example `nvim,btop`);
  only listed ones are relaunched, since rerunning whatever was in the
  foreground could repeat a build or a delete. When one Ghostty process
  serves several windows, each program goes to the window whose title names
  it, then by elimination. Works with Ghostty (`-e`), foot and kitty (`--`).
  The program runs inside your shell, so it finds what your shell config puts
  on PATH and quitting it leaves you at a prompt. Contributed by @dmkenney in
  #5.
- Windows come back in their slots on Lua layouts and other layouts that
  place windows by arrival order. Restore opened each window onto its
  workspace in snapshot order, so those layouts put windows in the wrong
  slots, and a swapped window came back unswapped. Each window's position in
  its workspace is now saved; during restore, windows wait on a hidden
  workspace and then move in that order. A Lua config can report the real
  slot order through a global `window_slot_order()` (see the README).
  Ordering runs only when that function exists, so Dwindle and Master users
  see no change; `restoreOrder true` or `false` overrides that. A save taken
  while windows are parked is skipped, so `last` never loses them.
  Contributed by @dmkenney in #8.

## 0.4.1 — 2026-10-21

- The session panel shows window titles, commands and error messages as plain
  text. A saved window title containing markup, such as an image tag, could
  make the panel load a remote image when it was opened. Reported in the
  Omarchy plugin marketplace review.
- Chromium and Electron windows saved through the /proc cmdline fallback now
  restore. Their rewritten process title was saved as one quoted word, and
  uwsm failed with `Path "<whole command line>" does not exist!`.
  Contributed by @paracycle in #4 (#3).

## 0.4.0 — 2026-10-21

- Restores Dwindle tiled layouts from the saved window geometry. After the
  windows come back, OmaSession rebuilds the split tree and checks the result
  against the snapshot. Contributed by @Tech0001 in #2; requirements and limits
  are in `docs/tiled-layout.md`.
- Chrome and Chromium now bring back every window after a reboot, shutdown or
  logout from the Omarchy menu. The menu closes windows one at a time, Chrome
  kept only the last one, and the restore filled the rest with empty windows.
  OmaSession now undoes those closes in the session file before launching the
  browser, keeps the original file aside and logs the decision in
  `last-restore.log`.
- A cleanup step that fails after a tiled layout was rebuilt and verified, such
  as handing focus back to a window that is gone, is reported as a warning
  instead of a failed restore.

## 0.3.1 — 2026-09-20

- Added separate **Restore App Windows** cards for Google Chrome and Ghostty,
  with independent app toggles.
- Added native browser app restoration: the Chrome card can ask Chrome to
  restore its saved windows and tabs, then apply the saved Hyprland workspace
  placement.
- Preserved the separate repair path for Chrome windows that are already open,
  while making exact browser-title matches win before approximate matches.
- Improved restore behavior when a browser title sidecar is unavailable and
  made the panel/status fallback safer when an auxiliary read fails.
- Added the isolated Chrome restart diagnostic and expanded coverage for the
  browser restore and panel commands.

## 0.3.0 — 2026-09-17

- Added the Chrome automatic browser-repair toggle.
- Documented OmaBackup as the companion project for restoring Omarchy's dotfiles
  and configuration.
