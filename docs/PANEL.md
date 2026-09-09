# Panel.qml — estado, e como iterar nele

**Status:** mockup renderizado e validado no Omarchy 4.0.1 do lab em
2026-09-08. Os dados vêm do bloco `mock` dentro do próprio `Panel.qml`, não do
CLI — `bin/omasession` ainda não existe.

![estado normal](../screenshots/panel-healthy.png)

Três estados, alternáveis pela propriedade `scenario`:

| `scenario` | O que mostra | Por que existe |
|---|---|---|
| `healthy` | `6 windows` · `JUST NOW` · badge `on login` | O estado normal |
| `refused` | banner `partial save blocked: 1 window written, 6 on screen`, badge `refused` | O guard recusando é meia notícia boa (preservou a sessão) e meia ruim (o snapshot está mais velho do que parece). A versão que só escrevia no journal é como uma sessão de 6 janelas se perdeu sem ninguém ver |
| `contested` | banner sobre o daemon do hyprresume | Se outro processo escreve os mesmos arquivos, nosso guard não protege nada. É a única defesa possível: dizer |

O bloco `mock` **é o contrato** que `omasession status --json` terá de cumprir.
`mockMode: false` é a graduação, não um descarte.

## O loop de iteração

1. `omarchy dev ui preview` — a `dev-gallery` renderiza os componentes reais do
   `qs.Ui`. É o catálogo contra o qual se desenha.
2. Symlink do repo em `~/.config/omarchy/plugins/brenoperucchi.omasession/`; o
   shell descobre na hora. `omarchy plugin validate <pasta>` antes de habilitar.
3. `omarchy plugin enable brenoperucchi.omasession right`.

**Nunca itere isso na máquina de trabalho.** `docs/DESIGN.md` §6: um erro aqui
derruba a barra, o dock e o menu de uma vez. O lab existe para isso, e
`test/shoot.sh` renderiza um cenário e traz o PNG.

## A armadilha que custou mais caro

**O widget não aparece na barra e nada é reportado.** `Bar.qml:1581` dimensiona
cada slot por `activeItem.implicitWidth`, e um `Item` raiz sem largura implícita
vira um slot de largura zero: o plugin valida, carrega, aparece em
`listPlugins` com `enabled=True`, o painel abre por IPC — e a barra fica vazia,
sem uma linha no journal. O `Panel` raiz precisa de:

```qml
implicitWidth: button.implicitWidth
implicitHeight: button.implicitHeight
```

Todo painel de barra do Omarchy declara isso (`tailscale:337`, `monitor:349`,
`network:804`) e o `brenoperucchi.omabackup` também (`:1389`). Não é opcional.

Corolário para diagnosticar: `active=False` em `listPlugins` **não** significa
que o widget não montou — todos os widgets da barra, inclusive os que desenham,
reportam `active=False` quando o painel está fechado. Não use esse campo como
sinal.

## Outras armadilhas medidas

- **O hot reload deixa a superfície antiga na tela.** O shell loga
  `Local plugin changed, reloading` e remonta o widget, mas o conteúdo visível
  continua o da versão anterior — `omarchy plugin disable`+`enable` também não
  resolve. Só `pkill -x quickshell` mostra o novo. Para layout o reload serve;
  para conferir texto, reinicie o shell.
- **`screendump` do QMP não serve de diagnóstico.** Com `-display gtk,gl=on` o
  QEMU faz scanout por dmabuf e o comando responde `no surface` mesmo com a VM
  renderizando normalmente. Ele só devolve imagem quando a saída está inativa
  (aí captura a mensagem de fallback) — exatamente o inverso do que parece. Para
  ver a tela do convidado, use `grim` de dentro dele.
- **`grim` trava se a saída de vídeo estiver inativa**, sem mensagem. No guest
  isso aconteceu porque o autostart do lab mata o `hyprlock` e o Hyprland ficou
  preso no overlay de lockscreen crashado. Saída:
  `hyprctl eval 'hl.clear_crashed_lockscreen()'` e
  `hyprctl repl 'hl.dispatch(hl.dsp.dpms{state="on"})'`.

## Três APIs que eu tinha suposto errado

Ficam registradas porque nenhuma dá erro visível — dão layout errado em silêncio:

- `Panel` **não tem** propriedade `barWidget`. A superfície da barra é um
  `BarIconButton` filho; o popup é um `KeyboardPanel` ancorado nele.
- **Não existe `Color.error` nem `Color.warning`.** A paleta é
  `foreground` / `background` / `accent` / `urgent` / `muted`.
- `PanelHero.detail` vira um **badge à direita**, e um texto longo ali elide o
  `title` para `…`. Por isso o detalhe da recusa vive no banner, e o badge só
  diz `refused`.

## Pendente antes de virar o painel real

- Ligar em `omasession status --json` (`mockMode: false`).
- Os botões só fazem `console.log`. Devem chamar o CLI, nunca salvar ou
  replayar de dentro do QML (`docs/DESIGN.md` §6).
- Decidir onde a configuração mora: `manifest.json` declara `saveIntervalSec`,
  `restoreOnLogin` e `browserRestore` sob `barWidget.schema`, ou seja, no store
  do quickshell — mas quem precisa deles é o timer do systemd e o replay, que
  rodam quando não há shell. Falta a ponte (provavelmente
  `~/.config/omasession/config.json`), e ela muda a forma do CLI.
