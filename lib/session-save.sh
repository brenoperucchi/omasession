#!/usr/bin/env bash
# Save a session: hyprresume's own save, plus the one field it does not record.
#
# hyprresume writes app_id, workspace, geometry and cwd -- everything except
# what distinguishes one browser window from another. Without a title there is
# no way to send a restored Chromium window back to the workspace it came from,
# because all of them share a PID, a command line and a class.
#
# The sidecar is additive: hyprresume's last.toml is untouched.

set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    HYPRLAND_INSTANCE_SIGNATURE="$(ls -t "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)"
    export HYPRLAND_INSTANCE_SIGNATURE
fi

SESSION_DIR="${HYPRRESUME_SESSION_DIR:-$HOME/.local/share/hyprresume/sessions}"
NAME="${1:-last}"
SIDECAR="$SESSION_DIR/$NAME.titles.json"

hyprresume save "$NAME" >/dev/null

# Refuse to overwrite a populated sidecar with an empty one: a save that runs
# while the desktop is still coming up must not erase yesterday's session.
new_count="$(hyprctl clients -j | jq '[.[] | select(.mapped) | select(.workspace.id > 0)] | length')"
if [[ -f "$SIDECAR" ]] && (( new_count == 0 )); then
    old_count="$(jq '.windows | length' "$SIDECAR" 2>/dev/null || echo 0)"
    if (( old_count > 0 )); then
        echo "session-save: refusing to overwrite $old_count saved windows with 0" >&2
        exit 0
    fi
fi

hyprctl clients -j | jq --arg when "$(date -u +%FT%TZ)" '{
    when: $when,
    windows: [ .[]
        | select(.mapped) | select(.workspace.id > 0)
        | {class, title, pid,
           workspace: .workspace.id,
           monitor: .monitor,
           at, size, floating} ]
}' > "$SIDECAR"

echo "session-save: $NAME -- $(jq '.windows | length' "$SIDECAR") windows (+ titles)"
