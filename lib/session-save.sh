#!/usr/bin/env bash
# Save a session: our own capture (lib/capture.py), plus the one field it
# cannot see from a static hyprctl dump, behind a guard that refuses to
# replace a good session with a worse one.
#
# capture.py writes app_id, workspace, geometry and cwd -- everything except
# what distinguishes one browser window from another. Without a title there is
# no way to send a restored Chromium window back to the workspace it came from,
# because all of them share a PID, a command line and a class.
#
# Why capture.py and not hyprresume save, which did the same job until
# 2026-09-09: hyprresume resolves a custom-classed window (no .desktop entry --
# `--class`/`--app-id` set by the launcher itself) by its binary's own
# /proc/<pid>/exe, discarding every command-line argument. Measured on a real
# case, not a synthetic one: the Herdr TUI runs as
# `foot --app-id=TUI.tile herdr`, and `hyprresume resolve TUI.tile` returns
# bare `foot` -- the window comes back as an empty terminal, herdr never
# started. lib/resolve.py resolves the same window from /proc/<pid>/cmdline
# and returns the full command; capture.py is what puts that resolver on the
# path replay.py actually reads from. See docs/plans/003-command-resolver.md.
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
# is only checkable AFTER the new content exists: stage the capture, check what
# came out, and only publish it if it is not worse than what is already there.
#
# Exit codes:  0 saved   3 refused (session preserved)   4 another save running

set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
    HYPRLAND_INSTANCE_SIGNATURE="$(ls -t "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)"
    export HYPRLAND_INSTANCE_SIGNATURE
fi

# Nosso diretório, não mais o do hyprresume -- ver bin/omasession para o
# porquê da migração. Antes esta variável não tinha efeito real: o writer era
# hyprresume, que não aceita outro diretório, e uma variável que parecesse
# redirecioná-lo movia o guard sem mover quem escreve de fato. Agora o writer é
# nosso (capture.py, chamado abaixo), então honrá-la é honesto.
SESSION_DIR="${OMASESSION_SESSION_DIR:-$HOME/.local/share/omasession/sessions}"
NAME="${1:-last}"
TOML="$SESSION_DIR/$NAME.toml"
SIDECAR="$SESSION_DIR/$NAME.titles.json"

say() { printf 'session-save: %s\n' "$*"; }
die() { printf 'session-save: %s\n' "$*" >&2; exit 1; }
err() { printf 'session-save: %s\n' "$*" >&2; }

mkdir -p "$SESSION_DIR"

# One save at a time. The snapshot timer fires every 30s by default and a slow
# hyprctl is enough to overlap two runs; two captures racing on the same file
# is a way to produce exactly the truncated toml this guard exists to catch.
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
# capture.py escreve para o stdout -- não toca em arquivo nenhum, quem decide
# o destino é este script. Então ele nunca escreve sobre a sessão publicada:
# escreve numa geração lateral, que só vira `last` depois de validada.
#
# Isso substitui o backup-e-rollback anterior, que tinha um furo que a revisão
# encontrou: entre a escrita do toml e o rename do sidecar existia uma janela em
# que o par ficava dessincronizado, e o `trap EXIT` não cobre SIGTERM nem queda
# de energia -- exatamente os dois casos para os quais este plugin existe.
# Nomes internos vivem num namespace que o usuário não pode escolher. Antes o
# staging era "$NAME-staging" no mesmo espaço público: depois de `save
# work-staging`, um `save work` apagava e sobrescrevia aquela sessão do usuário
# com o próprio staging. Os locks eram diferentes e o arquivo era o mesmo.
case "$NAME" in
    omasession.*|*.prev)
        die "'$NAME' is reserved for internal use"
        ;;
esac
STAGING="omasession.staging.$NAME.$$"
STAGING_TOML="$SESSION_DIR/$STAGING.toml"
STAGING_SIDECAR="$SESSION_DIR/$STAGING.titles.json"

cleanup() {
    rm -f "$STAGING_TOML" "$STAGING_TOML.raw" "$STAGING_SIDECAR" "$STAGING_SIDECAR.raw" \
          "$SESSION_DIR/.$NAME.titles."* "${capture_err:-}"
}
trap cleanup EXIT

# Varre stagings órfãos de execuções que morreram sem rodar o trap -- SIGKILL e
# queda de energia não rodam trap nenhum. São inertes (nada os lê), mas
# acumulariam para sempre, e um diretório cheio de restos esconde o estado real.
#
# `kill -0 $PID` sozinho não basta: o PID codificado no nome do arquivo pode ter
# sido reciclado por QUALQUER outro processo do sistema entre a morte original e
# esta varredura -- medido, num teste que mata 8 saves em sequência, sobrando
# arquivos que `kill -0` via como "vivos" porque o número de PID já pertencia a
# outra coisa. `/proc/<pid>/comm` não resolve isso: é sempre "bash" para
# qualquer script bash invocado via `#!/usr/bin/env bash` (confirmado
# empiricamente -- o kernel/bash setam comm a partir do primeiro argv do exec,
# que é literalmente "bash", não o nome do arquivo). `/proc/<pid>/cmdline`
# carrega o caminho completo do script como segundo argumento e é o que de fato
# distingue esta execução de qualquer outra coisa que tenha herdado o PID.
# O padrão cobre tanto o par publicável (.toml/.titles.json) quanto os
# arquivos que só existem enquanto a captura está em andamento
# (.toml.raw/.titles.json.raw, escritos antes do rename para o nome final).
# Achado da revisão: um SIGKILL bem no meio da captura (linhas abaixo) deixava
# justamente esse tipo pra trás, e o padrão antigo -- só toml/titles.json --
# nunca casava com ele, então acumulava para sempre exatamente como a
# varredura existe para impedir.
for stale in "$SESSION_DIR"/omasession.staging.*; do
    [[ -e "$stale" ]] || continue
    [[ "$stale" =~ \.([0-9]+)\.(toml|toml\.raw|titles\.json|titles\.json\.raw)$ ]] || continue
    pid="${BASH_REMATCH[1]}"
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")"
    [[ "$cmdline" == *session-save.sh* ]] || rm -f "$stale"
done

# O stderr temporário do capture.py (.$NAME.capture-err.XXXXXX) não carrega
# pid nenhum no nome -- mktemp usa um sufixo aleatório, não o processo -- então
# a mesma checagem de /proc não se aplica. Mas por construção nenhuma execução
# em andamento pode estar usando um que já existia ANTES desta varredura: a
# variável só é populada mais abaixo, com um mktemp novo. Um que sobrevive até
# aqui é órfão de um SIGKILL de outra execução (própria ou de outro NAME) que
# não rodou o trap; a idade mínima é só para não correr atrás de um que outra
# execução concorrente, de outro NAME, tenha criado no mesmíssimo instante.
for stale in "$SESSION_DIR"/.*.capture-err.*; do
    [[ -e "$stale" ]] || continue
    [[ -n "$(find "$stale" -mmin +1 2>/dev/null)" ]] || continue
    rm -f "$stale"
done

count_old="$(toml_windows "$TOML")"
(( count_old >= 0 )) || { err "existing $NAME.toml does not parse -- treating as empty"; count_old=0; }

SELF_DIR="$(dirname "${BASH_SOURCE[0]}")"
# Ponto de troca só para o guard-cases.sh: substituir a captura de verdade por
# uma controlada é como se exercitam as decisões do guard sem depender de uma
# falha real. Sem isto o teste teria de simular uma tela quebrada de verdade
# para cada caso -- o que o antigo stub fazia interceptando `hyprresume` pelo
# PATH, um mecanismo que deixou de existir quando a captura passou a ser
# chamada por caminho, não por busca no PATH.
CAPTURE="${OMASESSION_CAPTURE:-$SELF_DIR/capture.py}"
rm -f "$STAGING_TOML"
capture_err="$(mktemp "$SESSION_DIR/.$NAME.capture-err.XXXXXX")"
# $clients por stdin, não uma segunda hyprctl dentro do capture.py: achado da
# revisão desta rodada. "O sidecar sai da MESMA leitura que decidiu o guard"
# (comentário mais abaixo) era garantido de graça enquanto o toml vinha de um
# binário externo que este script nunca invocava com os dados já em mão. Ler
# de novo por conta própria -- o que capture.py fazia antes desta correção --
# reabre exatamente a corrida que o comentário descreve: uma janela fechando
# ou abrindo entre as duas leituras faz count_new (desta leitura) e
# count_screen (da leitura de cima) discordarem por um motivo que não é nem
# save incompleto nem sessão vazia.
if ! python3 "$CAPTURE" "$NAME" <<<"$clients" > "$STAGING_TOML.raw" 2>"$capture_err"; then
    err "capture failed: $(tail -3 "$capture_err")"
    rm -f "$capture_err" "$STAGING_TOML.raw"
    exit 3
fi
rm -f "$capture_err"
mv -f "$STAGING_TOML.raw" "$STAGING_TOML"

# Não a contagem de [[window]] crua: uma janela cujo resolvedor devolveu
# argv=None ainda vira um registro (app_id/workspace/geometria, sem
# launch_cmd) -- válido para o parser, inútil para o replay, que pula esse
# registro no restore (replay.py: "no launch_cmd, skipped"). Achado da
# revisão: contando o registro mesmo assim, o piso e a cobertura viam uma
# tela cheia como coberta quando na verdade uma janela não tinha comando
# nenhum pra voltar -- o guard existe exatamente pra recusar isto, e a
# contagem crua o deixava cego pra esse caso específico.
toml_resolved_windows() {
    [[ -f "$1" ]] || { echo 0; return; }
    python3 - "$1" <<'PY' 2>/dev/null || echo -1
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(sum(1 for w in tomllib.load(fh).get("window", []) if w.get("launch_cmd")))
PY
}

count_new="$(toml_resolved_windows "$STAGING_TOML")"

# As duas regras. Cobrem acidentes diferentes e nenhuma implica a outra.
#
#   piso        nunca publicar uma sessão vazia sobre uma populada. É o caso do
#               boot: a captura é fiel a uma tela que ainda não subiu, e
#               publicá-la apaga o trabalho de ontem.
#   cobertura   o que foi escrito tem de dar conta da tela de onde foi tirado.
#
# A regra de cobertura substitui uma anterior que comparava também com a
# contagem já salva ("menor que a tela E menor que o que havia"). A revisão
# achou o furo: com `old=1, screen=6, new=1` as duas condições falhavam e a
# sessão de uma janela era publicada. Pior, o sidecar vem da mesma leitura e
# teria as 6 -- um par carimbado com a mesma geração, coerente por selo e
# incoerente por conteúdo. O selo daria ao defeito uma aparência de correção.
#
# Comparar só com a tela também resolve isso por construção: ou as duas metades
# cobrem a mesma captura e publicam juntas, ou nada publica. E quem fechou
# janelas de verdade continua salvando, porque aí count_new == count_screen.
if (( count_new < 0 )); then
    err "capture produced a session that does not parse -- previous session kept"
    exit 3
fi
if (( count_new == 0 && count_old > 0 )); then
    err "refusing to overwrite $count_old saved window(s) with 0 (screen: $count_screen)"
    exit 3
fi
if (( count_new < count_screen )); then
    err "refusing an incomplete save: $count_new window(s) written, $count_screen on screen"
    # Escotilha, porque uma recusa permanente também é uma falha: se nesta
    # máquina o `hyprctl clients` nunca dá conta de alguma janela, sem isto o
    # usuário fica sem sessão nenhuma, o que é pior que uma sessão parcial
    # declarada.
    if [[ "${OMASESSION_ALLOW_PARTIAL:-}" != "1" ]]; then
        err "  set OMASESSION_ALLOW_PARTIAL=1 to publish partial sessions on this machine"
        exit 3
    fi
    err "  publishing anyway (OMASESSION_ALLOW_PARTIAL=1)"
    PARTIAL=1
fi

# Um selo igual nos dois arquivos. Dois renames não são uma transação, então em
# vez de fingir que são, o par carrega de que geração cada metade veio e o
# replay pode detectar um par rasgado em vez de restaurar meia sessão achando
# que está inteira.
PARTIAL="${PARTIAL:-0}"
GENERATION="$(date -u +%s%N)"
printf '\n[omasession]\ngeneration = "%s"\n' "$GENERATION" >> "$STAGING_TOML"

# O sidecar sai da MESMA leitura que decidiu o guard: contar de uma leitura e
# gravar de outra deixa janelas aparecerem ou sumirem entre as duas, e decide o
# guard sobre evidência diferente da que ele protege.
monitors="$(hyprctl monitors -j 2>/dev/null || echo '[]')"
# `partial` viaja com o par. Sem isto a escotilha publicava um toml de uma
# janela ao lado de um sidecar de seis, os dois com o mesmo selo -- o "coerente
# por selo e incoerente por conteúdo" que esta rodada acabou de identificar como
# o pior caso, reintroduzido pela própria escotilha. Quem lê precisa saber que a
# cobertura das duas metades não é a mesma.
jq --arg when "$(date -u +%FT%TZ)" --arg gen "$GENERATION" \
   --argjson partial "$PARTIAL" --argjson written "$count_new" \
   --argjson mons "$monitors" '{
    when: $when,
    generation: $gen,
    partial: ($partial == 1),
    windowsWritten: $written,
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

# Durabilidade não é atomicidade, e o rename só garante a segunda. capture.py
# escreve para o stdout, não para disco -- quem decide se o conteúdo chega ao
# disco de fato é este script, e sem isto um corte de energia logo após o save
# publica um arquivo cujo conteúdo ainda está só no page cache -- e queda de
# energia é metade do motivo deste plugin.
#
# E a falha do fsync IMPEDE publicar. A versão anterior a engolia com `|| true`
# e publicava assim mesmo, anunciando uma durabilidade que não tinha conseguido:
# ENOSPC ou EIO reportado no flush virava sucesso silencioso.
sync_path() {
    python3 -c '
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)' "$1"
}
if ! sync_path "$STAGING_TOML" || ! sync_path "$STAGING_SIDECAR"; then
    err "could not flush the new session to disk -- not publishing it"
    exit 3
fi

# A geração anterior só é substituída por um par que se prova inteiro. A versão
# anterior copiava incondicionalmente: se uma publicação morresse entre os dois
# renames, a tentativa seguinte promovia esse par RASGADO a `.prev` e destruía
# o único fallback bom que existia.
pair_is_whole() {
    local toml="$1" side="${1%.toml}.titles.json"
    [[ -f "$toml" && -f "$side" ]] || return 1
    local gt gs
    gt="$(sed -n 's/^generation = "\(.*\)"/\1/p' "$toml" | tail -1)"
    gs="$(jq -r '.generation // ""' "$side" 2>/dev/null)" || return 1
    [[ "$gt" == "$gs" ]]
}

if pair_is_whole "$TOML"; then
    cp -p "$TOML" "$SESSION_DIR/$NAME.prev.toml"
    cp -p "$SIDECAR" "$SESSION_DIR/$NAME.prev.titles.json"
    sync_path "$SESSION_DIR/$NAME.prev.toml" || true
    sync_path "$SESSION_DIR/$NAME.prev.titles.json" || true
elif [[ -f "$TOML" ]]; then
    err "the session being replaced is not a whole pair -- keeping the older fallback"
fi

mv -f "$STAGING_TOML" "$TOML"
# Ponto de pausa determinístico: a janela entre os dois renames dura
# microssegundos, e um kill em instante aleatório praticamente nunca cai nela.
# Sem isto o caminho do par rasgado não é testável, e um caminho não testado é
# uma afirmação, não uma garantia.
[[ -n "${OMASESSION_TEST_PAUSE_BETWEEN_MV:-}" ]] && sleep "$OMASESSION_TEST_PAUSE_BETWEEN_MV"
mv -f "$STAGING_SIDECAR" "$SIDECAR"
sync_path "$SESSION_DIR" || err "published, but the directory flush failed -- durability uncertain"

extra=""
(( count_scratch > 0 )) && extra=", $count_scratch scratchpad window(s) not saved"
if (( PARTIAL )); then
    say "$NAME -- PARTIAL: $count_new of $count_screen window(s) written$extra"
else
    say "$NAME -- $count_new window(s) + titles$extra"
fi
