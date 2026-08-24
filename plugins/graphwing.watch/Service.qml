import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

Item {
  id: root

  property var settings: ({})

  property bool online: false
  property bool apiActive: false
  property bool unitsHealthy: false
  property var units: ({})
  property var counts: Model.emptyCounts()
  property var activeJobs: []
  property var recentJobs: []
  property string statusText: "Checking…"
  property string lastError: ""
  property string lastCode: ""
  property bool refreshing: false

  readonly property int activeCount: Number(counts.active || 0)
  readonly property int failedRecent: Number(counts.failed_recent || 0)
  readonly property bool busy: activeCount > 0
  readonly property bool alarming: !online || !apiActive || failedRecent > 0

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 5, 2, 120)
  readonly property int port: intSetting("port", 8645, 1, 65535)
  readonly property string homeDir: resolveHome()
  readonly property string helperPath: pluginFile("status.py")

  property string _output: ""
  property string _error: ""

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    if (n < min) n = min
    if (n > max) n = max
    return n
  }

  function expandPath(path) {
    var value = String(path || "").trim()
    var home = Quickshell.env("HOME") || ""
    if (value === "") return ""
    if (value === "~") return home
    if (value.indexOf("~/") === 0) return home + value.substring(1)
    if (value.indexOf("$HOME/") === 0) return home + value.substring(5)
    return value
  }

  function resolveHome() {
    var configured = expandPath(setting("home", ""))
    if (configured !== "") return configured
    var envHome = expandPath(Quickshell.env("GRAPHWING_HOME") || "")
    if (envHome !== "") return envHome
    return (Quickshell.env("HOME") || "") + "/.graphwing"
  }

  function pluginFile(name) {
    var url = String(Qt.resolvedUrl(name))
    if (url.indexOf("file://") === 0) url = url.substring(7)
    return url
  }

  function applySnapshot(raw) {
    var snap = Model.parseWatch(raw)
    online = snap.online
    apiActive = snap.api_active
    unitsHealthy = snap.units_healthy
    units = snap.units
    counts = snap.counts
    activeJobs = snap.active
    recentJobs = snap.recent
    lastError = snap.online ? "" : String(snap.error || "daemon down")
    lastCode = String(snap.code || "")
    statusText = Model.statusLabel(snap)
  }

  function refresh() {
    if (statusProcess.running) return
    if (helperPath === "") return
    _output = ""
    _error = ""
    refreshing = true
    statusProcess.command = ["python3", helperPath, homeDir, String(port)]
    statusProcess.running = true
  }

  function openHerdr() {
    Quickshell.execDetached(["omarchy-launch-tui", "--app-id=org.omarchy.graphwing-herdr", "herdr", "--session", "graphwing"])
  }

  function openJournal() {
    Quickshell.execDetached([
      "omarchy-launch-floating-terminal-with-presentation",
      "journalctl --user -u graphwing-api -f"
    ])
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    running: false
    onExited: {
      root.refreshing = false
      if (root._output.trim() !== "") root.applySnapshot(root._output)
      else {
        var failed = Model.emptySnapshot()
        failed.error = root._error.trim() !== "" ? root._error.trim() : "daemon down"
        failed.code = "unreachable"
        root.applySnapshot(JSON.stringify(failed))
      }
    }

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root._output = text
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root._error = text
    }
  }
}
