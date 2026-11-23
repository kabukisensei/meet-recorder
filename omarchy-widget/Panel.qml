import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Meeting recorder: an always-visible mic icon in the bar.
//   dim mic            idle
//   red dot + MM:SS    recording
//   accent mic + "…"   transcribing the last recording
// Left click opens the panel (start/stop, auto-record toggle, open files).
// Middle or right click toggles recording directly.
Panel {
  id: root
  moduleName: "jennings.meetrecorder"
  ipcTarget: "meetrecorder"

  readonly property string cliPath: Quickshell.env("HOME") + "/.local/bin/meet-recorder"

  property string phase: "idle"        // idle | recording | transcribing
  property int elapsed: 0
  property string source: ""
  property string title: ""
  property bool autoOn: true
  property bool watching: false
  property bool inCall: false
  property string lastFolder: ""
  property string lastStatus: ""
  property string outputDir: Quickshell.env("HOME") + "/Recordings"
  property bool busy: false

  readonly property bool recording: phase === "recording"
  readonly property bool transcribing: phase === "transcribing"
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function clock(seconds) {
    var m = Math.floor(seconds / 60), s = seconds % 60
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
  }

  readonly property string stateLine: recording
    ? "Recording " + clock(elapsed) + (source === "teams" ? " · Teams call" : " · started by you")
    : transcribing ? "Saved. Writing the transcript…"
    : inCall ? "Teams call detected"
    : "Not recording"

  readonly property string autoLine: !autoOn ? "Off. Record with the button or Super+Shift+R."
    : watching ? "On. Starts when a Teams call begins, stops when it ends."
    : "On, but the background service is not running."

  readonly property string lastLine: lastFolder === "" ? "No recordings yet"
    : lastFolder.replace(Quickshell.env("HOME"), "~")
      + (lastStatus === "transcribing" ? "  (transcribing…)"
        : lastStatus === "no-speech" ? "  (no speech found)"
        : lastStatus === "failed" ? "  (transcription failed)" : "")

  function refresh() { if (!probe.running) probe.running = true }

  function act(args) {
    if (actor.running) return
    root.busy = true
    actor.command = [root.cliPath].concat(args)
    actor.running = true
  }

  function toggleRecording() { act([recording ? "stop" : "start"]) }

  function openPath(path) {
    if (path === "") return
    opener.command = ["xdg-open", path]
    opener.running = true
    root.close()
  }

  Process {
    id: probe
    running: false
    command: [root.cliPath, "status", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var s = JSON.parse(text)
          root.phase = String(s.state || "idle")
          root.elapsed = Number(s.elapsed) || 0
          root.source = String(s.source || "")
          root.title = String(s.title || "")
          root.autoOn = s.auto === true
          root.watching = s.watching === true
          root.inCall = s.in_call === true
          root.lastFolder = String(s.last_folder || "")
          root.lastStatus = String(s.last_status || "")
          if (s.output_dir) root.outputDir = String(s.output_dir)
        } catch (error) {
          root.phase = "idle"
        }
      }
    }
  }

  Process {
    id: actor
    running: false
    onExited: function(code) { root.busy = false; root.refresh() }
  }

  Process { id: opener; running: false }

  Timer {
    interval: root.recording || root.opened ? 1000 : 3000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  onOpenedChanged: if (opened) refresh()

  IpcHandler {
    target: "meetrecorder-control"
    function record(): void { root.toggleRecording() }
    function status(): string { return root.phase }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // A plain WidgetButton sizes itself to its text, so the slot widens for
  // the timer and shrinks back to one icon when idle.
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.recording ? "\u{f044a} " + root.clock(root.elapsed)
      : root.transcribing ? "\u{f036c} …" : "\u{f036c}"
    fontSize: Style.bar.iconFont
    fixedWidth: -1
    foreground: root.recording ? root.urgent
      : root.transcribing ? Commons.Color.accent
      : Qt.darker(root.barForeground, 1.55)
    useActiveColor: false
    tooltipText: "Meeting recorder: " + root.stateLine
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton) root.toggle()
      else root.toggleRecording()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onActivateRequested: root.toggleRecording()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var key = String(t).toLowerCase()
        if (key === "r" || key === "s") root.toggleRecording()
        else if (key === "a") root.act(["auto", "toggle"])
        else if (key === "o") root.openPath(root.outputDir)
        else if (key === "l") root.openPath(root.lastFolder)
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: "Meeting recorder"
          meta: root.stateLine
          detail: root.recording && root.title !== "" ? root.title : ""
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            Text {
              textFormat: Text.PlainText
              text: root.recording ? "\u{f044a}" : "\u{f036c}"
              color: root.recording ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
            }
          }
        }

        Button {
          width: parent.width
          bordered: true
          enabled: !root.busy
          iconText: root.recording ? "\u{f04db}" : "\u{f044a}"
          text: root.busy ? "Working…" : (root.recording ? "Stop recording  (R)" : "Start recording  (R)")
          foreground: root.recording ? root.urgent : root.foreground
          fontFamily: root.fontFamily
          onClicked: root.toggleRecording()
        }

        Toggle {
          width: parent.width
          label: "Auto-record Teams calls  (A)"
          description: root.autoLine
          checked: root.autoOn
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.act(["auto", "toggle"])
        }

        PanelSectionHeader {
          text: "RECORDINGS"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: "Last: " + root.lastLine
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WrapAnywhere
        }

        Button {
          width: parent.width
          leftAlign: true
          enabled: root.lastFolder !== ""
          iconText: "\u{f0219}"
          text: "Open last recording  (L)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openPath(root.lastFolder)
        }

        Button {
          width: parent.width
          leftAlign: true
          iconText: "\u{f024b}"
          text: "Open all recordings  (O)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openPath(root.outputDir)
        }
      }
    }
  }
}
