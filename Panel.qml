import QtQuick
// Only for ScrollBar. Both this and qs.Ui export a `Button` -- QML resolves
// to whichever import comes LAST, so qs.Ui below must stay after this one, or
// the two Buttons below silently switch to QtQuick.Controls' own style with
// no error. network/Panel.qml (Omarchy's own) keeps the same order for the
// same reason.
import QtQuick.Controls
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
  readonly property string pluginVersion: "0.5.1"

  readonly property var mock: ({
    "healthy": {
      "windows": 4, "workspaces": 4, "agoSec": 42, "refused": false, "detail": "",
      "intervalSec": 30, "restoreOnLogin": true, "browserRepair": true,
      "browserRepairChrome": true,
      "appRestoreGhostty": true,
      "appAvailability": {
        "googleChrome": { "installed": true, "captured": false },
        "ghostty": { "installed": true, "captured": false }
      },
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
      "intervalSec": 30, "restoreOnLogin": true, "browserRepair": true,
      "browserRepairChrome": true,
      "appRestoreGhostty": true,
      "appAvailability": {
        "googleChrome": { "installed": true, "captured": false },
        "ghostty": { "installed": true, "captured": false }
      },
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
  property var browserRepairChromeOverride: null
  property bool checking: false
  property bool refreshQueued: false
  property string lastError: ""
  property string browserRestoreOutput: ""
  property string browserRestoreError: ""
  property string ghosttyRestoreOutput: ""
  property string ghosttyRestoreError: ""
  property var appRestoreGhosttyOverride: null
  property var realStatus: null

  readonly property var status: mockMode ? mock[scenario] : realStatus

  function refresh() {
    if (mockMode) return
    // The tab that is showing reloads with the status, not only when picked.
    if (view === "saved") loadSessions()
    if (view === "layout") loadLayout(layoutName)
    // A writer can finish while status --json is still being read. Queue that
    // refresh instead of dropping it, so the final toggle state is read back.
    if (checking) {
      refreshQueued = true
      return
    }
    checking = true
    statusProc.running = true
  }

  Process {
    id: statusProc
    command: [root.cliPath, "status", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
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
      if (root.refreshQueued) {
        root.refreshQueued = false
        root.refresh()
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
    onExited: function(code) {
      root.refresh()
    }
  }

  Process {
    id: restoreProc
    command: [root.cliPath, "restore"]
    onExited: function(code) { root.refresh() }
  }

  // ── saved sessions, history and the layout preview ──────────────────────
  // "windows" (the cards), "layout" (screens drawn to scale) or "saved".
  property string view: "windows"
  property var sessions: null          // list --json: { named: [], history: [] }
  property string layoutName: "last"   // which session the Layout tab draws
  property var layoutData: null        // layout <name>: monitors, workspaces, windows
  property string layoutWorkspace: ""
  property string savedMessage: ""

  function loadSessions() {
    if (mockMode || listProc.running) return
    listProc.running = true
  }
  function loadLayout(name) {
    if (mockMode) return
    layoutName = name
    layoutProc.running = false
    layoutProc.command = [root.cliPath, "layout", name]
    layoutProc.running = true
  }
  function previewSession(name) {
    view = "layout"
    loadLayout(name)
  }
  // Omarchy gives each monitor workspaces 1-10 in blocks of ten (31-40 on the
  // fourth block); the bar shows 1-10, so the preview does too.
  function workspaceLabel(id) {
    var n = parseInt(id)
    return isNaN(n) ? String(id) : String(((n - 1) % 10) + 1)
  }
  // history-20261021-222300 is UTC; show it in local time.
  function sessionTitle(name) {
    var m = /^history-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(name)
    if (!m) return name
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]))
    return d.toLocaleString(Qt.locale(), "ddd MMM d, h:mm AP")
  }
  function agoLabel(sec) {
    if (sec < 90) return sec + "s ago"
    if (sec < 5400) return Math.floor(sec / 60) + "m ago"
    if (sec < 172800) return (sec / 3600).toFixed(1) + "h ago"
    return Math.floor(sec / 86400) + "d ago"
  }

  Process {
    id: listProc
    command: [root.cliPath, "list", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.sessions = JSON.parse(String(this.text).trim()) }
        catch (e) { root.sessions = null }
      }
    }
  }

  Process {
    id: layoutProc
    command: [root.cliPath, "layout", "last"]
    stdout: StdioCollector {
      onStreamFinished: {
        var data = null
        try { data = JSON.parse(String(this.text).trim()) } catch (e) {}
        root.layoutData = (data && !data.error) ? data : null
        if (!root.layoutData) {
          // Thinning can delete the snapshot being previewed. Go back to the
          // current save instead of retrying a name that is gone.
          if (root.layoutName !== "last") Qt.callLater(function() { root.loadLayout("last") })
          return
        }
        var spaces = root.layoutData.workspaces || []
        var shown = ""
        var mons = root.layoutData.monitors || []
        for (var i = 0; i < mons.length && shown === ""; i++) {
          var active = String(mons[i].activeWorkspace)
          if (spaces.indexOf(active) !== -1) shown = active
        }
        if (spaces.indexOf(root.layoutWorkspace) !== -1) shown = root.layoutWorkspace
        root.layoutWorkspace = shown !== "" ? shown : (spaces.length ? spaces[0] : "")
      }
    }
  }

  Process {
    id: saveAsProc
    property string name: ""
    command: [root.cliPath, "save", name]
    stderr: StdioCollector {
      onStreamFinished: {
        var t = String(this.text).trim()
        if (t !== "") root.savedMessage = t.split("\n").pop()
      }
    }
    onExited: function(code) {
      if (code === 0) root.savedMessage = "Saved \"" + saveAsProc.name + "\""
      root.loadSessions()
    }
  }

  Process {
    id: restoreNamedProc
    property string name: ""
    command: [root.cliPath, "restore", name]
    onExited: function(code) {
      root.savedMessage = code === 0 ? "Restored \"" + restoreNamedProc.name + "\""
                                     : "Restore of \"" + restoreNamedProc.name + "\" finished with problems"
      root.refresh()
    }
  }

  onViewChanged: {
    if (view === "saved") loadSessions()
    if (view === "layout" && layoutData === null) loadLayout(layoutName)
  }

  Process {
    id: browserRestoreProc
    command: [root.cliPath, "restore-app", "google-chrome"]
    stdout: StdioCollector {
      id: browserRestoreStdout
      waitForEnd: true
      onStreamFinished: root.browserRestoreOutput = String(text || "").trim()
    }
    stderr: StdioCollector {
      id: browserRestoreStderr
      waitForEnd: true
      onStreamFinished: root.browserRestoreError = String(text || "").trim()
    }
    onExited: function(code) {
      var output = String(browserRestoreStdout.text || root.browserRestoreOutput || "").trim()
      var error = String(browserRestoreStderr.text || root.browserRestoreError || "").trim()
      var lines = (error || output).split("\n").filter(function(line) { return line.trim() !== "" })
      root.browserRestoreError = code === 0 ? "" : (lines.length > 0 ? lines[lines.length - 1] : "Chrome restore failed (exit " + code + ")")
      root.browserRestoreOutput = code === 0 ? (lines.length > 0 ? lines[lines.length - 1] : "Chrome windows restored") : ""
      root.refresh()
    }
  }

  Process {
    id: ghosttyRestoreProc
    command: [root.cliPath, "restore-app", "ghostty"]
    stdout: StdioCollector {
      id: ghosttyRestoreStdout
      waitForEnd: true
      onStreamFinished: root.ghosttyRestoreOutput = String(text || "").trim()
    }
    stderr: StdioCollector {
      id: ghosttyRestoreStderr
      waitForEnd: true
      onStreamFinished: root.ghosttyRestoreError = String(text || "").trim()
    }
    onExited: function(code) {
      var output = String(ghosttyRestoreStdout.text || root.ghosttyRestoreOutput || "").trim()
      var error = String(ghosttyRestoreStderr.text || root.ghosttyRestoreError || "").trim()
      var lines = (error || output).split("\n").filter(function(line) { return line.trim() !== "" })
      root.ghosttyRestoreError = code === 0 ? "" : (lines.length > 0 ? lines[lines.length - 1] : "Ghostty restore failed (exit " + code + ")")
      root.ghosttyRestoreOutput = code === 0 ? (lines.length > 0 ? lines[lines.length - 1] : "Ghostty windows restored") : ""
      root.refresh()
    }
  }

  Process {
    id: configProc
    property string key: ""
    property string value: ""
    command: [root.cliPath, "config", "set", key, value]
    onExited: function(code) { root.refresh() }
  }

  Process {
    id: browserRepairProc
    command: [root.cliPath, "config", "set", "browserRepairChrome", browserRepairValue]
    property string browserRepairValue: "true"
    onExited: function(code) {
      root.browserRepairChromeOverride = null
      root.refresh()
    }
  }

  Process {
    id: ghosttyRestoreConfigProc
    command: [root.cliPath, "config", "set", "appRestoreGhostty", ghosttyRestoreValue]
    property string ghosttyRestoreValue: "true"
    onExited: function(code) {
      root.appRestoreGhosttyOverride = null
      root.refresh()
    }
  }

  function setRestoreOnLogin(on) {
    configProc.key = "restoreOnLogin"
    configProc.value = on ? "true" : "false"
    configProc.running = true
  }

  function setBrowserRepairChrome(on) {
    browserRepairProc.browserRepairValue = on ? "true" : "false"
    browserRepairProc.running = true
  }

  function setAppRestoreGhostty(on) {
    ghosttyRestoreConfigProc.ghosttyRestoreValue = on ? "true" : "false"
    ghosttyRestoreConfigProc.running = true
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
  // Mock statuses carry neither field, so only a real status can raise this.
  readonly property string automationOff: {
    if (root.mockMode || !root.status) return ""
    if (root.status.pluginEnabled === false)
      return "Automatic save and login restore are paused: omarchy-shell does not list this plugin as enabled."
    if (root.status.timer !== undefined && root.status.timer !== "active")
      return "Automatic save is off: the snapshot timer is not running. Run `omasession install` to set it up."
    return ""
  }
  readonly property bool browserRepair: status ? status.browserRepair : true
  readonly property bool browserRepairChrome: browserRepairChromeOverride !== null
                                             ? browserRepairChromeOverride
                                             : (status && status.browserRepairChrome !== undefined
                                                ? status.browserRepairChrome : true)
  readonly property var appAvailability: status && status.appAvailability !== undefined
                                          ? status.appAvailability
                                          : ({
                                              googleChrome: { installed: true, captured: true },
                                              ghostty: { installed: true, captured: true }
                                            })
  readonly property var googleChromeAvailability: appAvailability.googleChrome || ({ installed: true, captured: true })
  readonly property var ghosttyAvailability: appAvailability.ghostty || ({ installed: true, captured: true })
  readonly property bool chromeInstalled: !!googleChromeAvailability.installed
  readonly property bool chromeCaptured: !!googleChromeAvailability.captured
  readonly property bool ghosttyInstalled: !!ghosttyAvailability.installed
  readonly property bool ghosttyCaptured: !!ghosttyAvailability.captured
  readonly property bool appRestoreGhostty: appRestoreGhosttyOverride !== null
                                             ? appRestoreGhosttyOverride
                                             : (status && status.appRestoreGhostty !== undefined
                                                ? status.appRestoreGhostty : true)
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
    // maxHeight aqui e no orcamento do Flickable la embaixo tem que ser o
    // MESMO valor -- achado da revisao (omasession-10, verificado ao vivo
    // 2026-09-10): o orcamento usava availableCardHeight sozinho, sem este
    // teto, entao com conteudo grande o suficiente (18 workspaces) o
    // Flickable se permitia crescer mais do que a superficie do popup
    // realmente tem (que E capada por este 680) -- o mesmo vazamento por
    // cima da borda que este arquivo existe pra fechar, so que voltando pelo
    // teto externo em vez da lista interna.
    readonly property real maxHeight: Style.space(680)
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, maxHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.spacing.lg

        // The hero stays visible while the body grows or scrolls. Everything
        // that can become long belongs below it, so a failure message cannot
        // push the actual controls outside the popup surface.
        Item {
          id: heroBlock
          width: parent.width
          height: hero.implicitHeight

          Item {
            id: hero
            anchors.left: parent.left
            anchors.right: loginToggle.left
            anchors.rightMargin: Style.space(8)
            implicitHeight: Math.max(heroIcon.height, heroLabels.implicitHeight)

            Item {
              id: heroIcon
              width: Style.font.display
              height: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              OpticalGlyph {
                anchors.centerIn: parent
                text: ""
                color: root.fg
                fontSize: Style.font.display
              }
            }

            Column {
              id: heroLabels
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(14)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Row {
                id: titleRow
                width: parent.width
                spacing: Style.space(8)

                Text {
                  textFormat: Text.PlainText
                  text: "OmaSession"
                  width: Math.min(implicitWidth,
                                  Math.max(0, parent.width - versionText.implicitWidth - parent.spacing))
                  color: root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.title
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  textFormat: Text.PlainText
                  id: versionText
                  text: root.pluginVersion
                  color: root.dim
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                  anchors.verticalCenter: parent.verticalCenter
                }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: root.cliMissing     ? "CLI NOT INSTALLED"
                    : !root.status        ? "NOTHING SAVED YET"
                    : root.restoreOnLogin ? "RESTORES ON NEXT LOGIN"
                                          : "RESTORE ON LOGIN IS OFF"
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
                elide: Text.ElideRight
              }
            }
          }

          ToggleSwitch {
            id: loginToggle
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            checked: root.restoreOnLogin
            onToggled: {
              if (root.mockMode) return
              root.setRestoreOnLogin(loginToggle.checked)
            }

            PanelToolTip {
              visible: loginToggle.containsMouse
              fontFamily: Style.font.family
              text: root.restoreOnLogin
                    ? "Reboot now and these windows come back"
                    : "Reboot now and nothing reopens"
            }
          }
        }

        // One scroll surface contains both workflows. It keeps the compact
        // header and the snapshot cadence reachable on short screens while
        // the app and restart sections stay in the order shown in the mockup.
        Flickable {
          id: bodyScroll
          width: parent.width
          readonly property real surface: panel.availableCardHeight > 0
                                         ? Math.min(panel.availableCardHeight, panel.maxHeight)
                                         : panel.maxHeight
          readonly property real budget: surface
                                        - panel.verticalContentInset
                                        - heroBlock.height
                                        - footerBlock.implicitHeight
                                        - column.spacing * 2
          height: Math.max(0, Math.min(bodyContent.implicitHeight, budget))
          contentWidth: width
          contentHeight: bodyContent.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Column {
            id: bodyContent
            width: bodyScroll.width
            spacing: Style.spacing.lg

            // ── status and snapshot actions ────────────────────────────────
            Rectangle {
              visible: !root.mockMode && root.cliMissing
              width: parent.width
              height: missingText.implicitHeight + Style.space(12)
              radius: Style.space(3)
              color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)
              Text {
                textFormat: Text.PlainText
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
                textFormat: Text.PlainText
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

            Rectangle {
              visible: root.mockMode || !root.cliMissing
              width: parent.width
              height: summaryCol.implicitHeight + Style.space(18)
              radius: Style.space(4)
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.05)
              border.width: 1
              border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10)

              Column {
                id: summaryCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.margins: Style.space(9)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.md

                Text {
                  textFormat: Text.PlainText
                  text: root.comingBack === root.windowCount
                        ? "All " + root.windowCount + " come back"
                        : root.comingBack + " of " + root.windowCount + " come back"
                  color: root.unresolvable > 0 ? Color.urgent : root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.subtitle
                }

                Item {
                  id: ruler
                  width: parent.width
                  height: Style.space(4)
                  visible: root.captured.length > 0
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
                  textFormat: Text.PlainText
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
                    focusable: true
                    enabled: !root.mockMode && !saveProc.running
                             && !browserRestoreProc.running
                    onClicked: saveProc.running = true
                  }
                  Button {
                    text: "Restore session"
                    bordered: true
                    focusable: true
                    enabled: !root.mockMode && !restoreProc.running
                             && !browserRestoreProc.running && !restoreNamedProc.running
                             && !ghosttyRestoreProc.running
                    onClicked: restoreProc.running = true
                  }
                }
              }
            }

            // The plugin's service sets up the timer and the login restore;
            // until it has, or after `omasession uninstall`, nothing is saved
            // in the background, which the panel would otherwise not show.
            Rectangle {
              visible: root.automationOff !== ""
              width: parent.width
              height: automationText.implicitHeight + Style.space(12)
              radius: Style.space(3)
              color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10)
              Text {
                textFormat: Text.PlainText
                id: automationText
                anchors.left: parent.left; anchors.right: parent.right
                anchors.margins: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                wrapMode: Text.WordWrap
                color: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                text: root.automationOff
              }
            }

            Rectangle {
              visible: root.guardRefused
              width: parent.width
              height: refusedText.implicitHeight + Style.space(12)
              radius: Style.space(3)
              color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.14)
              Text {
                textFormat: Text.PlainText
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

            // ── app restoration ────────────────────────────────────────────
            Rectangle {
              visible: root.mockMode || !root.cliMissing
              width: parent.width
              height: appsCol.implicitHeight + Style.space(18)
              radius: Style.space(4)
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.035)
              border.width: 1
              border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)

              Column {
                id: appsCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.margins: Style.space(9)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.sm

                Text {
                  textFormat: Text.PlainText
                  text: "Restore App Windows"
                  color: root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }

                // Each app owns its action and setting. Availability comes
                // from the CLI's system detection; a closed app stays
                // selectable, while an app that is not installed is visibly
                // disabled instead of silently disappearing.
                Row {
                  id: appCards
                  width: parent.width
                  spacing: Style.spacing.sm

                  Item {
                    id: chromeControls
                    width: (appCards.width - appCards.spacing) / 2
                    height: Style.space(54)

                    BorderSurface {
                      anchors.fill: parent
                      color: "transparent"
                      borderSpec: Border.controlSpec("normal", root.fg, Color.accent)
                      radius: Style.cornerRadius
                    }

                    Button {
                      id: chromeRestoreButton
                      anchors.left: chromeToggle.right
                      anchors.right: parent.right
                      anchors.rightMargin: Style.spacing.sm
                      anchors.leftMargin: Style.spacing.xs
                      anchors.verticalCenter: parent.verticalCenter
                      text: "Google Chrome"
                      iconText: ""
                      tooltipText: !root.chromeInstalled
                                    ? "Google Chrome is not installed"
                                    : !root.chromeCaptured
                                    ? "Google Chrome is not in the saved snapshot"
                                    : browserRestoreProc.running
                                    ? "Restoring Chrome windows…"
                                    : "Restore Chrome windows and saved tabs"
                      bordered: false
                      focusable: true
                      enabled: !root.mockMode && !root.cliMissing
                               && root.chromeInstalled && root.chromeCaptured
                               && !browserRestoreProc.running && !ghosttyRestoreProc.running
                               && !restoreProc.running && !saveProc.running
                               && !restoreNamedProc.running
                      onClicked: {
                        root.browserRestoreOutput = ""
                        root.browserRestoreError = ""
                        root.ghosttyRestoreOutput = ""
                        root.ghosttyRestoreError = ""
                        browserRestoreProc.running = true
                      }
                    }

                    ToggleSwitch {
                      id: chromeToggle
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.sm
                      anchors.verticalCenter: parent.verticalCenter
                      checked: root.chromeInstalled && root.browserRepair && root.browserRepairChrome
                      busy: browserRepairProc.running
                      enabled: !root.mockMode && !root.cliMissing
                               && root.chromeInstalled && root.browserRepair
                               && !browserRepairProc.running
                      foreground: root.fg
                      cursorRing: false
                      onToggled: {
                        if (root.mockMode) return
                        var next = !root.browserRepairChrome
                        root.browserRepairChromeOverride = next
                        root.setBrowserRepairChrome(next)
                      }

                      PanelToolTip {
                        visible: chromeToggle.containsMouse
                        fontFamily: Style.font.family
                        text: !root.chromeInstalled
                              ? "Google Chrome is not installed"
                              : !root.browserRepair
                              ? "Automatic browser restoration is disabled globally"
                              : root.browserRepairChrome
                              ? "Automatically restore Chrome windows after a browser restart"
                              : "Automatic Chrome window restoration is off"
                      }
                    }
                  }

                  Item {
                    id: ghosttyControls
                    width: (appCards.width - appCards.spacing) / 2
                    height: Style.space(54)

                    BorderSurface {
                      anchors.fill: parent
                      color: "transparent"
                      borderSpec: Border.controlSpec("normal", root.fg, Color.accent)
                      radius: Style.cornerRadius
                    }

                    Button {
                      id: ghosttyRestoreButton
                      anchors.left: ghosttyToggle.right
                      anchors.right: parent.right
                      anchors.rightMargin: Style.spacing.sm
                      anchors.leftMargin: Style.spacing.xs
                      anchors.verticalCenter: parent.verticalCenter
                      text: "Ghostty"
                      iconText: ""
                      tooltipText: !root.ghosttyInstalled
                                    ? "Ghostty is not installed"
                                    : !root.ghosttyCaptured
                                    ? "Ghostty is not in the saved snapshot"
                                    : ghosttyRestoreProc.running
                                    ? "Restoring Ghostty windows…"
                                    : "Restore Ghostty windows to saved workspaces"
                      bordered: false
                      focusable: true
                      enabled: !root.mockMode && !root.cliMissing
                               && root.ghosttyInstalled && root.ghosttyCaptured
                               && !ghosttyRestoreProc.running && !browserRestoreProc.running
                               && !restoreProc.running && !saveProc.running
                               && !restoreNamedProc.running
                      onClicked: {
                        root.browserRestoreOutput = ""
                        root.browserRestoreError = ""
                        root.ghosttyRestoreOutput = ""
                        root.ghosttyRestoreError = ""
                        ghosttyRestoreProc.running = true
                      }
                    }

                    ToggleSwitch {
                      id: ghosttyToggle
                      anchors.left: parent.left
                      anchors.leftMargin: Style.spacing.sm
                      anchors.verticalCenter: parent.verticalCenter
                      checked: root.ghosttyInstalled && root.appRestoreGhostty
                      busy: ghosttyRestoreConfigProc.running
                      enabled: !root.mockMode && !root.cliMissing
                               && root.ghosttyInstalled && !ghosttyRestoreConfigProc.running
                      foreground: root.fg
                      cursorRing: false
                      onToggled: {
                        if (root.mockMode) return
                        var next = !root.appRestoreGhostty
                        root.appRestoreGhosttyOverride = next
                        root.setAppRestoreGhostty(next)
                      }

                      PanelToolTip {
                        visible: ghosttyToggle.containsMouse
                        fontFamily: Style.font.family
                        text: !root.ghosttyInstalled
                              ? "Ghostty is not installed"
                              : root.appRestoreGhostty
                              ? "Restore Ghostty on the next login or session restore"
                              : "Automatic Ghostty restoration is off"
                      }
                    }
                  }
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  wrapMode: Text.WordWrap
                  text: (!root.chromeInstalled ? "Chrome unavailable"
                        : root.chromeCaptured ? "Chrome saved"
                        : "Chrome not in snapshot")
                        + "  ·  "
                        + (!root.ghosttyInstalled ? "Ghostty unavailable"
                           : root.ghosttyCaptured ? "Ghostty saved"
                           : "Ghostty not in snapshot")
                  color: root.dim
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  visible: root.browserRestoreOutput !== "" || root.browserRestoreError !== ""
                            || root.ghosttyRestoreOutput !== "" || root.ghosttyRestoreError !== ""
                  wrapMode: Text.WordWrap
                  text: root.browserRestoreError !== "" ? root.browserRestoreError
                        : root.ghosttyRestoreError !== "" ? root.ghosttyRestoreError
                        : root.browserRestoreOutput !== "" ? root.browserRestoreOutput
                        : root.ghosttyRestoreOutput
                  color: (root.browserRestoreError !== "" || root.ghosttyRestoreError !== "")
                         ? Color.urgent : root.dim
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }
            }

            // ── restart restoration ────────────────────────────────────────
            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Restore Windows on Restart"
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            ButtonGroup {
              id: viewTabs
              visible: root.mockMode || !root.cliMissing
              options: [
                { value: "windows", label: "Windows" },
                { value: "layout", label: "Layout" },
                { value: "saved", label: "Saved" }
              ]
              value: root.view
              foreground: root.fg
              fontSize: Style.font.caption
              onChanged: function(v) { root.view = v }
            }

            // ── Layout: the screens to scale, one workspace at a time ─────────
            Column {
              id: layoutView
              visible: root.view === "layout"
              width: bodyScroll.width
              spacing: Style.spacing.sm

              Text {
                textFormat: Text.PlainText
                width: parent.width
                elide: Text.ElideRight
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                text: root.layoutData === null
                      ? (layoutProc.running ? "Loading…" : "Nothing saved to draw yet")
                      : (root.layoutName === "last" ? "Current save" : root.sessionTitle(root.layoutName))
                        + (root.layoutName !== "last" ? "  ·  Back to current" : "")
                MouseArea {
                  anchors.fill: parent
                  enabled: root.layoutName !== "last"
                  cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                  onClicked: root.loadLayout("last")
                }
              }

              Flow {
                width: parent.width
                spacing: Style.spacing.xs
                visible: root.layoutData !== null
                Repeater {
                  model: root.layoutData ? root.layoutData.workspaces : []
                  Button {
                    text: root.workspaceLabel(modelData)
                    bordered: true
                    focusable: true
                    selected: root.layoutWorkspace === modelData
                    onClicked: root.layoutWorkspace = modelData
                  }
                }
              }

              // The screens. Every monitor box is in Hyprland's layout space
              // (logical pixels, already turned for rotation), so one scale
              // factor draws an ultrawide, a row of monitors or a portrait
              // screen in true proportion.
              Item {
                id: screenMap
                visible: root.layoutData !== null
                width: parent.width
                readonly property var mons: root.layoutData ? root.layoutData.monitors : []
                readonly property real minX: mons.length ? Math.min.apply(null, mons.map(function(m) { return m.box.x })) : 0
                readonly property real minY: mons.length ? Math.min.apply(null, mons.map(function(m) { return m.box.y })) : 0
                readonly property real spanW: mons.length ? Math.max.apply(null, mons.map(function(m) { return m.box.x + m.box.w })) - minX : 1
                readonly property real spanH: mons.length ? Math.max.apply(null, mons.map(function(m) { return m.box.y + m.box.h })) - minY : 1
                readonly property real scale: Math.min(width / Math.max(spanW, 1), Style.space(260) / Math.max(spanH, 1))
                height: spanH * scale

                Repeater {
                  model: screenMap.mons
                  Rectangle {
                    x: (modelData.box.x - screenMap.minX) * screenMap.scale
                    y: (modelData.box.y - screenMap.minY) * screenMap.scale
                    width: modelData.box.w * screenMap.scale
                    height: modelData.box.h * screenMap.scale
                    radius: Style.space(3)
                    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.03)
                    border.width: 1
                    border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.25)
                    Text {
                      textFormat: Text.PlainText
                      anchors.right: parent.right
                      anchors.bottom: parent.bottom
                      anchors.margins: Style.space(3)
                      text: (modelData.name || "") + (modelData.transform % 2 === 1 ? " · rotated" : "")
                      color: root.dim
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                Repeater {
                  model: root.layoutData
                         ? root.layoutData.windows.filter(function(w) { return w.workspace === root.layoutWorkspace })
                         : []
                  Rectangle {
                    x: (modelData.box.x - screenMap.minX) * screenMap.scale
                    y: (modelData.box.y - screenMap.minY) * screenMap.scale
                    width: Math.max(modelData.box.w * screenMap.scale, 4)
                    height: Math.max(modelData.box.h * screenMap.scale, 4)
                    radius: Style.space(2)
                    color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, modelData.floating ? 0.10 : 0.18)
                    border.width: modelData.floating ? 2 : 1
                    border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.8)
                    clip: true
                    Column {
                      anchors.fill: parent
                      anchors.margins: Style.space(4)
                      spacing: 1
                      Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        elide: Text.ElideRight
                        text: modelData.label + (modelData.floating ? " (floating)" : "")
                        color: root.fg
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                      }
                      Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        elide: Text.ElideRight
                        text: modelData.title
                        color: root.dim
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                      }
                    }
                  }
                }
              }
            }

            // ── Saved: named layouts, history, save as ───────────────────────
            Column {
              id: savedView
              visible: root.view === "saved"
              width: bodyScroll.width
              spacing: Style.spacing.sm

              Row {
                width: parent.width
                spacing: Style.spacing.sm
                TextField {
                  id: saveAsField
                  width: parent.width - saveAsButton.width - parent.spacing
                  placeholderText: "Name this layout (e.g. work)"
                  foreground: root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  onAccepted: if (saveAsButton.enabled) saveAsButton.clicked()
                }
                Button {
                  id: saveAsButton
                  text: "Save as"
                  bordered: true
                  focusable: true
                  enabled: !root.mockMode && !saveAsProc.running
                           && !restoreProc.running && !restoreNamedProc.running
                           && /^[A-Za-z0-9][A-Za-z0-9 _-]{0,40}$/.test(saveAsField.text.trim())
                  onClicked: {
                    saveAsProc.name = saveAsField.text.trim()
                    root.savedMessage = ""
                    saveAsProc.running = true
                    saveAsField.text = ""
                  }
                }
              }

              Text {
                textFormat: Text.PlainText
                visible: text !== ""
                width: parent.width
                wrapMode: Text.WordWrap
                text: root.savedMessage
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              Repeater {
                model: [
                  { title: "Saved layouts", rows: root.sessions ? root.sessions.named : [], empty: "None yet. Name the current windows above." },
                  { title: "History", rows: root.sessions ? root.sessions.history : [], empty: listProc.running ? "Loading…" : "Nothing yet." }
                ]
                Column {
                  width: savedView.width
                  spacing: Style.spacing.xs
                  Text {
                    textFormat: Text.PlainText
                    text: modelData.title
                    color: Color.accent
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    textFormat: Text.PlainText
                    visible: modelData.rows.length === 0
                    text: modelData.empty
                    color: root.dim
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                  Repeater {
                    model: modelData.rows
                    Rectangle {
                      width: savedView.width
                      height: Math.max(rowText.implicitHeight, previewButton.implicitHeight) + Style.space(10)
                      radius: Style.space(3)
                      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.04)
                      Column {
                        id: rowText
                        anchors.left: parent.left
                        anchors.right: previewButton.left
                        anchors.leftMargin: Style.space(8)
                        anchors.rightMargin: Style.spacing.sm
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                          textFormat: Text.PlainText
                          width: parent.width
                          elide: Text.ElideRight
                          text: root.sessionTitle(modelData.name)
                          color: root.fg
                          font.family: Style.font.family
                          font.pixelSize: Style.font.body
                        }
                        Text {
                          textFormat: Text.PlainText
                          width: parent.width
                          elide: Text.ElideRight
                          text: modelData.windows + " windows · " + root.agoLabel(modelData.agoSec)
                          color: root.dim
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption
                        }
                      }
                      Button {
                        id: previewButton
                        anchors.right: restoreButton.left
                        anchors.rightMargin: Style.spacing.xs
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Preview"
                        bordered: true
                        focusable: true
                        onClicked: root.previewSession(modelData.name)
                      }
                      Button {
                        id: restoreButton
                        anchors.right: parent.right
                        anchors.rightMargin: Style.space(6)
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Restore"
                        bordered: true
                        focusable: true
                        enabled: !root.mockMode && !restoreNamedProc.running && !restoreProc.running
                                 && !browserRestoreProc.running && !ghosttyRestoreProc.running
                        onClicked: {
                          restoreNamedProc.name = modelData.name
                          root.savedMessage = "Restoring \"" + modelData.name + "\"…"
                          restoreNamedProc.running = true
                        }
                      }
                    }
                  }
                }
              }
            }

            Column {
              id: monitorList
              visible: root.view === "windows"
              width: bodyScroll.width
              spacing: Style.spacing.lg

              Repeater {
                model: (root.mockMode || !root.cliMissing) ? root.byMonitor : []

                Column {
                  width: monitorList.width
                  spacing: Style.spacing.sm

                  Text {
                    textFormat: Text.PlainText
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
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: modelData.app
                            color: root.fg
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                          }
                          Text {
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: "ws" + modelData.ws + " · " + (modelData.detail || modelData.title)
                            color: root.dim
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                          }
                          Text {
                            textFormat: Text.PlainText
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
            }
          }
        }

        Column {
          id: footerBlock
          width: parent.width
          spacing: column.spacing

          PanelSeparator { width: parent.width; visible: root.mockMode || !root.cliMissing }

          Text {
            textFormat: Text.PlainText
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
}
