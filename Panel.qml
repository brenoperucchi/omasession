import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// OmaSession's bar surface -- currently a MOCKUP.
//
// Every number below comes from the `mock` block, not from the CLI: this file
// exists to settle what the panel should say before `bin/omasession` exists to
// say it. Flip `mockMode` to false once `omasession status --json` lands; the
// shape of `mock` is the contract that command has to satisfy.
//
// DESIGN.md §6 applies here first: an error in this file takes down the bar,
// the dock and the menu at once, and restoring has to work when there is no
// shell. So the panel only ever reports, and asks the CLI to act -- it never
// saves or replays anything itself.
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

  // ── mock data ────────────────────────────────────────────────────────────
  // The contract for `omasession status --json`. Three states worth designing
  // for, switchable by `scenario` while iterating:
  //   healthy    a recent snapshot, nothing wrong
  //   refused    the guard blocked a save (exit 3) -- the case that used to
  //              destroy the session silently, so it must be visible
  //   contested  hyprresume's daemon is running, writing the same files
  property bool mockMode: true
  property string scenario: "healthy"

  readonly property var mock: ({
    "healthy": {
      "windows": 4, "workspaces": 4, "agoSec": 42, "refused": false, "detail": "",
      "daemonActive": false, "intervalSec": 30, "restoreOnLogin": true,
      "captured": [
        { "ws": 1, "mon": "DP-1", "cls": "foot",                 "app": "Foot",     "title": "~/Devs/my project",  "resolvable": true },
        { "ws": 2, "mon": "DP-1", "cls": "org.gnome.Nautilus",   "app": "Files",    "title": "Home",               "resolvable": true },
        { "ws": 3, "mon": "HDMI-A-1", "cls": "md.obsidian.Obsidian", "app": "Obsidian", "title": "Vault",          "resolvable": true },
        { "ws": 4, "mon": "HDMI-A-1", "cls": "chromium",             "app": "Chromium", "title": "Hyprland Wiki",  "resolvable": true }
      ]
    },
    "refused": {
      "windows": 4, "workspaces": 4, "agoSec": 214, "refused": true,
      "detail": "partial save blocked: 1 window written, 4 on screen",
      "daemonActive": false, "intervalSec": 30, "restoreOnLogin": true,
      "captured": [
        { "ws": 1, "mon": "DP-1", "cls": "foot",                 "app": "Foot",     "title": "~/Devs/my project",  "resolvable": true },
        { "ws": 2, "mon": "DP-1", "cls": "org.gnome.Nautilus",   "app": "Files",    "title": "Home",               "resolvable": true },
        { "ws": 3, "mon": "HDMI-A-1", "cls": "md.obsidian.Obsidian", "app": "Obsidian", "title": "Vault",          "resolvable": true },
        { "ws": 4, "mon": "HDMI-A-1", "cls": "some.unknown.App",     "app": "App",      "title": "no desktop entry", "resolvable": false }
      ]
    },
    "contested": {
      "windows": 1, "workspaces": 1, "agoSec": 8, "refused": false, "detail": "",
      "daemonActive": true, "intervalSec": 30, "restoreOnLogin": false,
      "captured": [
        { "ws": 1, "mon": "DP-1", "cls": "foot", "app": "Foot", "title": "~/Devs/omasession", "resolvable": true }
      ]
    }
  })

  readonly property var status: mock[scenario]
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
      for (var j = 0; j < wss.length; j++)
        groups.push({ ws: parseInt(wss[j]), items: mons[name][wss[j]] })
      out.push({ mon: name, workspaces: groups })
    }
    return out
  }

  readonly property bool multiMonitor: byMonitor.length > 1


  readonly property int unresolvable: {
    var n = 0
    for (var i = 0; i < captured.length; i++) if (!captured[i].resolvable) n++
    return n
  }
  readonly property var captured:      status ? status.captured : []
  readonly property bool guardRefused: status ? status.refused : false
  readonly property bool daemonActive: status ? status.daemonActive : false
  readonly property bool attention:    guardRefused || daemonActive

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.55)

  // A snapshot going stale is the failure the user cannot see any other way:
  // the plugin looks installed and quietly does nothing. Show it as a number.
  readonly property string agoText: {
    if (!status) return "never"
    var s = status.agoSec
    // PanelHero renderiza o meta em maiusculas, e "42s ago" vira "42S AGO":
    // a unidade colada no numero fica ilegivel. Abaixo de um minuto o numero
    // exato nao informa nada de util de qualquer forma.
    if (s < 60) return "just now"
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
            meta: "pick up where you left off"
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
            onToggled: console.log("mock: escreveria restoreOnLogin em config.json")

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

        // ── o cartão da sessão salva ─────────────────────────────────────
        // O que estava salvo, quando, e os dois verbos. Os botões levam rótulo:
        // um ícone sozinho na barra é aceitável porque tem tooltip, mas dentro
        // do painel ninguém deveria adivinhar o que "salvar" e "restaurar" são.
        Rectangle {
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

            Item {
              width: parent.width
              height: savedLabel.implicitHeight
              Text {
                id: savedLabel
                anchors.left: parent.left
                text: "SAVED SESSION"
                color: Qt.darker(root.fg, 1.35)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
              Text {
                anchors.right: parent.right
                text: root.agoText
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            Text {
              text: root.windowCount + (root.windowCount === 1 ? " window" : " windows")
                    + "  ·  " + root.workspaceCount
                    + (root.workspaceCount === 1 ? " workspace" : " workspaces")
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
            }

            Row {
              width: parent.width
              spacing: Style.spacing.lg

              Button {
                text: "Save now"
                bordered: true
                onClicked: console.log("mock: omasession save")
              }
              Button {
                text: "Restore session"
                bordered: true
                onClicked: console.log("mock: omasession restore")
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

        Rectangle {
          visible: root.daemonActive
          width: parent.width
          height: daemonText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)
          Text {
            id: daemonText
            anchors.left: parent.left; anchors.right: parent.right
            anchors.margins: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            text: "hyprresume's daemon is writing the same session files. "
                + "Run `omasession install` to disarm it."
          }
        }

        PanelSeparator { width: parent.width }

        // ── as janelas: monitor -> workspace -> apps ─────────────────────
        Repeater {
          model: root.byMonitor

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

            Repeater {
              model: modelData.workspaces

              Column {
                width: column.width
                spacing: Style.spacing.sm

                PanelSectionHeader {
                  width: parent.width
                  text: "WORKSPACE " + modelData.ws
                }

                Repeater {
                  model: modelData.items

                  Rectangle {
                    width: parent.width
                    height: itemCol.implicitHeight + Style.space(10)
                    radius: Style.space(3)
                    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.04)

                    Column {
                      id: itemCol
                      anchors.left: parent.left
                      anchors.right: statusText.left
                      anchors.margins: Style.space(7)
                      anchors.rightMargin: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.spacing.xxs

                      Text {
                        text: modelData.app
                        color: root.fg
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                      }
                      Text {
                        width: parent.width
                        elide: Text.ElideRight
                        text: modelData.title
                        color: root.dim
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                      }
                    }

                    // "Ready" só onde o resolvedor achou um .desktop. Uma
                    // janela que não volta tem de aparecer ANTES do reboot.
                    Text {
                      id: statusText
                      anchors.right: parent.right
                      anchors.rightMargin: Style.space(7)
                      anchors.verticalCenter: parent.verticalCenter
                      text: modelData.resolvable ? "Ready" : "No command"
                      color: modelData.resolvable ? Qt.darker(root.fg, 1.4) : Color.urgent
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }
        }

        PanelSeparator { width: parent.width }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          color: root.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          text: {
            var base = "Snapshot every " + root.intervalSec + "s"
            if (root.unresolvable > 0)
              return base + " · " + root.unresolvable + " window(s) have no command and will not come back"
            return base + " · Save now before rebooting"
          }
        }
      }
    }
  }
}
