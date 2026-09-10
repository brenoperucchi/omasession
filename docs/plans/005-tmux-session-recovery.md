# 005 — Reanexar a sessão tmux certa, não recuperar o que há dentro dela

**Status:** feito e medido em 2026-09-10. `lib/resolve.py` reconhece um
terminal cujo filho é um cliente tmux e aponta pra sessão certa
(`tmux new -A -s <nome>`); a recuperação de conteúdo continua sendo do
`tmux-resurrect`/`tmux-continuum` do próprio usuário, não deste plugin.

## Por que isso apareceu

`child_cwd()` já sabia dizer, honestamente, quando o filho de um terminal era
um cliente de multiplexador em vez de um shell: "cwd lives in the multiplexer
server, not in this tree" (docs/plans/003). O que faltava era perguntar: dado
que sabemos que é um cliente tmux, o próprio tmux sabe pra qual sessão ele
está anexado -- por que não perguntar a ele?

A pergunta inicial foi levantada pensando no Herdr como motivador (que tem seu
próprio multiplexador interno e por isso **não** roda dentro de tmux -- rodar
os dois juntos gera conflito, medido pelo usuário na prática: `kitty + herdr`
sem tmux no meio é o fluxo real). Uma consulta cega aos dois revisores do
projeto (`.herdr/ask/omasession-1/`) discordou sobre se valia a pena perseguir
sem o Herdr como caso -- não por valores diferentes, por premissas diferentes.
Um revisor assumiu que não havia caso concreto sem o Herdr; o outro mediu, ao
vivo, no host de trabalho: **quatro janelas kitty rodando `tmux new`, quatro
sessões tmux distintas, e `~/.tmux.conf` já configurado com
`tmux-resurrect`+`tmux-continuum` e `@continuum-restore on`.** Verificado de
forma independente antes de decidir a favor de qualquer um dos dois: a
medição batia. Não era uma discordância de valores para escalar ao usuário --
era uma pergunta factual com resposta checável, e a resposta favorecia
perseguir.

## O que foi medido no lab antes de escrever qualquer código

Cenário: dois `foot`, cada um `-D <dir> -- tmux new`, cada um numa sessão
tmux distinta com diretório distinto. `omasession save`, reboot real.

**Sem o fix**: `resolve.py` resolvia pelo `.desktop` (que ganha do `cmdline`
na ordem de autoridade) e devolvia `foot` puro -- pior do que se temia. Não é
"sessão órfã duplicada"; é perda total. Depois do reboot: duas janelas `foot`
rodando `bash` solto em `$HOME`, **nenhum servidor tmux sequer chegou a
subir**. `error connecting to /tmp/tmux-1000/default: No such file or
directory`.

**Com o fix**: `launch_cmd = "foot -- tmux new -A -s <nome>"`. Depois do
mesmo reboot real: as duas sessões existem, `attached=1` cada uma, cada
`foot` anexado exatamente à sessão que tinha antes. Sem sessão extra. O
`tmux-continuum` (`@continuum-restore on`, sem `@continuum-boot`) sobe seu
próprio restore quando o `omasession restore` cria a primeira sessão do
servidor recém-nascido -- e como o nome bate com o que o `-A` está pedindo,
os dois convergem em vez de competir.

Achado colateral, não relacionado ao resolvedor: `@continuum-restore on`
dispara no PRIMEIRO `tmux new` de um servidor novo, mesmo vindo de outro
teste inteiramente sem relação. Um snapshot velho em
`~/.local/share/tmux/resurrect/last` contaminou silenciosamente duas rodadas
de teste antes de ser identificado -- `test/tmux-cases.sh` agora recusa rodar
se encontrar um servidor tmux vivo ou um snapshot do resurrect, em vez de
tentar limpar sozinho (apagar o snapshot de alguém sem perguntar é exatamente
o tipo de ação que este projeto evita em outros lugares).

## O que o fix faz, exatamente

Em `lib/resolve.py`:

- `tmux_session(pid)`: descobre se algum filho do terminal é um
  `tmux: client` (mesma varredura de `child_cwd()`, mesma recusa quando há
  mais de um -- não dá pra saber qual janela é qual). Se achar exatamente um,
  pergunta a `tmux list-clients` o nome da sessão e o `session_path`. Só olha
  o socket default: um cliente noutro `-L`/`-S` é invisível aqui, do mesmo
  jeito que multiplexadores não-tmux (`screen`, `zellij`) continuam
  devolvendo "not recoverable" -- não estendido a eles porque nada neste
  projeto os testou.
- `TRAILING_ARGV_TERMINALS = {"foot", "kitty"}`: não é a mesma lista que
  `TERMINAL_CWD_FLAG` (essa é sobre o *flag* de diretório; esta é sobre o
  terminal aceitar um comando solto no final da linha, sem `-e`/`--`
  obrigatório). `foot` medido direto nesta rodada; `kitty` sustentado pela
  própria evidência do projeto (README: "19/19 ... dois kitty com --class
  próprio, ambos por cmdline", relançados com os argumentos originais
  intactos). Alacritty/ghostty/wezterm ficam de fora não por serem
  diferentes, por nunca terem sido usados por este projeto -- e este projeto
  já criticou o suficiente ferramentas que afirmam o que não mediram.
- Quando os dois batem (terminal conhecido + sessão tmux achada), o `argv`
  vira `[..., "--", "tmux", "new", "-A", "-s", "<nome>"]`, e o `cwd` (o
  `session_path` do tmux, de brinde) vai para o mesmo campo que o `cwd_note`
  ocuparia -- `replay.py` já sabia inserir esse flag antes do `--` (comentário
  próprio, "hyprresume records terminals as `foot -- btop`"), então nenhuma
  mudança foi necessária lá.

## O que continua sendo do usuário, não deste plugin

- Conteúdo dos panes, quantos existiam, o que rodava em cada um: isso é
  `tmux-resurrect`. O OmaSession garante a janela e a sessão; o que há dentro
  da sessão é entre o usuário e a config de tmux dele.
- Nenhum `send-keys`, nenhuma whitelist de comandos pra relançar. Esse era
  exatamente o defeito que a pesquisa sobre `tmux-resurrect` encontrou nele
  mesmo (só relança uma lista fixa de programas) -- não copiado aqui.
- `screen`, `zellij`, `abduco`, `dtach`: continuam caindo no "not recoverable"
  honesto que já existia. Estender exigiria medir cada um, não assumir que o
  mecanismo do tmux generaliza.

## Testes

- `test/tmux-cases.sh` (novo): duas sessões tmux nomeadas, duas janelas
  `foot` anexadas, `omasession resolve --json` confere `command`/`cwd`, depois
  um ciclo completo fechar-e-restaurar confere reanexação sem sessão órfã.
  6/6 no lab.
- `test/guard-cases.sh`: 25/25, sem regressão -- a mudança é aditiva e só age
  quando `child_cwd()` já teria devolvido "not recoverable".
- Reboot real: coberto acima, não automatizado (o mesmo motivo pelo qual
  `guard-cases.sh` não reinicia a VM sozinho -- caro e não determinístico o
  bastante pra rodar toda hora; a medição manual é o que este documento
  registra).
