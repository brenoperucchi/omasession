# 003 — O resolvedor de comando: apps arbitrários sem configuração

**Status:** aberto. É o diferencial que sobrou, e ele coincide com o fim da
nossa única dependência.

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
