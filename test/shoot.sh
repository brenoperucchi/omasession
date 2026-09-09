#!/usr/bin/env bash
# Renderiza um cenario do mockup no guest do lab e traz o PNG para ca.
#
#   ./shoot.sh healthy|refused|contested [crop]
#
# Saida em $OMASESSION_SHOT_DIR (default: um mktemp -d, cujo caminho e impresso).
# Iterar o Panel.qml na maquina de trabalho e o que docs/DESIGN.md 6 proibe:
# um erro em QML derruba a barra, o dock e o menu de uma vez.
# Reinicia o quickshell de proposito: o hot reload deixa a superficie antiga
# na tela (medido -- disable/enable tambem nao basta).
set -uo pipefail
SCEN="$1"; CROP="${2:-700:450:4404:0}"
L=$HOME/VMs/hyprsession-lab
SCRATCH="${OMASESSION_SHOT_DIR:-$(mktemp -d)}"
ssh_() { timeout 120 ssh -p 2223 -i "$L/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$L/known_hosts" -o ConnectTimeout=15 \
  omatest@127.0.0.1 "$1"; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Restaura cenario E mockMode ao sair: este script e uma ferramenta de captura,
# nao pode deixar o repo modificado so porque alguem tirou um print. mockMode
# tambem precisa ser restaurado desde que o default virou false (o painel le o
# CLI de verdade agora) -- sem forcar mockMode=true aqui, este script deixaria
# de renderizar o mock fixo e passaria a depender do que estiver salvo de
# verdade no guest no momento da captura.
ORIG_SCEN=$(sed -n 's/.*property string scenario: "\([a-z]*\)".*/\1/p' Panel.qml | head -1)
ORIG_MOCK=$(sed -n 's/.*property bool mockMode: \(true\|false\).*/\1/p' Panel.qml | head -1)
set_panel() {
  python3 - "$1" "$2" <<'PY'
import re,sys
p='Panel.qml'; s=open(p,encoding='utf-8').read()
s=re.sub(r'property string scenario: "\w+"', f'property string scenario: "{sys.argv[1]}"', s)
s=re.sub(r'property bool mockMode: (true|false)', f'property bool mockMode: {sys.argv[2]}', s)
open(p,'w',encoding='utf-8').write(s)
PY
}
restore_panel() {
  [ -n "$ORIG_SCEN" ] && [ -n "$ORIG_MOCK" ] && set_panel "$ORIG_SCEN" "$ORIG_MOCK"
}
trap restore_panel EXIT

set_panel "$SCEN" true
scp -q -P 2223 -i "$L/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$L/known_hosts" \
  Panel.qml omatest@127.0.0.1:.config/omarchy/plugins/brenoperucchi.omasession/Panel.qml || exit 1

ssh_ 'export XDG_RUNTIME_DIR=/run/user/1000; export WAYLAND_DISPLAY=wayland-1
pkill -x quickshell; sleep 9
Q=$(pgrep -x quickshell | head -1); [ -n "$Q" ] || { echo "quickshell nao respawnou"; exit 1; }
export $(tr "\0" "\n" < /proc/$Q/environ | grep -E "^OMARCHY_PATH=" | xargs)
timeout 10 omarchy-shell brenoperucchi.omasession open >/dev/null 2>&1
sleep 3
timeout 15 grim /tmp/shot.png && echo "grim ok"' || exit 1

scp -q -P 2223 -i "$L/id_ed25519" -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$L/known_hosts" \
  omatest@127.0.0.1:/tmp/shot.png "$SCRATCH/raw-$SCEN.png" || exit 1
ffmpeg -y -loglevel error -i "$SCRATCH/raw-$SCEN.png" \
  -vf "crop=$CROP,scale=1400:-1:flags=lanczos" "$SCRATCH/panel-$SCEN.png"
echo "pronto: $SCRATCH/panel-$SCEN.png"
