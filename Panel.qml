import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// OmaSession's bar surface.
//
// DESIGN.md §6 applies here first: an error in this file takes down the bar,
// the dock and the menu at once, and restoring has to work when there is no
// shell. So the panel only ever reports what `omasession status --json` says,
// and asks the CLI to act -- it never saves, replays, or writes config.json
// itself. `mockMode` exists only for `test/shoot.sh`, which cannot rely on a
// real session existing in the lab guest it screenshots; production always
// runs with it false, reading the live CLI.
Panel {
  id: root
  moduleName: "brenoperucchi.omasession"
  ipcTarget: "brenoperucchi.omasession"
  manageIpc: true

  // Sem estas duas linhas o widget nao aparece na barra e nada e reportado:
  // Bar.qml dimensiona cada slot por `activeItem.implicitWidth`, e um Item raiz
  // sem largura implicita vira um slot de largura zero. Todo painel de barra do
  // Omarchy declara isto (tailscale:337, monitor:349, network:804).
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ── data source: the real CLI, or a fixed mock for screenshots ────────────
  // The contract below is `omasession status --json`'s actual shape, not a
  // guess at it: `real` is populated by parsing that command's output, and
  // `mock` is kept only because test/shoot.sh needs a session to render that
  // does not depend on whatever happens to be open in the lab guest at the
  // time. Two states worth keeping there, switchable by `scenario`:
  //   healthy    a recent snapshot, nothing wrong
  //   refused    the guard blocked a save (exit 3) -- the case that used to
  //              destroy the session silently, so it must be visible
  property bool mockMode: false
  property string scenario: "healthy"

  readonly property string cliPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/brenoperucchi.omasession/bin/omasession"

  readonly property var mock: ({
    "healthy": {
      "windows": 4, "workspaces": 4, "agoSec": 42, "refused": false, "detail": "",
      "intervalSec": 30, "restoreOnLogin": true,
      "captured": [
        { "ws": 1, "mon": "DP-1", "cls": "foot",                 "app": "Foot",     "title": "~/Devs/my project",  "detail": "~/Devs/my project", "warn": "", "resolvable": true },
        { "ws": 2, "mon": "DP-1", "cls": "org.gnome.Nautilus",   "app": "Files",    "title": "Home",               "detail": "", "warn": "", "resolvable": true },
        { "ws": 3, "mon": "HDMI-A-1", "cls": "md.obsidian.Obsidian", "app": "Obsidian", "title": "Vault",          "detail": "", "warn": "", "resolvable": true },
        { "ws": 4, "mon": "HDMI-A-1", "cls": "chromium",             "app": "Chromium", "title": "Hyprland Wiki",  "detail": "tabs restored by the browser", "warn": "", "resolvable": true }
      ]
    },
    "refused": {
      "windows": 4, "workspaces": 4, "agoSec": 214, "refused": true,
      "detail": "partial save blocked: 1 window written, 4 on screen",
      "intervalSec": 30, "restoreOnLogin": true,
      "captured": [
        { "ws": 1, "mon": "DP-1", "cls": "foot",                 "app": "Foot",     "title": "~/Devs/my project",  "detail": "~/Devs/my project", "warn": "", "resolvable": true },
        { "ws": 2, "mon": "DP-1", "cls": "org.gnome.Nautilus",   "app": "Files",    "title": "Home",               "detail": "", "warn": "", "resolvable": true },
        { "ws": 3, "mon": "HDMI-A-1", "cls": "md.obsidian.Obsidian", "app": "Obsidian", "title": "Vault",          "detail": "", "warn": "", "resolvable": true },
        { "ws": 4, "mon": "HDMI-A-1", "cls": "some.unknown.App",     "app": "App",      "title": "no desktop entry", "detail": "", "warn": "", "resolvable": false }
      ]
    }
  })

  // ── real status, from the CLI ─────────────────────────────────────────────
  property bool cliMissing: false
  property bool checking: false
  property string lastError: ""
  property var realStatus: null

  readonly property var status: mockMode ? mock[scenario] : realStatus

  function refresh() {
    if (mockMode || checking) return
    checking = true
    statusProc.running = true
  }

  Process {
    id: statusProc
    command: [root.cliPath, "status", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        root.checking = false
        var text = String(this.text).trim()
        if (text === "") return
        try {
          root.realStatus = JSON.parse(text)
          root.cliMissing = false
          root.lastError = ""
        } catch (e) {
          // A CLI that changed shape or printed a stray line is not the same
          // failure as one that is simply not there -- and binding straight
          // to a malformed object is how a single bad run turns into a wall
          // of TypeErrors across every Text below instead of one message here.
          root.lastError = "status did not return valid JSON"
        }
      }
    }
    onExited: function(code) {
      root.checking = false
      if (code !== 0 && root.realStatus === null) {
        // exit 127 from the shell (command not found) is the common case on a
        // machine where `install` never ran; anything else still means the
        // panel has nothing trustworthy to show, and saying so beats staying
        // blank with no explanation.
        root.cliMissing = true
      }
    }
  }

  // Fire-and-forget actions. Each is its own Process because Save and Restore
  // can be pressed independently and neither should block the other; both
  // refresh the real status once they exit, whatever the exit code -- the
  // guard's own refusal message is exactly what the "refused" banner below is
  // for, so a non-zero exit here is data, not a reason to hide the result.
  Process {
    id: saveProc
    command: [root.cliPath, "save"]
    onExited: function(code) { root.refresh() }
  }

  Process {
    id: restoreProc
    command: [root.cliPath, "restore"]
    onExited: function(code) { root.refresh() }
  }

  Process {
    id: configProc
    property string key: ""
    property string value: ""
    command: [root.cliPath, "config", "set", key, value]
    onExited: function(code) { root.refresh() }
  }

  function setRestoreOnLogin(on) {
    configProc.key = "restoreOnLogin"
    configProc.value = on ? "true" : "false"
    configProc.running = true
  }

  Component.onCompleted: refresh()
  // Refreshed on open (the case that matters most: the user is looking right
  // now) and on a slow timer regardless, so the bar icon's `attention` state
  // -- visible even with the panel closed -- does not go stale for the whole
  // interval between two logins.
  onOpenedChanged: if (root.opened) root.refresh()
  Timer {
    interval: Math.max(10, root.intervalSec) * 1000
    running: !root.mockMode
    repeat: true
    onTriggered: root.refresh()
  }
  readonly property int windowCount:   status ? status.windows : 0
  readonly property int workspaceCount: status ? status.workspaces : 0
  readonly property int intervalSec:   status ? status.intervalSec : 30
  readonly property bool restoreOnLogin: status ? status.restoreOnLogin : true

  // Monitor -> workspace -> janelas. Um workspace só faz sentido junto do
  // monitor em que estava: "workspace 2" na tela do meio e "workspace 2" na da
  // direita são lugares diferentes, e quem tem duas telas navega pensando na
  // tela primeiro. Com um monitor só o nível some, porque aí ele não informa
  // nada e só empurra a lista para baixo.
  readonly property var byMonitor: {
    var mons = {}
    var order = []
    for (var i = 0; i < captured.length; i++) {
      var w = captured[i]
      var m = w.mon || "?"
      if (!mons[m]) { mons[m] = {}; order.push(m) }
      if (!mons[m][w.ws]) mons[m][w.ws] = []
      mons[m][w.ws].push(w)
    }
    var out = []
    for (var k = 0; k < order.length; k++) {
      var name = order[k]
      var wss = Object.keys(mons[name]).sort(function(a, b) { return a - b })
      var groups = []
      var flat = []
      for (var j = 0; j < wss.length; j++) {
        groups.push({ ws: parseInt(wss[j]), items: mons[name][wss[j]] })
        // Achatado pra grade de cards: cada item já carrega seu próprio `ws`
        // (veio de `captured`), então a grade não perde a informação de
        // workspace mesmo sem o cabeçalho "WORKSPACE N" por cima do grupo.
        for (var x = 0; x < mons[name][wss[j]].length; x++)
          flat.push(mons[name][wss[j]][x])
      }
      out.push({ mon: name, workspaces: groups, flatItems: flat })
    }
    return out
  }

  readonly property bool multiMonitor: byMonitor.length > 1


  readonly property int comingBack: {
    var n = 0
    for (var i = 0; i < captured.length; i++) if (captured[i].resolvable) n++
    return n
  }

  readonly property int unresolvable: {
    var n = 0
    for (var i = 0; i < captured.length; i++) if (!captured[i].resolvable) n++
    return n
  }
  readonly property var captured:      status ? status.captured : []
  readonly property bool guardRefused: status ? status.refused : false
  readonly property bool attention:    guardRefused

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.55)

  // A snapshot going stale is the failure the user cannot see any other way:
  // the plugin looks installed and quietly does nothing. Show it as a number.
  readonly property string agoText: {
    if (!status) return "never"
    var s = status.agoSec
    // Segundos exatos abaixo de um minuto: antes virava "just now" porque
    // isto ficava dentro do `meta` do PanelHero, que maiusculiza sozinho e
    // fazia "42s ago" virar "42S AGO" -- ilegível com a unidade colada no
    // número. Não mora mais lá (virou parte da linha de fatos, texto normal,
    // maiúscula/minúscula como escrito), e a razão de existir deste campo é
    // justamente notar quando o snapshot está envelhecendo -- "just now"
    // escondia isso pro primeiro minuto inteiro.
    if (s < 60) return s + "s ago"
    if (s < 3600) return Math.floor(s / 60) + " min ago"
    return Math.floor(s / 3600) + " h ago"
  }

  // ── bar ──────────────────────────────────────────────────────────────────
  // `text:` em vez de `iconComponent:` -- a forma que omarchy.monitor usa.
  // Com um iconComponent envolvendo um OpticalGlyph o widget nao desenhava
  // nada na barra, sem uma linha sequer no journal, embora o mesmo glifo
  // desenhasse no painel. Nao investigar de novo: use text.
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    foreground: root.attention ? root.barForeground
                               : Qt.darker(root.barForeground, 1.35)
    onPressed: function(b) { root.toggle() }
  }

  // ── panel ──────────────────────────────────────────────────────────────
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.spacing.lg

        Item {
          width: parent.width
          height: hero.implicitHeight

          PanelHero {
            id: hero
            anchors.left: parent.left
            anchors.right: loginToggle.left
            anchors.rightMargin: Style.space(8)
            title: "OmaSession"
            // O `meta` do PanelHero é onde os painéis nativos do Omarchy põem
            // estado, não slogan -- o tailscale escreve "Tailscale is
            // disconnected" ali. "pick up where you left off" era tagline, e
            // palavra por palavra a mesma do OmaResume. Este slot passa a
            // responder a pergunta que o interruptor ao lado levanta e que
            // antes só tinha resposta no hover -- PanelHero já deixa em
            // caixa alta sozinho, por isso o texto aqui fica em minúsculas.
            meta: root.cliMissing     ? "cli not installed"
                : !root.status        ? "nothing saved yet"
                : root.restoreOnLogin ? "restores on next login"
                                      : "restore on login is off"
            foreground: root.fg
            iconComponent: Component {
              Item {
                implicitWidth: Style.font.display
                implicitHeight: Style.font.display
                OpticalGlyph {
                  anchors.centerIn: parent
                  text: ""
                  color: root.fg
                  fontSize: Style.font.display
                }
              }
            }
          }

          // O interruptor mora aqui, na linha do nome, porque é o estado do
          // plugin inteiro -- não mais um item de lista entre outros. O texto
          // aparece no hover e diz o que ACONTECE, não o que a opção se chama:
          // a pergunta do usuário é "e se eu reiniciar agora?".
          ToggleSwitch {
            id: loginToggle
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            checked: root.restoreOnLogin
            onToggled: {
              if (root.mockMode) return
              root.setRestoreOnLogin(loginToggle.checked)
            }

            // PanelToolTip é um ToolTip (Popup): não aceita anchors, e se
            // posiciona sozinho quando é filho do item a que pertence. Tentar
            // ancorá-lo derruba o widget inteiro com "Cannot assign to
            // non-existent property verticalCenter" -- e derruba junto o ícone
            // da barra, porque é o mesmo arquivo.
            PanelToolTip {
              visible: loginToggle.containsMouse
              fontFamily: Style.font.family
              text: root.restoreOnLogin
                    ? "Reboot now and these windows come back"
                    : "Reboot now and nothing reopens"
            }
          }
        }

        // ── CLI ausente ──────────────────────────────────────────────────
        // "0 windows come back" com cara de sessão real, quando na verdade o
        // CLI nem existe, seria pior que dizer nada: pareceria uma sessão
        // vazia de verdade, não uma instalação incompleta. As duas causas têm
        // ações diferentes -- rodar install, ou não fazer nada porque não há
        // sessão mesmo -- e só uma mensagem explícita distingue.
        Rectangle {
          visible: !root.mockMode && root.cliMissing
          width: parent.width
          height: missingText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)
          Text {
            id: missingText
            anchors.left: parent.left; anchors.right: parent.right
            anchors.margins: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            text: "omasession was not found at " + root.cliPath
                + " -- install the plugin's CLI, or run `omasession install`."
          }
        }

        Rectangle {
          visible: !root.mockMode && !root.cliMissing && root.lastError !== ""
          width: parent.width
          height: errorText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)
          Text {
            id: errorText
            anchors.left: parent.left; anchors.right: parent.right
            anchors.margins: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            text: root.lastError
          }
        }

        // ── o cartão da sessão salva ─────────────────────────────────────
        // O que estava salvo, quando, e os dois verbos. Os botões levam rótulo:
        // um ícone sozinho na barra é aceitável porque tem tooltip, mas dentro
        // do painel ninguém deveria adivinhar o que "salvar" e "restaurar" são.
        Rectangle {
          visible: root.mockMode || !root.cliMissing
          width: parent.width
          height: savedCol.implicitHeight + Style.space(18)
          radius: Style.space(4)
          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
          border.width: 1
          border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10)

          Column {
            id: savedCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: Style.space(9)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.md

            // A pergunta que importa segundos antes de reiniciar não é
            // "o que eu tenho aberto" -- isso está na tela. É "o que volta".
            // Um inventário responde a primeira; este número responde a
            // segunda, e é a única das duas que uma ferramenta com lista fixa
            // de aplicativos não consegue responder, porque ela não distingue
            // "não suportado" de "vai funcionar".
            Text {
              text: root.comingBack === root.windowCount
                    ? "All " + root.windowCount + " come back"
                    : root.comingBack + " of " + root.windowCount + " come back"
              color: root.unresolvable > 0 ? Color.urgent : root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
            }

            // ── a régua do que volta ─────────────────────────────────────
            // Um segmento por janela capturada: aceso = tem comando para
            // reabrir, apagado = não tem. É a frase acima em forma de
            // desenho, e é a marca que o OmaResume não copia sem antes ter
            // um resolvedor -- a régua dele seria sempre cheia, porque todo
            // item dele diz "Ready". Sem campo novo: `resolvable` já está
            // no contrato.
            Item {
              id: ruler
              width: parent.width
              height: Style.space(4)
              visible: root.captured.length > 0

              // A largura vem do pai (savedCol, que tem largura por
              // anchors), nunca da soma dos filhos: um Row dimensionado
              // pelos filhos que se dimensionam pelo Row é o laço de
              // binding clássico, a mesma família da armadilha de
              // implicitWidth do PANEL.md.
              readonly property real gap: Style.spacing.sm
              readonly property real seg:
                Math.max(1, (width - gap * Math.max(0, root.captured.length - 1))
                            / Math.max(1, root.captured.length))

              Repeater {
                model: root.captured
                Rectangle {
                  x: index * (ruler.seg + ruler.gap)
                  width: ruler.seg
                  height: ruler.height
                  radius: height / 2
                  color: modelData.resolvable
                         ? Color.accent
                         : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.18)
                }
              }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.workspaceCount
                    + (root.workspaceCount === 1 ? " workspace" : " workspaces")
                    + (root.multiMonitor ? ", " + root.byMonitor.length + " monitors" : "")
                    + (root.status ? " · saved " + root.agoText : "")
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Row {
              width: parent.width
              spacing: Style.spacing.lg

              Button {
                text: "Save now"
                bordered: true
                enabled: !root.mockMode && !saveProc.running
                onClicked: saveProc.running = true
              }
              Button {
                text: "Restore session"
                bordered: true
                enabled: !root.mockMode && !restoreProc.running
                onClicked: restoreProc.running = true
              }
            }
          }
        }

        // ── avisos ───────────────────────────────────────────────────────
        // Nada disto existe no OmaResume, e é o que impede este painel de ser
        // decorativo: o guard recusando e um segundo escritor nos mesmos
        // arquivos são as duas formas de perder a sessão sem perceber.
        Rectangle {
          visible: root.guardRefused
          width: parent.width
          height: refusedText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.14)
          Text {
            id: refusedText
            anchors.left: parent.left; anchors.right: parent.right
            anchors.margins: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            text: "Last save refused, session preserved — "
                  + (root.status ? root.status.detail : "")
          }
        }

        PanelSeparator { width: parent.width; visible: root.mockMode || !root.cliMissing }

        // ── as janelas: monitor -> grade de cards ─────────────────────────
        // Uma linha por app, cheia de largura, é a mesma silhueta do
        // OmaResume (um item = uma linha). Uma grade de 2 colunas por
        // monitor é mais densa e não precisa do cabeçalho "WORKSPACE N"
        // repetido pra cada grupo -- o número de workspace vira um detalhe
        // discreto dentro do próprio card, não uma linha inteira por grupo.
        Repeater {
          model: (root.mockMode || !root.cliMissing) ? root.byMonitor : []

          Column {
            width: column.width
            spacing: Style.spacing.sm

            // O nível do monitor só aparece quando há mais de um. Numa tela só
            // ele seria uma linha constante repetindo o óbvio.
            Text {
              visible: root.multiMonitor
              text: modelData.mon
              color: Color.accent
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            Grid {
              id: appGrid
              width: parent.width
              columns: 2
              columnSpacing: Style.spacing.sm
              rowSpacing: Style.spacing.sm

              Repeater {
                model: modelData.flatItems

                Rectangle {
                  width: (appGrid.width - appGrid.columnSpacing) / 2
                  height: tileCol.implicitHeight + Style.space(14)
                  radius: Style.space(3)
                  color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.04)

                  Column {
                    id: tileCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.spacing.xxs

                    Text {
                      width: parent.width
                      elide: Text.ElideRight
                      text: modelData.app
                      color: root.fg
                      font.family: Style.font.family
                      font.pixelSize: Style.font.body
                    }
                    // O que este card vai RECUPERAR. Para um terminal é o
                    // diretório -- e quando não dá para recuperá-lo, dizer
                    // isso vale mais que repetir o título da janela, que
                    // ninguém vai reconhecer depois do reboot de qualquer
                    // forma. "ws N" na frente porque o card sozinho, sem o
                    // cabeçalho de grupo que existia antes, não diz mais em
                    // que workspace a janela volta.
                    Text {
                      width: parent.width
                      elide: Text.ElideRight
                      text: "ws" + modelData.ws + " · " + (modelData.detail || modelData.title)
                      color: root.dim
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                    // Silêncio significa "vai voltar". Só a exceção fala.
                    Text {
                      width: parent.width
                      elide: Text.ElideRight
                      visible: text !== ""
                      text: !modelData.resolvable ? "no command"
                            : (modelData.warn || "")
                      color: Color.urgent
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }
        }

        PanelSeparator { width: parent.width; visible: root.mockMode || !root.cliMissing }

        Text {
          visible: root.mockMode || !root.cliMissing
          width: parent.width
          wrapMode: Text.WordWrap
          color: root.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          text: {
            var base = "Snapshot every " + root.intervalSec + "s"
            if (root.unresolvable > 0)
              return base + " · " + root.unresolvable
                     + " window(s) have no launch command; nothing will reopen them"
            return base
          }
        }
      }
    }
  }
}
