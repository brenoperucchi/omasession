# 008 — Chrome volta com uma janela só depois do reboot pelo menu do Omarchy

**Status:** aberto. Causa medida no lab (2026-10-18/19) e reportada upstream
(omacom/omarchy#7085, comentário de 2026-10-20). Estratégia A provada à mão
no lab (2026-10-20) e implementada em `lib/browser_session.py`, chamada por
`repair_browser_session` em `lib/replay.py` (2026-10-21). Falta o passo 6
(host).

## O sintoma

Depois de reiniciar pelo menu do Omarchy, o Chrome devolve uma janela completa
e as outras não voltam. O OmaSession preenche o lugar delas com janelas vazias
(`+ empty window → wsN (browser did not restore "...")` no `last-restore.log`),
que são as "páginas sem conteúdo". Com `systemctl reboot`, `shutdown -h now` ou
depois de um freeze, tudo volta.

Host, restore de 2026-10-17 18:45: `8/8 placed (1 reopened by the browser)` e 7
janelas vazias. O journal mostra `systemd-run ... systemctl reboot --no-wall` às
18:11, que é o caminho do menu.

## Causa (medida)

`omarchy-system-reboot`, `-shutdown` e `-logout` rodam
`omarchy-hyprland-window-close-all` antes de desligar. Ele fecha uma janela de
cada vez (`hyprctl dispatch ... window.close` por endereço). O Chrome trata cada
fechamento, menos o último, como "o usuário fechou esta janela" e grava um
`kCommandWindowClosed` na sessão. Só a última janela fica guardada para o
restore.

Código do Chromium (`main`, conferido em 2026-10-20):

- `chrome/browser/sessions/session_service.cc`, `WindowClosing`/`WindowClosed`:
  com outras janelas abertas, a janela entra em `window_closing_ids_` e recebe
  `CreateWindowClosedCommand`. Só a última vai para `pending_window_close_ids_`.
- `chrome/browser/lifetime/browser_close_manager.cc`, `CloseBrowsers`: na saída
  normal (inclusive SIGTERM), os session services são desligados antes, "so an
  exit can restore all browsers open before exiting".

Lab, sem reboot, perfil isolado, 4 janelas, relançado com
`--restore-last-session`, 3 rodadas de cada:

| como o Chrome terminou | voltou |
|---|---|
| `omarchy-hyprland-window-close-all` (omarchy 4.0.3) | 1 de 4 (só a última) |
| `timeout 2s omarchy-hyprland-window-close-all --wait` (quattro, #15231) | 1 de 4 (só a última) |
| `kill -TERM` no processo principal | 4 de 4 |

O fix que entrou upstream (#15231) não resolve: ele espera o processo sair, mas
as janelas já saíram da sessão. Até o Omarchy mudar, a correção precisa estar
do nosso lado.

## Formato do arquivo de sessão

Conferido em `components/sessions/core/command_storage_backend.cc` e
`session_service_commands.cc`, e nos arquivos do host:

- `~/.config/google-chrome/Default/Sessions/Session_<timestamp>`, com header
  `SNSS` (`0x53534E53`) e versão 3, sem criptografia, no host. A versão 5
  (criptografada, em outro diretório) existe no código. Se aparecer, o
  OmaSession não mexe no arquivo.
- Cada registro: `uint16` tamanho, `uint8` id do comando, payload.
  `kCommandSetTabWindow = 0`, `kCommandTabClosed = 16`,
  `kCommandWindowClosed = 17`, e o marcador de estado inicial é `255`.
- No startup, o Chrome lê o `Session_*` válido de timestamp mais novo e **apaga
  os outros** (`DeleteLastSessionFiles`). Qualquer cópia que a gente queira
  guardar tem que ficar fora de `Sessions/`.

## Estratégias

### A. Desfazer os fechamentos da cauda (escolhida, provada no lab)

O close-all só **acrescenta** registros no fim do arquivo, e o estado das
janelas fechadas continua mais acima. Antes de abrir o Chrome, o OmaSession
tira os `WindowClosed` e `TabClosed` da cauda, e o Chrome lê a sessão como se
tivesse saído por SIGTERM. Não precisa de backup periódico.

**Medição, lab, 2026-10-20** (perfil isolado, 4 janelas de 2 abas,
`timeout 2s omarchy-hyprland-window-close-all --wait`, 3 rodadas):

1. **Só acrescenta: confirmado.** Nas 3 rodadas, o arquivo depois do close-all
   começa byte a byte igual ao de antes. Não houve `Truncate`.
2. **A cauda não é só `WindowClosed`: refutada a versão ingênua.** Os 23 a 28
   registros acrescentados misturam, por janela fechada, um `TabClosed` para
   cada aba, depois o `WindowClosed`, intercalados com `SetWindowBounds3` das
   janelas que sobram (o Hyprland reorganiza o tiling a cada fechamento).
   Exemplo da rodada 1 da primeira medição:
   `TabClosed ×2, SetWindowBounds3 ×4, WindowClosed, TabClosed ×2, ...`.
   Remover só a sequência final de fechamentos não tira nada, porque o último
   registro é um `SetWindowBounds3` (medido: 1 de 4 depois desse strip).
3. **O que funciona:** pegar a sequência final de registros de "ruído de
   desligamento" (`SetWindowBounds3`, `TabClosed`, `WindowClosed`,
   `SetActiveWindow`, `LastActiveTime`, ids 14, 16, 17, 20, 21) e, dentro
   dela, remover só os ids 16 e 17. Resultado: **4 de 4 nas 3 rodadas**, e a
   sessão gravada depois tem **2 abas em cada janela**. Em cada rodada foram
   removidos 9 registros (3 `WindowClosed` + 6 `TabClosed`).

As mudanças de tamanho ficam no arquivo e não atrapalham: o OmaSession
reposiciona as janelas depois.

Ferramenta usada na medição (não é código do projeto):
`/tmp/claude-1000/-home-brenoperucchi-Devs-omasession/c67f7947-8e75-4ed5-9683-93a3c298550b/scratchpad/snss.py`
(`dump`, `appended`, `strip2`).

### B. Backup periódico (fallback, se A não se sustentar)

O timer do snapshot (30s) copia o `Session_*` mais novo para
`~/.local/state/omasession/chrome-session/` quando o número de janelas abertas
no arquivo bate com o número de janelas do Chrome no Hyprland. No boot, se o
arquivo atual tiver menos janelas que o backup e terminar com a rajada de
`WindowClosed`, o backup entra no lugar dele.

Custo: perde o que mudou depois do último backup (até 30s de navegação), e o
backup não pode ser sobrescrito durante o próprio close-all (no meio dele, o
Hyprland e o arquivo concordam em "1 janela"). Por isso A é preferida.

## Quando aplicar (as duas estratégias)

A rajada final de `WindowClosed` também aparece se o usuário fechar janelas na
mão logo antes de um `systemctl reboot`. Para não ressuscitar janelas fechadas
de propósito:

- só aplicar quando o último snapshot do OmaSession tiver mais janelas do
  Chrome do que o arquivo de sessão deixa abertas;
- nunca devolver mais janelas do que o snapshot tinha. Quando a cauda tem mais
  fechamentos do que isso (janelas fechadas na mão logo antes do reboot, sem
  atividade no meio), só os **mais recentes** voltam, porque os do close-all
  são sempre os últimos;
- registrar a decisão no `last-restore.log`, aplicando ou não
  (`google-chrome session: 1 -> 4 window(s), undid the closes ...` ou
  `google-chrome session: left as is: <motivo>`).

A cauda não tem sinal que separe o close-all de um fechamento manual (esses
registros não guardam horário), então o limite do snapshot é a defesa. A
rodada 2 do passo 5 mede esse caso.

## Segurança

O arquivo de sessão é o histórico de abas do usuário. Mesmas regras do
`arm_browser_profile` (`lib/replay.py`):

- recusar se o Chrome estiver rodando com esse perfil (`profile_in_use`);
- copiar o original para `~/.local/state/omasession/browser-sessions/<app>/`
  antes de qualquer mudança, pelo `safe_fs` (o mesmo caminho verificado do
  resto do estado), e manter as 5 últimas cópias;
- escrever com tempfile no mesmo diretório e `os.replace`, mantendo o nome;
- qualquer coisa inesperada (header diferente, versão 5, registro vazio) faz
  o OmaSession não mexer e registrar o motivo. Um comando fora do ruído na
  cauda só encurta a cauda;
- `Sessions_Encrypted/` ao lado não bloqueia: o Chrome 152 do guest grava as
  duas cópias e lê a sem criptografia (uma versão que recusava nesse caso
  trouxe 1 de 3 janelas no lab, 2026-10-21). O log avisa que a cópia
  criptografada existe, para o dia em que o Chrome passar a preferir ela
  (`kEncryptSessionStorage`). O Chrome 155 do host não cria esse diretório;
- um último registro incompleto (Chrome morto no meio do flush) é ignorado,
  como o próprio Chrome faz (`SessionFileReader::Read`);
- qualquer exceção no reparo vira uma linha no log e o restore segue.

## Plano

1. ~~Medir no lab as hipóteses de A.~~ Feito em 2026-10-20, ver acima.
2. ~~Experimento manual.~~ Feito: 4 de 4, 2 abas por janela, 3 rodadas. A
   está provada e B fica só como registro.
3. **Regressão primeiro** (fluxo do projeto), em `test/`: arquivos SNSS
   sintéticos, com a cauda no formato medido (`TabClosed` por aba,
   `WindowClosed`, `SetWindowBounds3` intercalados).
   - 4 janelas e essa cauda fechando 3, com snapshot de 4: hoje sai 1 janela,
     com o fix saem 4.
   - Um comando fora do ruído (por exemplo `UpdateTabNavigation`) depois do
     último `WindowClosed`: a cauda acaba ali, não mexe.
   - Mesma cauda com snapshot de 1: não mexe.
   - Header inválido, versão 5 e registro truncado: não mexe e registra o
     motivo.
4. **Código**: um leitor/escritor SNSS pequeno em `lib/` (só os campos
   necessários) e a chamada em `replay.py`, logo depois de
   `arm_browser_profile` (que recusa com o browser rodando), para
   `google-chrome` e `chromium`.
5. **Lab de ponta a ponta**: feito em 2026-10-21. Perfil normal do Chrome do
   guest, 4 janelas de 2 abas, `omarchy-system-reboot` (omarchy 4.0.3). O
   `last-restore.log` registrou
   `google-chrome session: 1 -> 4 window(s), undid the closes ...` e o Chrome
   reabriu E1 a E4. Nessa primeira tentativa o snapshot ainda era o antigo (8
   janelas): o reboot veio com o `browser-repair` ainda esperando os títulos
   ("restart detected, waiting for titled windows"), e o OmaSession completou
   o layout com 4 janelas vazias. Isso é do save, não desta correção.
   Repetido com o snapshot já atualizado:
   - **Rodada 1**, 4 janelas no snapshot: `1 -> 4 window(s)`,
     `4/4 placed (4 reopened by the browser)`, cada janela no workspace
     salvo, casada pelo título, nenhuma janela vazia.
   - **Rodada 2**, uma janela (F1) fechada na mão e o snapshot atualizado para
     3 antes do reboot pelo menu: `1 -> 3 window(s)`, `3/3 placed`, e a F1
     continuou fechada.
   - **Rodada 3**, código depois da revisão (backup pelo `safe_fs`, aviso
     de `Sessions_Encrypted` no lugar da recusa), 3 janelas novas G2 a G4
     abertas depois de fechar à mão as 3 anteriores: `1 -> 3 window(s) ...;
     an encrypted copy exists too and was not changed`, `3/3 placed (3
     reopened by the browser)`. A tentativa anterior, com a recusa, trouxe 1
     de 3 e duas janelas vazias.
6. **Host**: depois de um reboot real pelo menu, conferir no `last-restore.log`
   a linha da decisão e a ausência de janelas vazias.

## Limitações conhecidas (revisão de 2026-10-21)

Duas revisões independentes conferiram o código contra o Chromium `main`
(ids, payloads `ClosedPayload` de 16 bytes, "só acrescenta", índice e seleção
das abas devolvidas). Nenhum caminho corrompe a sessão. O que sobra:

- **Reparo parcial.** Um `UpdateTabNavigation` no meio da rajada (aba que
  muda o título sozinha, tipo "(3) WhatsApp") encerra a cauda ali, e só as
  janelas fechadas depois dele voltam. O mesmo acontece se o reset de 250
  comandos do Chrome (`session_service_base.cc`) cair no meio da rajada: ele
  abre um `Session_` novo só com as janelas ainda abertas. O log mostra o
  número (`1 -> 2`), então dá para ver no host.
- **O limite do snapshot é aproximado.** `want` conta as janelas do snapshot
  com a mesma class: DevTools e popups inflam, uma janela no scratchpad
  reduz, e o snapshot pode ter até 30s. Fechar uma janela à mão e reiniciar
  pelo menu dentro desses 30s faz ela voltar.
- **Uma aba fechada à mão** numa janela que o close-all fecha logo depois, sem
  nada no meio, volta junto. O arquivo não tem como separar os dois casos.
- **`sessionStorage`** das abas devolvidas volta vazio (o Chrome já marcou o
  namespace para descarte no fechamento). "Recentemente fechadas" continua
  listando as janelas devolvidas.

## Fora do escopo

- Corrigir o Omarchy. O report está em omacom/omarchy#7085. Se um fix por
  SIGTERM entrar lá, este código continua inofensivo: sem rajada final, ele não
  faz nada.
- Brave, Edge e outros Chromium: o formato é o mesmo, mas só entram quando
  houver medição com eles.
- O log geral de "como desligou, quantas janelas voltaram" no host, discutido
  na mesma conversa. A linha de decisão de "Quando aplicar" cobre o Chrome. O log geral,
  se ainda fizer falta, vira plano próprio.
