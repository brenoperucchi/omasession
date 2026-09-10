# 003 — O resolvedor de comando: apps arbitrários sem configuração

**Status:** resolvedor escrito e medido (`lib/resolve.py`, `omasession resolve`).
Falta trocar a captura do hyprresume pela nossa — o passo 3 abaixo. Deixou de
ser melhoria teórica em 2026-09-09: um caso real de uso quebrou por causa
exatamente disso — ver "Evidência que fecha a decisão" abaixo.

## Evidência que fecha a decisão (2026-09-09)

Cenário real, não sintético: o Herdr (terminal-workspace-manager do usuário,
cliente-servidor) roda sua TUI como `foot --app-id=TUI.tile herdr` — uma classe
custom, sem `.desktop`, com argumentos que definem qual aplicação abre dentro
do terminal. Testado no lab com reboot real:

- `herdr.service` (o servidor) **sobreviveu ao reboot sozinho**, ancorado em
  `default.target` como o nosso timer — nenhuma dependência do OmaSession.
- A janela cliente **não voltou funcional**: reapareceu como um `foot` genérico
  sem o `herdr` rodando dentro, e duplicada (2 janelas em vez de 1).

Causa isolada e reproduzível com um comando, fora de qualquer código nosso:

    $ hyprresume resolve TUI.tile
    TUI.tile → foot

O hyprresume resolve a classe pelo binário real do processo (`/proc/<pid>/exe`
→ `/usr/bin/foot`), descartando os argumentos de linha de comando. `omasession
resolve` — que usa `/proc/<pid>/cmdline`, não `/proc/<pid>/exe` — resolveu essa
mesma janela corretamente (`foot --app-id=TUI.tile herdr`) antes do reboot. Mas
o `replay.py` restaura pelo `launch_cmd` que vem do `hyprresume save`, não pelo
resultado de `omasession resolve`: o resolvedor certo já existe e já foi
medido, só não está no caminho que o restore realmente usa.

A duplicação da janela não foi investigada a fundo — pode ser um efeito
colateral do mesmo mecanismo de captura do hyprresume, ou algo específico de
como o `herdr` interage com o terminal. Não vale investigar isso separadamente:
troca-se a captura, o problema desaparece com ela ou vira um caso a medir de
novo depois, num resolvedor cujo comportamento a gente entende.

## Por que agora

`dimef.omaresume` (marketplace, 08/09/2026) restaura sessão no Omarchy 4
falando a mesma API Lua. A premissa de abertura deste projeto — *"nenhuma
ferramenta fala a API que o compositor aceita"* — deixou de distinguir.

O que ainda distingue está no `launch_spec` dele:

```python
terminals = {'foot': [...], 'Alacritty': [...], 'kitty': [...], 'com.mitchellh.ghostty': [...]}
browsers  = {'brave-browser': ..., 'chromium': ..., 'google-chrome': ..., 'firefox': ...}
...
return None
```

Oito classes conhecidas. Qualquer outra janela devolve `None` e **não volta**, a
menos que o usuário escreva o `argv` à mão em `applications`. No nosso cenário
de lab, isso deixaria `nautilus`, `obsidian` e o resto de fora.

Nós resolvemos isso hoje — mas de empréstimo. O `last.toml` vem do `hyprresume`,
sem manutenção desde março de 2026, e o `DESIGN.md` §2 já dizia: *"se ele tiver
que sair, o trabalho é o resolvedor de comando"*. Escrever o resolvedor é a
mesma tarefa que virar o diferencial e cortar a dependência.

## O que o resolvedor precisa fazer

Dada uma janela do `hyprctl clients`, produzir um comando que a traga de volta:

1. **`/proc/<pid>/cmdline`** — o caso fácil, e o mais enganoso. Um `chromium`
   traz uma linha que não contém nenhuma URL; um Electron traz o caminho do
   binário empacotado. Serve como último recurso, não como primeira escolha.
2. **Índice de `.desktop`** — casar `class`/`initialClass` com `StartupWMClass`
   ou com o nome do arquivo em `/usr/share/applications` e
   `~/.local/share/applications`, e usar o `Exec=` (removendo os campos `%u`,
   `%U`, `%f`, `%F`, `%i`, `%c`, `%k`). É o que resolve `nautilus` e
   `md.obsidian.Obsidian` sem o usuário configurar nada.
3. **cgroup** — `/proc/<pid>/cgroup` dá `app-<launcher>-<id>.scope` sob o
   `app.slice` do systemd, e é o caminho para Flatpak (`app-flatpak-<appid>-…`),
   onde o `cmdline` mostra o `bwrap` e não o aplicativo.
4. **cwd real do terminal** — o `cwd` do processo da janela é o do emulador, não
   o do shell. Descer `/proc/<pid>/task/<pid>/children` até o primeiro filho e
   ler o `cwd` dele. Já fazemos isso indiretamente via hyprresume; passa a ser
   nosso.

## Como saber que está certo

Não medir "quantas janelas voltaram" — essa é a métrica que deixa o hyprresume
reportar 6/6 contra uma tela vazia. Medir **resolução por classe**, contra um
cenário que cubra os quatro caminhos acima:

| classe | caminho esperado | por que está na lista |
|---|---|---|
| `foot` | desktop + cwd do filho | terminal, o caso com cwd |
| `org.gnome.Nautilus` | `.desktop` | o `omaresume` não cobre |
| `md.obsidian.Obsidian` | `.desktop` (AppImage/Electron) | `cmdline` engana |
| `chromium` | `.desktop`, sessão pelo browser | PID e cmdline compartilhados |
| um Flatpak qualquer | cgroup | `cmdline` mostra `bwrap` |

Critério: **classe resolvida para um comando que relança**, verificado
lançando-o de fato — não inspecionando a string.

### Medido em 2026-09-09

No guest, fechando tudo e relançando **apenas** pelo comando resolvido:

| classe | via | relançou na classe certa |
|---|---|---|
| `foot` | `.desktop` + cwd | sim, e o cwd bateu: `~/Devs/my project` |
| `org.gnome.Nautilus` | `.desktop` | sim |
| `md.obsidian.Obsidian` | `.desktop` | sim |
| `chromium` | `.desktop` | sim |

**4/4.** As duas do meio são exatamente as que o `launch_spec` do `omaresume`
devolveria como `None`.

No host, contra uma sessão real de 19 janelas: 19/19 resolvidas, incluindo
`qemu` e dois `kitty` com `--class` próprio, ambos por `cmdline` — que é o
caminho correto ali, porque o `.desktop` traria um `kitty` genérico sem os
argumentos que definem aquela janela.

**O caminho `cgroup` continua NÃO verificado**: não há Flatpak instalado no
guest. Não afirmar que funciona até haver.

### A armadilha do cwd, que nenhuma das duas ferramentas trata

Pegar `children[0]` e ler o `cwd` dele — o que o `omaresume` faz — devolve o
diretório errado com cara de certo. Medido no host: cada janela `kitty` tem PID
próprio, mas seus filhos são `kitten` e `tmux: client`, **todos** reportando
`$HOME`, enquanto os shells em que o usuário está vivem dentro do servidor do
tmux, em outra árvore de processos. Restaurar sete terminais em `$HOME` teria
parecido sucesso.

`child_cwd()` portanto procura um processo que seja de fato um shell, desce até
três níveis, e quando só encontra um cliente de multiplexador devolve
`(None, "cwd lives in the multiplexer server, not in this tree")`. Um
"não recuperado" honesto vale mais que um diretório errado com confiança.

## Ordem

1. Escrever `lib/resolve.py` com os quatro caminhos e um `resolve --explain`
   no CLI, que diz por qual caminho cada janela foi resolvida. Sem isso, um
   resolvedor errado é indistinguível de um app que não abre.
2. Medir a tabela acima no lab.
3. Só então trocar a leitura do `last.toml` por captura própria, e o
   `hyprresume` deixa de ser dependência. Manter o formato do arquivo: o
   `test/fixtures/last.toml` é o contrato, e a troca deve ser invisível para o
   `replay.py`.

## O que não fazer

- Não copiar a whitelist. Uma tabela de oito classes é o problema, não a
  solução; se o resolvedor precisar de casos especiais, que sejam exceções
  sobre um mecanismo geral, não o mecanismo.
- Não remover o `hyprresume` antes de (2) passar. Ele funciona hoje; trocar um
  resolvedor que funciona por um que ainda não foi medido é a troca que este
  projeto passou a semana inteira criticando.
