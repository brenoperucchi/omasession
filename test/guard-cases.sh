#!/usr/bin/env bash
# Cases the session-save guard has to get right. Runs inside the guest: needs a
# live Hyprland and hyprresume, and it drives real windows.
#
# The guard exists because the first version of it did not protect the file that
# matters. Measured on 2026-09-08 against the version at commit aef8f0b, with an
# empty screen and a 3-window session on disk:
#
#     session-save: refusing to overwrite 3 saved windows with 0
#     exit code: 0
#     resultado: toml=0 sidecar=3
#
# It printed the refusal, reported success, and destroyed the toml anyway --
# leaving a sidecar claiming three windows beside a session holding none. That
# pair makes the replay print "title sidecar: 3 window(s)" and then "0/0 placed",
# and exit 0. Reporting a restore that did not happen is the failure this whole
# project exists to avoid, so it gets a test.
#
#   ./guard-cases.sh          run every case
#   ./guard-cases.sh --keep   leave the scenario windows open afterwards

set -uo pipefail

# Por padrão testa O CÓDIGO DESTE REPOSITÓRIO. A versão anterior apontava para
# uma cópia solta no $HOME, então "16/16 ok" podia estar aprovando um arquivo
# diferente do que seria commitado -- um teste verde sobre código que ninguém
# leu é pior que nenhum teste.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAVE="${OMASESSION_SAVE:-$SELF_DIR/lib/session-save.sh}"
[[ -x "$SAVE" ]] || { echo "guard-cases: não encontrei $SAVE" >&2; exit 1; }
S="$HOME/.local/share/hyprresume/sessions"
STUB="$(mktemp -d)"
BACKUP="$(mktemp -d)"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

# Freio. Este teste FECHA todas as janelas da sessão e sobrescreve a sessão
# salva; rodá-lo na máquina de trabalho por engano custa o desktop inteiro.
if [[ "${OMASESSION_TEST_I_MEAN_IT:-}" != "1" ]]; then
    cat >&2 <<'WARN'
guard-cases: este teste fecha TODAS as janelas abertas e sobrescreve a sessão
salva. Ele é para o guest do lab, não para uma máquina em uso.

  OMASESSION_TEST_I_MEAN_IT=1 ./guard-cases.sh
WARN
    exit 2
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export HYPRLAND_INSTANCE_SIGNATURE="${HYPRLAND_INSTANCE_SIGNATURE:-$(ls -t "$XDG_RUNTIME_DIR/hypr" | head -1)}"

pass=0; fail=0
check() { # check <name> <expected> <actual>
    if [[ "$2" == "$3" ]]; then printf '  ok   %s\n' "$1"; pass=$((pass+1))
    else printf '  FAIL %s: esperado [%s], obtido [%s]\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
windows_on_screen() { hyprctl clients -j | jq '[.[]|select(.mapped)|select(.workspace.id>0)]|length'; }
toml_count()    { grep -c '^\[\[window\]\]' "$S/last.toml" 2>/dev/null || echo 0; }
sidecar_count() { jq '.windows|length' "$S/last.titles.json" 2>/dev/null || echo 0; }
fingerprint()   { md5sum "$S/last.toml" "$S/last.titles.json" 2>/dev/null | cut -d' ' -f1 | tr '\n' ' '; }
close_all()     { for a in $(hyprctl clients -j | jq -r '.[]|select(.mapped)|.address'); do
                      hyprctl repl "hl.dispatch(hl.dsp.window.close{window=\"address:$a\"})" >/dev/null
                  done; sleep 4; }

cat > "$STUB/hyprresume" <<'STUBEOF'
#!/usr/bin/env bash
# Produz um toml controlado para exercitar as decisões do guard sem esperar uma
# falha real do hyprresume.
#
# Honra o NOME pedido, como o hyprresume de verdade: `save <nome>` escreve
# `<nome>.toml` e não toca em nenhum outro -- verificado no 0.5.0. Um stub que
# escrevesse direto em `last.toml` estaria testando um mecanismo que não existe
# mais, e passaria a reprovar o código por fazer a coisa certa.
S=$HOME/.local/share/hyprresume/sessions
NAME="${2:-last}"
case "${STUB_MODE:-partial}" in
  partial) printf '[session]\nname = "%s"\n\n[[window]]\napp_id = "foot"\nlaunch_cmd = "foot"\nworkspace = "1"\n' "$NAME" > "$S/$NAME.toml" ;;
  garbage) printf '[session\nnot toml ][\n' > "$S/$NAME.toml" ;;
  empty)   printf '[session]\nname = "%s"\n' "$NAME" > "$S/$NAME.toml" ;;
  crash)   exit 7 ;;
esac
STUBEOF
chmod +x "$STUB/hyprresume"

cp -a "$S/." "$BACKUP/" 2>/dev/null || true
restore_session() { rm -rf "${S:?}"/*; cp -a "$BACKUP/." "$S/" 2>/dev/null || true; }
cleanup() { restore_session; rm -rf "$STUB" "$BACKUP"; }
trap cleanup EXIT

echo "== caminho feliz: uma tela populada publica, e o par fica consistente"
close_all
"$HOME/probe.sh" scenario >/dev/null 2>&1
sleep 3
screen="$(windows_on_screen)"
"$SAVE" >/dev/null; rc=$?
check "publica com rc=0"              "0"       "$rc"
check "toml recebe as $screen janelas" "$screen" "$(toml_count)"
check "par consistente"                "$(toml_count)" "$(sidecar_count)"

echo "== recusas: cada uma preserva OS DOIS arquivos byte a byte"
for mode in partial garbage empty crash; do
    before="$(fingerprint)"
    STUB_MODE="$mode" PATH="$STUB:$PATH" "$SAVE" >/dev/null 2>&1; rc=$?
    check "$mode: nao publica"        "nao-zero" "$( ((rc)) && echo nao-zero || echo zero)"
    check "$mode: arquivos intactos"  "$before"  "$(fingerprint)"
done

echo "== o furo da regra antiga: uma sessao salva PEQUENA nao autoriza publicar pior"
# old=1, screen=N, new=1 passava nas duas condicoes antigas e publicava uma
# sessao de uma janela sobre uma tela cheia -- com o sidecar trazendo todas.
close_all
"$HOME/probe.sh" scenario >/dev/null 2>&1
sleep 3
printf '[session]\nname = "last"\n\n[[window]]\napp_id = "foot"\nlaunch_cmd = "foot"\nworkspace = "1"\n' > "$S/last.toml"
rm -f "$S/last.titles.json"
before="$(fingerprint)"
STUB_MODE=partial PATH="$STUB:$PATH" "$SAVE" >/dev/null 2>&1; rc=$?
check "com 1 salva e a tela cheia, recusa" "3" "$rc"
check "e nao mexe nos arquivos"            "$before" "$(fingerprint)"

echo "== encolhimento legitimo: quem fechou janelas de verdade tem de conseguir salvar"
saved_before="$(toml_count)"
for a in $(hyprctl clients -j | jq -r '.[]|select(.mapped)|select(.class!="foot")|.address'); do
    hyprctl repl "hl.dispatch(hl.dsp.window.close{window=\"address:$a\"})" >/dev/null
done
sleep 4
screen="$(windows_on_screen)"
"$SAVE" >/dev/null; rc=$?
check "publica mesmo com menos que antes ($saved_before -> $screen)" "0" "$rc"
check "toml acompanha a tela"                                        "$screen" "$(toml_count)"

echo "== tela vazia: o caso que destruia a sessao"
close_all
before="$(fingerprint)"
"$SAVE" >/dev/null 2>&1; rc=$?
check "recusa com rc=3"          "3"       "$rc"
check "arquivos intactos"        "$before" "$(fingerprint)"

echo "== publicacao: o par nunca sai rasgado, nem morrendo no meio"
close_all
"$HOME/probe.sh" scenario >/dev/null 2>&1
sleep 3
"$SAVE" >/dev/null
gen_toml() { sed -n 's/^generation = "\(.*\)"/\1/p' "$S/last.toml" | tail -1; }
gen_side() { jq -r '.generation // ""' "$S/last.titles.json" 2>/dev/null; }
check "as duas metades carregam a mesma geracao" "$(gen_toml)" "$(gen_side)"
check "a geracao anterior fica recuperavel" "sim" \
      "$([ -f "$S/last.prev.toml" ] && echo sim || echo nao)"

torn=0
for i in $(seq 1 8); do
    ( "$SAVE" >/dev/null 2>&1 ) &
    bg=$!
    python3 -c "import time,random; time.sleep(random.uniform(0.05,0.9))"
    if (( i % 2 )); then kill -KILL "$bg" 2>/dev/null; else kill -TERM "$bg" 2>/dev/null; fi
    wait "$bg" 2>/dev/null
    [[ "$(gen_toml)" == "$(gen_side)" ]] || torn=$((torn+1))
done
check "8 mortes no meio do save, nenhum par rasgado" "0" "$torn"
check "nenhum staging orfao" "0" "$(ls "$S"/*staging* 2>/dev/null | wc -l)"

echo "== lock: dois saves nao se sobrepoem"
flock -x "$S/.last.lock" -c "sleep 5" &
sleep 1
"$SAVE" >/dev/null 2>&1; rc=$?
check "segundo save desiste com rc=4" "4" "$rc"
wait

((KEEP)) || close_all
printf '\n%d ok, %d falha(s)\n' "$pass" "$fail"
exit $(( fail > 0 ))
