#!/usr/bin/env bash
# Save a session: hyprresume's own save, plus the one field it does not record,
# behind a guard that refuses to replace a good session with a worse one.
#
# hyprresume writes app_id, workspace, geometry and cwd -- everything except
# what distinguishes one browser window from another. Without a title there is
# no way to send a restored Chromium window back to the workspace it came from,
# because all of them share a PID, a command line and a class.
#
# Why the guard is not just "refuse when the screen is empty":
#
#   Measured in the lab on 2026-09-08, with hyprresume's own daemon running:
#   last.toml held ONE window (a foot, autosaved while only it was up) next to a
#   sidecar holding FIVE from an hour earlier. Nothing was zero. A good session
#   was replaced by a worse one, and the pair was left desynchronised -- the
#   replay then reads one window from the toml while printing "title sidecar: 5
#   window(s)", and reports success.
#
# So the invariant is "never replace a good session with a worse one", and that
# is only checkable AFTER the new content exists. hyprresume writes last.toml in
# place and honours no output-directory option (there is no HYPRRESUME_* string
# in the 0.5.0 binary), so the only way to validate before publishing is to back
# both files up, let it write, check what came out, and roll back if it is worse.
#
# Exit codes:  0 saved   3 refused (session preserved)   4 another save running

set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    HYPRLAND_INSTANCE_SIGNATURE="$(ls -t "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)"
    export HYPRLAND_INSTANCE_SIGNATURE
fi

SESSION_DIR="${OMASESSION_SESSION_DIR:-$HOME/.local/share/hyprresume/sessions}"
NAME="${1:-last}"
TOML="$SESSION_DIR/$NAME.toml"
SIDECAR="$SESSION_DIR/$NAME.titles.json"

say() { printf 'session-save: %s\n' "$*"; }
err() { printf 'session-save: %s\n' "$*" >&2; }

mkdir -p "$SESSION_DIR"

# One save at a time. The snapshot timer fires every 30s by default and a slow
# hyprctl is enough to overlap two runs; two hyprresume saves racing on the same
# file is a way to produce exactly the truncated toml this guard exists to catch.
exec 9>"$SESSION_DIR/.$NAME.lock"
if ! flock -n 9; then
    err "another save is already running -- skipping this tick"
    exit 4
fi

# Read the screen ONCE. Counting here and building the sidecar from a second
# read lets windows appear or vanish in between, which silently decides the
# guard on different evidence than the one it protects.
if ! clients="$(hyprctl clients -j 2>/dev/null)" || [[ -z "$clients" ]]; then
    err "cannot read hyprctl clients -- session left untouched"
    exit 3
fi

# Special workspaces (scratchpad) have negative ids and are excluded here, as
# they are in the replay: neither side can place them. A desktop whose windows
# are all scratchpadded therefore looks empty to this script, and the floor rule
# below will refuse to save it -- which preserves the previous session rather
# than erasing it. Deliberate; revisit only together with the replay.
count_screen="$(jq '[.[] | select(.mapped) | select(.workspace.id > 0)] | length' <<<"$clients")"
count_scratch="$(jq '[.[] | select(.mapped) | select(.workspace.id < 0)] | length' <<<"$clients")"

# Count [[window]] entries by actually parsing the file: a toml that no longer
# parses is a failed save, not a small one, and must not be published either.
toml_windows() {
    [[ -f "$1" ]] || { echo 0; return; }
    python3 - "$1" <<'PY' 2>/dev/null || echo -1
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(len(tomllib.load(fh).get("window", [])))
PY
}

count_old="$(toml_windows "$TOML")"
(( count_old >= 0 )) || { err "existing $NAME.toml does not parse -- treating as empty"; count_old=0; }

# Back both files up before anything writes, and put them back on ANY failure
# from here on. Without this, `set -e` firing after hyprresume has written
# leaves a zeroed toml behind and never prints a word about it.
backup="$(mktemp -d "$SESSION_DIR/.$NAME.backup.XXXXXX")"
[[ -f "$TOML" ]] && cp -p "$TOML" "$backup/toml"
[[ -f "$SIDECAR" ]] && cp -p "$SIDECAR" "$backup/sidecar"

published=0
rollback() {
    local rc=$?
    if (( ! published )); then
        [[ -f "$backup/toml" ]] && mv -f "$backup/toml" "$TOML"
        [[ -f "$backup/sidecar" ]] && mv -f "$backup/sidecar" "$SIDECAR"
        (( rc == 0 || rc == 3 || rc == 4 )) || err "save failed (rc=$rc) -- previous session restored"
    fi
    rm -rf -- "$backup"
}
trap rollback EXIT

hyprresume save "$NAME" >/dev/null

count_new="$(toml_windows "$TOML")"

# The two rules. They cover different accidents and neither implies the other.
#
#   floor         never publish an empty session over a populated one. This is
#                 the boot case: the desktop is not up yet, the save is faithful
#                 to an empty screen, and publishing it erases yesterday's work.
#   under-capture the save came out smaller than the screen it was taken from AND
#                 smaller than what we already had -- a partial or failed write,
#                 not a user who closed windows. Someone who really did close
#                 windows produces count_new == count_screen, which publishes.
if (( count_new < 0 )); then
    err "hyprresume produced a $NAME.toml that does not parse -- previous session kept"
    exit 3
fi
if (( count_new == 0 && count_old > 0 )); then
    err "refusing to overwrite $count_old saved window(s) with 0 (screen: $count_screen)"
    exit 3
fi
if (( count_new < count_old && count_new < count_screen )); then
    err "refusing a partial save: $count_new window(s) written, $count_screen on screen, $count_old saved"
    exit 3
fi

# The sidecar is written from the SAME capture the guard was decided on, through
# a temp file in the same directory: `> "$SIDECAR"` truncates when the shell
# opens the redirect, so a jq that errors -- or a machine that dies mid-write,
# which is the amdgpu case the short snapshot interval exists for -- leaves a
# truncated file behind. The replay parses the sidecar without a try, so a
# truncated one takes the whole restore down with a traceback.
tmp_sidecar="$(mktemp "$SESSION_DIR/.$NAME.titles.XXXXXX")"
# O nome do monitor, não o índice. `client.monitor` é uma posição na lista de
# monitores daquele instante: desligar uma tela renumera todas as outras, e uma
# sessão gravada com índices passa a descrever um arranjo que não existe mais.
# O nome ("DP-1") é o que o compositor aceita de volta e o que uma pessoa lê.
monitors="$(hyprctl monitors -j 2>/dev/null || echo '[]')"

jq --arg when "$(date -u +%FT%TZ)" --argjson mons "$monitors" '{
    when: $when,
    monitors: [ $mons[]? | {id, name, description} ],
    windows: [ .[]
        | select(.mapped) | select(.workspace.id > 0)
        | . as $w
        | {class, title, pid,
           workspace: .workspace.id,
           monitor: .monitor,
           monitorName: (($mons[]? | select(.id == $w.monitor) | .name) // null),
           at, size, floating} ]
}' <<<"$clients" > "$tmp_sidecar"
mv -f "$tmp_sidecar" "$SIDECAR"

# Only now is the pair consistent: both files describe the same capture, or
# neither moved. A toml with six windows next to a sidecar with one is what
# makes the replay report a restore it did not do.
published=1

extra=""
(( count_scratch > 0 )) && extra=", $count_scratch scratchpad window(s) not saved"
say "$NAME -- $count_new window(s) + titles$extra"
