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
      "windows": 6, "agoSec": 42, "refused": false, "detail": "",
      "daemonActive": false,
      "captured": [
        { "ws": 1, "cls": "foot",                 "title": "~/Devs/omasession" },
        { "ws": 1, "cls": "foot",                 "title": "btop" },
        { "ws": 2, "cls": "chromium",             "title": "Hyprland Wiki" },
        { "ws": 3, "cls": "org.gnome.Nautilus",   "title": "Devs" },
        { "ws": 3, "cls": "foot",                 "title": "floating-probe" },
        { "ws": 4, "cls": "md.obsidian.Obsidian", "title": "Vault" }
      ]
    },
    "refused": {
      "windows": 6, "agoSec": 214, "refused": true,
      "detail": "partial save blocked: 1 window written, 6 on screen",
      "daemonActive": false,
      "captured": [
        { "ws": 1, "cls": "foot",                 "title": "~/Devs/omasession" },
        { "ws": 1, "cls": "foot",                 "title": "btop" },
        { "ws": 2, "cls": "chromium",             "title": "Hyprland Wiki" },
        { "ws": 3, "cls": "org.gnome.Nautilus",   "title": "Devs" },
        { "ws": 3, "cls": "foot",                 "title": "floating-probe" },
        { "ws": 4, "cls": "md.obsidian.Obsidian", "title": "Vault" }
      ]
    },
    "contested": {
      "windows": 1, "agoSec": 8, "refused": false, "detail": "",
      "daemonActive": true,
      "captured": [ { "ws": 1, "cls": "foot", "title": "~/Devs/omasession" } ]
    }
  })

  readonly property var status: mock[scenario]
  readonly property int windowCount:   status ? status.windows : 0
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

  // ── panel ────────────────────────────────────────────────────────────────
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.spacing.lg

        PanelHero {
          width: parent.width
          title: root.windowCount + (root.windowCount === 1 ? " window" : " windows")
          meta: root.agoText
          // Curto de proposito: PanelHero divide a linha entre title e
          // detail, e um detail longo elide o title para "…". O texto
          // especifico da recusa vive no banner logo abaixo.
          detail: root.guardRefused ? "refused" : "on login"
          foreground: root.fg
          iconComponent: Component {
            OpticalGlyph { text: ""; color: root.fg; fontSize: Style.font.display }
          }
        }

        // The guard refusing is good news (it kept the session) and bad news
        // (the snapshot is older than it looks). Both halves have to be on
        // screen: the version of this that only wrote to the journal is how a
        // six-window session was lost without anyone noticing.
        Rectangle {
          visible: root.guardRefused
          width: parent.width
          height: refusedText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.14)

          Text {
            id: refusedText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            wrapMode: Text.WordWrap
            color: root.fg
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            text: root.status ? root.status.detail : ""
          }
        }

        // Item (F) of the review, made visible instead of documented: our guard
        // cannot protect files another writer owns. If hyprresume's daemon is
        // running it silently defeats the whole protection, so say so.
        Rectangle {
          visible: root.daemonActive
          width: parent.width
          height: daemonText.implicitHeight + Style.space(12)
          radius: Style.space(3)
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)

          Text {
            id: daemonText
            anchors.left: parent.left
            anchors.right: parent.right
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

        PanelSectionHeader { width: parent.width; text: "Captured" }
        PanelSeparator { width: parent.width }

        Repeater {
          model: root.captured

          Item {
            width: column.width
            height: Style.space(18)

            Text {
              id: wsBadge
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "ws" + modelData.ws
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              anchors.left: wsBadge.right
              anchors.leftMargin: Style.space(8)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              elide: Text.ElideRight
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              // Class first: it is what the replay matches on and what the user
              // recognises. The title is the soft key (DESIGN.md §4).
              text: modelData.cls + "  ·  " + modelData.title
            }
          }
        }

        PanelSeparator { width: parent.width }

        Row {
          spacing: Style.spacing.lg

          PanelActionButton {
            iconText: ""                        // save
            tooltipText: "Save the session now"
            foreground: root.fg
            onClicked: console.log("mock: omasession save")
          }

          PanelActionButton {
            iconText: ""                        // restore
            tooltipText: "Restore the saved session"
            foreground: root.fg
            onClicked: console.log("mock: omasession restore")
          }
        }
      }
    }
  }
}
