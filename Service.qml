import QtQuick
import Quickshell

// OmaSession's setup, run by the shell itself.
//
// Omarchy runs no hook of a plugin on add, enable, disable or remove, so a
// standard `omarchy plugin add --enable` would leave the snapshot timer and the
// login restore unset. The shell creates this service once while the plugin is
// enabled (not once per monitor, as it does the bar widget), and it asks the
// CLI to converge the setup. `install --if-needed` changes nothing that is
// already right, leaves out a login line the user deleted, and does nothing
// after `omasession uninstall`.
//
// Detached on purpose: the shell SIGKILLs a child Process when it restarts,
// and the setup must not die halfway through a write. Disable and remove need
// nothing here: the timer and the login line ask the CLI, which checks that
// the plugin is still enabled before saving or restoring.
Item {
  id: root

  property var manifest: null
  property var shell: null
  property string omarchyPath: ""

  readonly property string cliPath:
    Quickshell.env("HOME") + "/.config/omarchy/plugins/brenoperucchi.omasession/bin/omasession"

  Component.onCompleted: Quickshell.execDetached([root.cliPath, "install", "--if-needed"])
}
