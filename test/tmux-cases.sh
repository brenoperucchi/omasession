#!/usr/bin/env bash
# O caso que motivou docs/plans/005: uma janela cujo conteúdo vive num cliente
# tmux, não num shell. Sem isto, o resolvedor devolve o terminal genérico
# (`foot`), e um reboot real mediu o resultado: dois servidores tmux nunca
# sobem, as duas janelas voltam como bash puro em $HOME. Com o fix, a janela
# aponta pra sessão certa (`foot -- tmux new -A -s <nome>`) e o conteúdo é
# problema do tmux-resurrect/continuum do próprio usuário, não nosso.
#
# Runs inside the guest: precisa de um Hyprland vivo, `tmux` instalado, e
# fecha/abre janelas de verdade.
#
#   ./tmux-cases.sh          roda os dois casos
#   ./tmux-cases.sh --keep   deixa as janelas de cenário abertas no fim

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OMASESSION="${OMASESSION_BIN:-$SELF_DIR/bin/omasession}"
[[ -x "$OMASESSION" ]] || { echo "tmux-cases: não encontrei $OMASESSION" >&2; exit 1; }
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

if [[ "${OMASESSION_TEST_I_MEAN_IT:-}" != "1" ]]; then
    cat >&2 <<'WARN'
tmux-cases: este teste fecha janelas, cria/mata sessões tmux e sobrescreve a
sessão salva (nome "tmux-cases-test"). É para o guest do lab.

  OMASESSION_TEST_I_MEAN_IT=1 ./tmux-cases.sh
WARN
    exit 2
fi

command -v tmux >/dev/null || { echo "tmux-cases: tmux não está instalado neste guest" >&2; exit 2; }
command -v foot >/dev/null || { echo "tmux-cases: foot não está instalado neste guest" >&2; exit 2; }

# Armadilha medida rodando este teste: se @continuum-restore estiver 'on', a
# PRIMEIRA sessão de um servidor tmux novo dispara o restore automático do
# tmux-resurrect -- inclusive de um snapshot velho, de outro teste ou sessão
# manual, sem relação nenhuma com os nomes que este script cria. Medido: isso
# não soma sessões extras de forma inofensiva, faz as sessões deste script
# desaparecerem do resultado final (docs/plans/005). Recusar em vez de tentar
# limpar sozinho: apagar `resurrect/last` sem perguntar destruiria a última
# sessão de verdade de quem estiver com o tmux configurado como o usuário
# deste projeto -- o mesmo motivo pelo qual este script não dá `tmux
# kill-server` por conta própria.
if tmux list-sessions >/dev/null 2>&1; then
    echo "tmux-cases: já existe um servidor tmux rodando -- mate-o (tmux kill-server) antes de rodar este teste" >&2
    exit 2
fi
if [[ -e ~/.local/share/tmux/resurrect/last || -e ~/.tmux/resurrect/last ]]; then
    echo "tmux-cases: existe um snapshot do tmux-resurrect -- o restore automático do continuum vai disparar no primeiro 'tmux new' deste teste e contaminar o cenário. Mova ~/.local/share/tmux/resurrect (ou ~/.tmux/resurrect) para outro lugar antes de rodar, e devolva depois" >&2
    exit 2
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export HYPRLAND_INSTANCE_SIGNATURE="${HYPRLAND_INSTANCE_SIGNATURE:-$(ls -t "$XDG_RUNTIME_DIR/hypr" | head -1)}"

pass=0; fail=0
check() { # check <name> <expected> <actual>
    if [[ "$2" == "$3" ]]; then printf '  ok   %s\n' "$1"; pass=$((pass+1))
    else printf '  FAIL %s: esperado [%s], obtido [%s]\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}

close_all() {
    for a in $(hyprctl clients -j | jq -r '.[]|select(.mapped)|.address'); do
        hyprctl repl "hl.dispatch(hl.dsp.window.close{window=\"address:$a\"})" >/dev/null
    done
    sleep 3
}

DIR_A="$(mktemp -d)"; DIR_B="$(mktemp -d)"
SESS_A="omasession-test-a-$$"
# Com espaço, de propósito: tmux aceita espaço em nome de sessão (a própria
# mesh do usuário tem um, "Contratos Thera"), e a revisão desta rodada achou
# que o parsing de tmux_session() usava espaço como separador -- corrompendo
# nome e cwd em silêncio para exatamente este caso. Sem este caso o teste não
# distingue o parsing certo do errado.
SESS_B="omasession-test-b $$"

cleanup() {
    tmux kill-session -t "$SESS_A" 2>/dev/null || true
    tmux kill-session -t "$SESS_B" 2>/dev/null || true
    rm -rf "$DIR_A" "$DIR_B"
    ((KEEP)) || close_all
}
trap cleanup EXIT

echo "== resolve: janela com tmux embaixo aponta pra sessão certa, não pro terminal genérico"
close_all
tmux new -d -s "$SESS_A" -c "$DIR_A"
tmux new -d -s "$SESS_B" -c "$DIR_B"
sleep 1
hyprctl repl "hl.dispatch(hl.dsp.exec_cmd(\"foot -- tmux attach -t '$SESS_A'\"))" >/dev/null
sleep 3
hyprctl repl "hl.dispatch(hl.dsp.exec_cmd(\"foot -- tmux attach -t '$SESS_B'\"))" >/dev/null
sleep 3

resolved="$("$OMASESSION" resolve --json)"
cmd_a="$(jq -r --arg d "$SESS_A" '.[] | select(.tmux_session == $d) | .command' <<<"$resolved")"
cmd_b="$(jq -r --arg d "$SESS_B" '.[] | select(.tmux_session == $d) | .command' <<<"$resolved")"
check "sessão A resolve pra 'tmux new -A -s'"  "foot -- tmux new -A -s $SESS_A" "$cmd_a"
# command() passa o argv por shlex.quote -- um nome com espaço sai entre
# aspas simples (`'omasession test b 12345'`), não solto. O valor esperado
# tem de refletir isso, não só concatenar o nome cru.
check "sessão B (nome com espaço) resolve pra 'tmux new -A -s', sem truncar"  "foot -- tmux new -A -s '$SESS_B'" "$cmd_b"

cwd_a="$(jq -r --arg d "$SESS_A" '.[] | select(.tmux_session == $d) | .cwd' <<<"$resolved")"
check "cwd da sessão A vem do tmux, não 'not recoverable'" "$DIR_A" "$cwd_a"

echo "== ponta a ponta: fecha as duas janelas, restaura, e cada uma reanexa na sessão certa"
"$OMASESSION" save tmux-cases-test >/dev/null
close_all

"$OMASESSION" restore tmux-cases-test >/dev/null
sleep 3

attached_a="$(tmux list-clients -F '#{session_name}' 2>/dev/null | grep -c "^${SESS_A}\$" || true)"
attached_b="$(tmux list-clients -F '#{session_name}' 2>/dev/null | grep -c "^${SESS_B}\$" || true)"
check "sessão A tem exatamente 1 cliente reanexado" "1" "$attached_a"
check "sessão B tem exatamente 1 cliente reanexado" "1" "$attached_b"

sessions_named_like_ours="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -c "^omasession-test-" || true)"
check "nenhuma sessão órfã extra foi criada (só as 2 nossas)" "2" "$sessions_named_like_ours"

printf '\n%d ok, %d falha(s)\n' "$pass" "$fail"
exit $(( fail > 0 ))
