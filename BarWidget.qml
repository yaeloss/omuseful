import QtQuick
import Quickshell
import qs.Ui
import qs.Commons as Theme

BarWidget {
  id: root
  moduleName: "omuseful"
  property var shell: null
  readonly property var service: root.bar && root.bar.shell
    ? root.bar.shell.serviceFor(root.moduleName)
    : (root.shell ? root.shell.serviceFor(root.moduleName) : null)
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }
  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.service = root.service
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onServiceChanged: injectPanel()

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    fixedWidth: mark.width + 2 * scaledHorizontalMargin
    foreground: root.service && root.service.connected ? Theme.Color.accent
      : (root.bar ? root.bar.barForeground : Theme.Color.foreground)
    dimmed: !root.service || !root.service.connected
    tooltipText: "Omuseful · " + (root.service ? root.service.statusLabel : "Starting…")
    onPressed: root.toggle()

    OmusefulMark {
      id: mark
      anchors.centerIn: parent
      width: Math.min(Theme.Style.space(24), Math.round(button.barSize * 0.8))
      height: width
      foreground: button.foreground
    }

    Rectangle {
      visible: root.service && root.service.connected
      anchors.right: mark.right
      anchors.bottom: mark.bottom
      width: Theme.Style.space(4)
      height: width
      radius: width / 2
      color: Theme.Color.accent
    }
  }

  Loader {
    id: panelLoader
    active: true
    visible: false
    source: Qt.resolvedUrl("Panel.qml")
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }
}
