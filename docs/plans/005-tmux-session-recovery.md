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
  pergunta a `tmux list-clients` o nome da sessão e o `pane_current_path` do
  painel ativo. Só olha o socket default: um cliente noutro `-L`/`-S` é
  invisível aqui, do mesmo jeito que multiplexadores não-tmux (`screen`,
  `zellij`) continuam devolvendo "not recoverable" -- não estendido a eles
  porque nada neste projeto os testou.
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

## Rodada de revisão (omasession-4) — três achados de verdade, corrigidos

O primeiro commit (`f802061`) nunca tinha passado pelos revisores -- foi pra
uma rodada só depois, junto com a remoção do hyprresume. Não foi APPROVE dos
dois lados, e com razão:

- **P1, os dois bateram independente:** `tmux_session()` separava
  `client_pid`/`session_name`/`cwd` por espaço, e tmux aceita espaço em nome de
  sessão -- medido por um revisor com um nome sintético (`'a b'`), e pelo outro
  com um nome REAL já em uso neste projeto: `"Contratos Thera"`, o nome de uma
  sessão do próprio autor. `partition(" ")` cortava no primeiro espaço: o nome
  virava só a primeira palavra, o resto ia pro cwd. Depois do reboot isso cria
  uma sessão nova errada (`-A -s "Contratos"` não acha `"Contratos Thera"`) e
  deixa a sessão de verdade órfã -- exatamente a falha que este mecanismo
  existe para evitar, num nome que já é real. Corrigido com tab como separador
  (`-F '#{client_pid}\t#{session_name}\t#{pane_current_path}'`); `'a b'` virou
  caso em `test/tmux-cases.sh`.
- **P2, achado de um revisor, medido no host:** a checagem por `klass` também
  gate keeper do bloco de `child_cwd()`, então uma janela de classe custom
  (`foot --app-id=X`, `kitty --class=X`) -- o caso que motivou o projeto
  inteiro -- pulava cwd e tmux por completo. Confirmado trocando a classe de
  um kitty real por `TUI.tile`: o ramo simplesmente não rodava. Corrigido
  checando também o binário já resolvido (`Path(argv[0]).name`) contra
  `TRAILING_ARGV_TERMINALS`, não só a classe.
- **P2, achado do outro revisor:** o sufixo `-- tmux new -A -s <nome>` era
  acrescentado mesmo quando o `argv` resolvido já tinha um `--` seu (um
  `.desktop` customizado, ou o próprio `cmdline` preservando um `tmux attach`
  real) -- o resultado tinha DOIS `tmux` na mesma linha, e o segundo nunca
  roda. Corrigido: só acrescenta se `"--" not in result["argv"]`. Consequência
  aceita, não um bug: quando o `cmdline` já é fiel a um `tmux attach`/`new`
  real, ele é preservado como está, sem `-A` de brinde -- mesma política que
  o resto do resolvedor já tem para essa fonte ("last resort; may not carry
  the app's own state" é sobre isto).

Achados menores, também corrigidos na mesma rodada, fora deste mecanismo mas
no mesmo diff: `capture.py` fazia sua PRÓPRIA leitura de `hyprctl clients`
em vez de receber a que `session-save.sh` já tinha lido (quebrava a
invariante "ler a tela uma vez" que o guard depende); o guard contava
`[[window]]` cru em vez de janelas com `launch_cmd` de verdade (uma falha de
resolução silenciosa passava pelo piso e pela cobertura); `bin/omasession`
não migrava sessões do diretório antigo do hyprresume nem desfazia, no
uninstall, o marcador de uma versão anterior que desarmava o autostart dele;
`capture.py` não escapava o nome da sessão pelo serializador TOML. Todos
medidos e corrigidos; ver o diff de `omasession-4` em `.herdr/review/`.

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

- `test/tmux-cases.sh`: duas sessões tmux, uma delas com espaço no nome (o
  caso que a rodada de revisão encontrou quebrado), duas janelas `foot`
  anexadas, `omasession resolve --json` confere `command`/`cwd` de cada uma,
  depois um ciclo completo fechar-e-restaurar confere reanexação sem sessão
  órfã. 6/6 no lab, depois da correção do parsing.
- `test/guard-cases.sh`: 25/25, sem regressão -- confirmado de novo depois de
  três mudanças na captura (leitura única de `hyprctl`, contagem por
  `launch_cmd`, varredura de órfãos estendida).
- Classe custom (`foot --app-id=X` rodando `tmux attach` por dentro): medido
  manualmente no lab depois da correção -- `tmux_note` aparece (antes não
  aparecia nada), e o `cmdline` já completo é preservado sem duplicar o
  sufixo. Não virou caso automatizado ainda; é candidato natural pro próximo
  `tmux-cases.sh`.
- Reboot real: coberto acima, não automatizado (o mesmo motivo pelo qual
  `guard-cases.sh` não reinicia a VM sozinho -- caro e não determinístico o
  bastante pra rodar toda hora; a medição manual é o que este documento
  registra).
