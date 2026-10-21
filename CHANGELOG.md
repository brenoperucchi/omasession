# Changelog

User-visible changes to OmaSession are recorded here. Marketplace verification
is a separate process tied to the exact repository commit in its verification
request.

## Unreleased

- A history of earlier sessions. Each save of `last` is also kept as
  `history-<time>` when the desktop's shape changed (windows, workspaces,
  slots, commands; not titles), thinned to every change for 15 minutes, one
  per 15 minutes for 2 hours, one per hour for a day and one per day for a
  week. `omasession list` shows them with named sessions (`omasession save
  <name>`); `omasession restore <name>` brings any back.

- Every browser profile that had a window comes back, not only Default. The
  save records the browser's open profiles (Local State
  `last_active_profiles`); restore marks each as cleanly closed and launches
  each with `--profile-directory`. Before, only Default reopened and the other
  profiles' windows were lost or came back empty.
- Browser app windows (`--app=URL`, including Omarchy's web apps) reopen on
  their own profile and URL, rebuilt from the window class
  (`brave-x.com__-Profile_4`). Before, they were saved with the browser's
  shared command line and reopened as a normal browser window.

- A terminal can come back running the program it had open instead of a bare
  shell. List programs with `terminalPrograms` (for example `nvim,btop`);
  only listed ones are relaunched, since rerunning whatever was in the
  foreground could repeat a build or a delete. When one Ghostty process
  serves several windows, each program goes to the window whose title names
  it, then by elimination. Works with Ghostty (`-e`), foot and kitty (`--`).

- Windows come back in their slots on Lua layouts and other layouts that
  place windows by arrival order. Restore opened each window onto its
  workspace in snapshot order, so those layouts put windows in the wrong
  slots, and a swapped window came back unswapped. Each window's position in
  its workspace is now saved; during restore, windows wait on a hidden
  workspace and then move in that order. A Lua config can report the real
  slot order through a global `window_slot_order()` (see the README). Off
  with `restoreOrder false`.

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
