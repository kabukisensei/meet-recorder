import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "jennings.meetrecorder"

  readonly property string cliPath: Quickshell.env("HOME") + "/.local/bin/meet-recorder"

  property bool recording: false
  property string elapsed: "--:--"

  function refresh() {
    if (probe.running) return
    probe.running = true
  }

  Process {
    id: probe
    running: false
    command: ["sh", "-c",
      "f=\"$HOME/.local/state/meet-recorder/session.json\"; " +
      "[ -f \"$f\" ] || exit 1; " +
      "start=$(sed -n \"s/.*\\\"start\\\": \\([0-9]*\\).*/\\1/p\" \"$f\"); " +
      "[ -n \"$start\" ] || exit 1; " +
      "e=$(( $(date +%s) - start )); " +
      "[ \"$e\" -ge 0 ] 2>/dev/null || exit 1; " +
      "printf \"%02d:%02d\" $(( e / 60 )) $(( e % 60 ))"]
    onExited: function(code) {
      if (code === 0) {
        var out = probe.stdout.trim()
        if (out.length > 0) {
          root.recording = true
          root.elapsed = out
          return
        }
      }
      root.recording = false
      root.elapsed = "--:--"
    }
  }

  Process {
    id: stopper
    running: false
    command: [root.cliPath, "stop"]
  }

  Timer {
    interval: 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  implicitWidth: root.recording ? button.implicitWidth : 0
  implicitHeight: root.recording ? button.implicitHeight : 0

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "● REC " + root.elapsed
    foreground: Commons.Color.urgent
    useActiveColor: false
    fontSize: Style.bar.iconFont
    fixedWidth: -1
    tooltipText: "Meeting recorder — click to stop"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton && !stopper.running) stopper.running = true
    }
  }
}
