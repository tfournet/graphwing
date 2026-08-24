import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "graphwing.watch"
  ipcTarget: "graphwing.watch"
  manageIpc: false

  property int jobIndex: 0
  property string focusSection: "header"
  property bool cursorActive: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var unitRows: Model.unitRows(watch.units)
  readonly property var jobs: watch.activeJobs.concat(watch.recentJobs)
  readonly property bool headerHasCursor: cursorActive && focusSection === "header"
  readonly property color barIconColor: {
    if (!watch.online || !watch.apiActive) return urgent
    if (watch.busy) return foreground
    return dim
  }
  readonly property string heroMeta: watch.statusText
  readonly property string glyph: "󰘬"

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }

  function ensureCursor() {
    if (jobs.length === 0) {
      focusSection = "header"
      jobIndex = 0
      return
    }
    if (jobIndex >= jobs.length) jobIndex = jobs.length - 1
    if (jobIndex < 0) jobIndex = 0
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy === 0) return
    if (focusSection === "header") {
      if (dy > 0 && jobs.length > 0) {
        focusSection = "jobs"
        jobIndex = 0
        scrollCursorIntoView()
      }
      return
    }
    if (focusSection === "jobs") {
      if (dy < 0 && jobIndex === 0) {
        focusSection = "header"
        if (panelFlick) panelFlick.contentY = 0
        return
      }
      jobIndex = clamp(jobIndex + dy, 0, jobs.length - 1)
      scrollCursorIntoView()
    }
  }

  function activateCursor() {
    if (focusSection === "header") watch.openHerdr()
    else if (focusSection === "jobs" && jobs.length > 0) watch.openJob(jobs[jobIndex])
  }

  function setJobCursor(index) {
    cursorActive = true
    focusSection = "jobs"
    jobIndex = index
    scrollCursorIntoView()
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "jobs" && jobColumn && jobIndex >= 0 && jobIndex < jobColumn.children.length)
      scrollItemIntoView(jobColumn.children[jobIndex])
  }

  function jobColor(status) {
    if (status === "failed") return urgent
    if (status === "running" || status === "queued") return foreground
    return dim
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    watch.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: watch
    settings: root.settings
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { watch.refresh(); return "ok" }
    function status(): string { return watch.statusText }
    function journal(): string { watch.openJournal(); return "ok" }
    function herdr(): string { watch.openHerdr(); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    active: watch.alarming || watch.busy
    dimmed: !watch.online
    tooltipText: watch.statusText
    iconComponent: Component {
      Item {
        Text {
          anchors.centerIn: parent
          text: root.glyph
          color: root.barIconColor
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
        }
        Text {
          visible: watch.activeCount > 0
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          text: String(watch.activeCount)
          color: root.barIconColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) watch.refresh()
      else if (buttonCode === Qt.MiddleButton) watch.openHerdr()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") watch.refresh()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.headerHasCursor
            function focusHero() {
              root.cursorActive = true
              root.focusSection = "header"
            }

            PanelHero {
              id: hero
              width: parent.width
              title: "Graphwing"
              meta: root.heroMeta
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: watch.online ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  text: root.glyph
                  color: root.barIconColor
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
              trailingControl: Component {
                PanelActionButton {
                  iconText: "󰑐"
                  tooltipText: "Refresh"
                  foreground: hero.foreground
                  fontFamily: hero.fontFamily
                  hasCursor: header.ringVisible
                  onHovered: function(on) { if (on) header.focusHero() }
                  onClicked: watch.refresh()
                }
              }
            }
          }

          Text {
            visible: watch.lastError !== ""
            width: parent.width
            text: watch.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: "UNITS"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.unitRows
              UnitRow {
                required property var modelData
                width: column.width
                unit: modelData
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: watch.busy ? "JOBS" : "RECENT"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: root.jobs.length === 0
              width: parent.width
              text: watch.online ? "No jobs yet." : "Start graphwing-api, then this fills in."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: jobColumn
              visible: root.jobs.length > 0
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.jobs
                JobRow {
                  required property var modelData
                  required property int index
                  width: jobColumn.width
                  job: modelData
                  rowIndex: index
                }
              }
            }
          }
        }
      }
    }
  }

  component UnitRow: Item {
    id: unitRow
    property var unit: null
    implicitHeight: Math.max(unitName.implicitHeight, unitState.implicitHeight)

    Text {
      id: unitName
      text: unitRow.unit ? unitRow.unit.name : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: unitState
      text: unitRow.unit ? unitRow.unit.state : ""
      color: unitRow.unit && unitRow.unit.active ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
    }
  }

  component JobRow: CursorSurface {
    id: jobRow
    property var job: null
    property int rowIndex: 0
    readonly property string status: job ? String(job.status || "") : ""

    hasCursor: root.cursorActive && root.focusSection === "jobs" && root.jobIndex === rowIndex
    foreground: root.foreground

    implicitHeight: jobContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setJobCursor(jobRow.rowIndex)
      onClicked: watch.openJob(jobRow.job)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)

      ColumnLayout {
        id: jobContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          text: jobRow.job ? jobRow.job.title : ""
          color: root.jobColor(jobRow.status)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          text: {
            if (!jobRow.job) return ""
            var bits = [jobRow.job.kind, jobRow.status]
            if (jobRow.job.repo) bits.push(jobRow.job.repo)
            return bits.join(" · ")
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
  }
}
