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

# ── encenar, validar, publicar ───────────────────────────────────────────────
# hyprresume aceita um NOME de sessão e escreve `<nome>.toml` no mesmo
# diretório, sem tocar em nenhum outro -- verificado. Então ele nunca mais
# escreve sobre a sessão publicada: escreve numa geração lateral, que só vira
# `last` depois de validada.
#
# Isso substitui o backup-e-rollback anterior, que tinha um furo que a revisão
# encontrou: entre a escrita do toml e o rename do sidecar existia uma janela em
# que o par ficava dessincronizado, e o `trap EXIT` não cobre SIGTERM nem queda
# de energia -- exatamente os dois casos para os quais este plugin existe.
STAGING="$NAME-staging"
STAGING_TOML="$SESSION_DIR/$STAGING.toml"
STAGING_SIDECAR="$SESSION_DIR/$STAGING.titles.json"

cleanup() { rm -f "$STAGING_TOML" "$STAGING_SIDECAR" "$SESSION_DIR/.$NAME.titles."*; }
trap cleanup EXIT

count_old="$(toml_windows "$TOML")"
(( count_old >= 0 )) || { err "existing $NAME.toml does not parse -- treating as empty"; count_old=0; }

rm -f "$STAGING_TOML"
hyprresume save "$STAGING" >/dev/null

count_new="$(toml_windows "$STAGING_TOML")"

# As duas regras. Cobrem acidentes diferentes e nenhuma implica a outra.
#
#   piso           nunca publicar uma sessão vazia sobre uma populada. É o caso
#                  do boot: a captura é fiel a uma tela que ainda não subiu, e
#                  publicá-la apaga o trabalho de ontem.
#   subcaptura     saiu menor que a tela de onde veio E menor que o que já
#                  havia -- escrita parcial, não usuário fechando janelas. Quem
#                  fechou de verdade produz count_new == count_screen.
if (( count_new < 0 )); then
    err "hyprresume produced a session that does not parse -- previous session kept"
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

# Um selo igual nos dois arquivos. Dois renames não são uma transação, então em
# vez de fingir que são, o par carrega de que geração cada metade veio e o
# replay pode detectar um par rasgado em vez de restaurar meia sessão achando
# que está inteira.
GENERATION="$(date -u +%s%N)"
printf '\n[omasession]\ngeneration = "%s"\n' "$GENERATION" >> "$STAGING_TOML"

# O sidecar sai da MESMA leitura que decidiu o guard: contar de uma leitura e
# gravar de outra deixa janelas aparecerem ou sumirem entre as duas, e decide o
# guard sobre evidência diferente da que ele protege.
monitors="$(hyprctl monitors -j 2>/dev/null || echo '[]')"
jq --arg when "$(date -u +%FT%TZ)" --arg gen "$GENERATION" --argjson mons "$monitors" '{
    when: $when,
    generation: $gen,
    monitors: [ $mons[]? | {id, name, description} ],
    windows: [ .[]
        | select(.mapped) | select(.workspace.id > 0)
        | . as $w
        | {class, title, pid,
           workspace: .workspace.id,
           monitor: .monitor,
           monitorName: (($mons[]? | select(.id == $w.monitor) | .name) // null),
           at, size, floating} ]
}' <<<"$clients" > "$STAGING_SIDECAR.raw"

# O que só um processo vivo sabe dizer: o diretório real de cada terminal, ou
# por que ele não é recuperável. Depois do reboot isso não existe em lugar
# nenhum.
python3 "$(dirname "${BASH_SOURCE[0]}")/annotate.py" "$STAGING_SIDECAR.raw" > "$STAGING_SIDECAR" \
    2>/dev/null || mv -f "$STAGING_SIDECAR.raw" "$STAGING_SIDECAR"
rm -f "$STAGING_SIDECAR.raw"

# Durabilidade não é atomicidade, e o rename só garante a segunda. hyprresume
# não faz fsync nenhum (nenhuma ocorrência no binário 0.5.0), então sem isto um
# corte de energia logo após o save publica um arquivo cujo conteúdo ainda está
# só no page cache -- e queda de energia é metade do motivo deste plugin.
sync_file() { python3 -c '
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)' "$1" 2>/dev/null || true; }
sync_file "$STAGING_TOML"
sync_file "$STAGING_SIDECAR"

# A geração anterior fica recuperável. Um par rasgado ou um restore que só
# trouxe metade deixa de ser irreversível.
[[ -f "$TOML" ]] && cp -p "$TOML" "$SESSION_DIR/$NAME.prev.toml"
[[ -f "$SIDECAR" ]] && cp -p "$SIDECAR" "$SESSION_DIR/$NAME.prev.titles.json"

mv -f "$STAGING_TOML" "$TOML"
mv -f "$STAGING_SIDECAR" "$SIDECAR"
sync_file "$SESSION_DIR"

extra=""
(( count_scratch > 0 )) && extra=", $count_scratch scratchpad window(s) not saved"
say "$NAME -- $count_new window(s) + titles$extra"
