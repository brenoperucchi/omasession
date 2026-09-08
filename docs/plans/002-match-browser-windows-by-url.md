# 002 — Match browser windows by URL instead of title

**Status:** open. Current matching works but is soft.

## Why

Browser windows share a PID, a class and a command line, so the title is the
only thing separating them. It is unstable: a page caught mid-load reports its
bare domain, and the saved title is the loaded one. Measured: a window saved as
"Hyprland Wiki - Chromium" came back as "wiki.hypr.land - Chromium" and did not
match until normalization was added.

Normalized matching (alphanumerics only, then `difflib` ratio ≥ 0.6, then
leftover slots) covers that case. It will not cover a page whose title changed
for real between save and restore.

## What to try

The URLs are present in plain text inside the browser's own session files
(`Sessions/Session_*`, SNSS format) — confirmed with `strings` during the
investigation, before the reboot problem appeared. Reading the active tab's URL
per window at save time would give a hard key.

Open question: mapping a URL found in the session file back to *which window*
it belonged to. If that turns out to need parsing SNSS properly, weigh it
against the title matching that already works.

## Do not regress

Whatever replaces it must keep the two fallbacks that exist today: normalized
match, then unfilled-slot assignment. Both were added because an exact-match
implementation dropped windows on the floor and piled them on the active
workspace.
