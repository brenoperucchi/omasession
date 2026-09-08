#!/usr/bin/env bash
# Session probe. Runs inside the guest.
#
#   probe.sh scenario   open a fixed set of windows covering the hard cases
#   probe.sh snapshot   write a JSON snapshot of every visual window to stdout
#   probe.sh compare A B  report what survived between two snapshots
#
# The snapshot records, per window, what a session-restore tool would have to
# reproduce: workspace, geometry, floating state -- and the two things that
# actually decide whether it can: the launch command and the working directory.

set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    HYPRLAND_INSTANCE_SIGNATURE="$(ls -t "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)"
    export HYPRLAND_INSTANCE_SIGNATURE
fi
[[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]] || { echo "no Hyprland instance" >&2; exit 1; }

die() { printf 'probe: %s\n' "$*" >&2; exit 1; }

# --- scenario -------------------------------------------------------------
# Each entry is deliberately a different class of restore difficulty.
scenario() {
    mkdir -p ~/Devs/project-a ~/Devs/project-b

    # Hyprland 0.56 with a Lua config parses `hyprctl dispatch` as Lua: the
    # legacy `dispatch exec foo` / `keyword windowrule ...` forms are gone
    # ("keyword can't work with non-legacy parsers. Use eval."). Everything
    # goes through hl.dispatch(hl.dsp.*) now.
    launch() { hyprctl repl "hl.dispatch(hl.dsp.exec_cmd(\"uwsm app -- $1\"))" >/dev/null 2>&1; }
    goto_ws() { hyprctl repl "hl.dispatch(hl.dsp.focus{workspace=$1})" >/dev/null 2>&1; }
    win() { hyprctl repl "hl.dispatch(hl.dsp.window.$1)" >/dev/null 2>&1 || true; }

    goto_ws 1
    launch "foot --working-directory=$HOME/Devs/project-a"
    sleep 3
    launch "foot --working-directory=$HOME/Devs/project-b -- btop"
    sleep 4

    goto_ws 2
    launch "chromium"
    sleep 8

    goto_ws 3
    launch "nautilus"
    sleep 5
    launch "foot --title=floating-probe"
    sleep 3
    win 'float{}'
    win 'resize{x=800, y=500, exact=true}'
    win 'move{x=200, y=150, exact=true}'

    goto_ws 4
    launch "obsidian"
    sleep 10

    goto_ws 1
    echo "scenario: opened $(hyprctl clients -j | jq 'length') windows"
}

# --- snapshot -------------------------------------------------------------
# cmdline/cwd come from /proc, which is exactly the fragile path every existing
# tool depends on -- recording it is how we measure the gap, not a fix for it.
snapshot() {
    hyprctl clients -j | jq -c '[ .[]
        | select(.mapped == true)
        | select(.workspace.id > 0)
        | {class, initialClass, title, pid,
           workspace: .workspace.id,
           at, size, floating, pinned, fullscreen} ]' \
    | jq -c --argjson procs "$(proc_table)" '
        [ .[] | . + ($procs[(.pid|tostring)] // {cmdline:null, cwd:null, exe:null}) ]
        | sort_by(.workspace, .class, .title)' \
    | jq -c --arg host "$(hostname)" --arg when "$(date -u +%FT%TZ)" \
        '{when: $when, host: $host, count: length, windows: .}'
}

proc_table() {
    local pids
    pids="$(hyprctl clients -j | jq -r '.[] | select(.mapped==true) | .pid' | sort -u)"
    {
        echo "{"
        local first=1
        for pid in $pids; do
            [[ -r "/proc/$pid/cmdline" ]] || continue
            local cmd cwd exe
            cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | sed 's/ *$//')"
            cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || echo '')"
            exe="$(readlink "/proc/$pid/exe" 2>/dev/null || echo '')"
            ((first)) || echo ","
            first=0
            jq -nc --arg p "$pid" --arg c "$cmd" --arg w "$cwd" --arg e "$exe" \
                '{key:$p, value:{cmdline:$c, cwd:$w, exe:$e}}' \
                | jq -r '"\(.key|tostring|@json): \(.value|tojson)"'
        done
        echo "}"
    } | tr -d '\n' | sed 's/}{/},{/g'
}

# --- compare --------------------------------------------------------------
# Identity is (class, workspace); PIDs and addresses never survive a reboot.
compare() {
    local before="$1" after="$2"
    [[ -r "$before" ]] || die "missing snapshot: $before"
    [[ -r "$after" ]] || die "missing snapshot: $after"

    jq -r --slurpfile b "$before" --slurpfile a "$after" -n '
        ($b[0].windows) as $B | ($a[0].windows) as $A
        | ($B | map({key: (.class + "|" + (.workspace|tostring)), value: .}) | from_entries) as $bi
        | ($A | map({key: (.class + "|" + (.workspace|tostring)), value: .}) | from_entries) as $ai
        | ($B | map(.class) | unique) as $bc
        | ($A | map(.class) | unique) as $ac
        | "before: \($B|length) windows   after: \($A|length) windows",
          "",
          "class                 ws  back?  ws-ok  geom-ok  cwd-ok",
          "-------------------------------------------------------",
          ( $B[] as $w
            | ($ai[$w.class + "|" + ($w.workspace|tostring)]) as $m
            | ($A | map(select(.class == $w.class)) | first) as $any
            | [ ($w.class | .[0:20] | . + (" " * (20 - length))),
                ($w.workspace | tostring | . + (" " * (3 - length))),
                (if $any then " yes " else " NO  " end),
                (if $m then "  yes " else "  no  " end),
                (if $m and ($m.size == $w.size) then "   yes  " else "   no   " end),
                (if $m and ($m.cwd == $w.cwd) and ($w.cwd != null) then "  yes"
                 elif $w.cwd == null then "   - "
                 else "  no " end) ]
            | join(" ") ),
          "",
          "lost classes:  \(($bc - $ac) | join(", ") // "none")",
          "new classes:   \(($ac - $bc) | join(", ") // "none")"
    '
}

case "${1:-}" in
    scenario) scenario ;;
    snapshot) snapshot ;;
    compare) shift; compare "${1:?need before.json}" "${2:?need after.json}" ;;
    *) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
