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

  function open() {
    if (!panelLoader.item) return
    // Notification clicks use summon (not toggle), always revealing the chat.
    panelLoader.item.chooseView("chat")
    panelLoader.item.open()
  }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { root.opened ? root.close() : root.open() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }
  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.service = root.service
    panelLoader.item.settings = root.settings
  }

  function syncServiceSettings() {
    if (root.service) root.service.notificationsEnabled = root.setting("notifyReplies", false) === true
  }

  function setReplyNotifications(enabled) {
    var entry = { id: root.moduleName }
    for (var key in root.settings) if (key !== "id") entry[key] = root.settings[key]
    entry.notifyReplies = enabled === true
    root.settings = entry
    var api = root.bar && root.bar.shell ? root.bar.shell : root.shell
    if (api && typeof api.updateEntryInline === "function") api.updateEntryInline(root.moduleName, entry)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onServiceChanged: { injectPanel(); syncServiceSettings() }
  onSettingsChanged: { injectPanel(); syncServiceSettings() }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    fixedWidth: mark.width + 2 * scaledHorizontalMargin
    foreground: root.service && (root.service.connected || root.service.unreadCount > 0) ? Theme.Color.accent
      : (root.bar ? root.bar.barForeground : Theme.Color.foreground)
    dimmed: !root.service || (!root.service.connected && root.service.unreadCount === 0 && !root.service.chatBusy)
    tooltipText: "Omuseful · " + (root.service ? root.service.barStatus : "Starting…")
    onPressed: root.toggle()

    OmusefulMark {
      id: mark
      anchors.centerIn: parent
      width: Math.min(Theme.Style.space(24), Math.round(button.barSize * 0.8))
      height: width
      foreground: button.foreground
    }

    Rectangle {
      visible: root.service && root.service.connected && !root.service.chatBusy
      anchors.right: mark.right
      anchors.bottom: mark.bottom
      width: Theme.Style.space(4)
      height: width
      radius: width / 2
      color: Theme.Color.accent
    }

    Text {
      visible: root.service && root.service.chatBusy
      anchors.right: mark.right
      anchors.bottom: mark.bottom
      text: "…"
      textFormat: Text.PlainText
      color: Theme.Color.accent
      font.family: Theme.Style.font.family
      font.pixelSize: Theme.Style.font.body
      font.bold: true
    }

    Rectangle {
      visible: root.service && root.service.unreadCount > 0
      anchors.right: mark.right
      anchors.rightMargin: -Theme.Style.space(3)
      anchors.top: mark.top
      anchors.topMargin: -Theme.Style.space(3)
      width: Math.max(Theme.Style.space(12), unreadText.implicitWidth + Theme.Style.space(5))
      height: Theme.Style.space(12)
      radius: height / 2
      color: Theme.Color.accent
      Text {
        id: unreadText
        anchors.centerIn: parent
        text: root.service ? (root.service.unreadCount > 9 ? "9+" : String(root.service.unreadCount)) : ""
        textFormat: Text.PlainText
        color: Theme.Color.background
        font.family: Theme.Style.font.family
        font.pixelSize: Theme.Style.font.caption * 0.8
        font.bold: true
      }
    }
  }

  Loader {
    id: panelLoader
    active: true
    visible: false
    source: Qt.resolvedUrl("Panel.qml")
    onLoaded: {
      root.injectPanel()
      root.syncServiceSettings()
      Qt.callLater(root.injectPanel)
    }
  }
}
