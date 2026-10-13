import QtQuick
import qs.Ui
import qs.Commons as Theme

// Setup checklist. Buttons open a fixed Muse page, copy a command, or open
// Omarchy's installer terminal running a fixed script from companion/. There
// is no token field here; the token is only ever typed into that terminal.
Column {
  id: root
  property var service: null
  property color foreground: Theme.Color.foreground
  readonly property var setup: service ? service.setup : ({ steps: [] })
  readonly property var steps: setup.steps || []
  readonly property bool complete: setup.complete === true
  signal terminalRequested()

  spacing: Theme.Style.space(14)

  function openMuse(url) {
    if (String(url).startsWith("https://gadgets.muse.ai/")) Qt.openUrlExternally(url)
  }

  Text {
    width: parent.width
    text: root.complete ? "You're all set. Chat with Muse from the Chat tab."
      : "Omuseful lets you chat with Muse from the Omarchy bar. Muse can't run anything on this computer."
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.complete ? Theme.Color.accent : root.foreground
    font.family: Theme.Style.font.family
    font.pixelSize: Theme.Style.font.bodySmall
  }

  Repeater {
    model: root.steps
    delegate: Column {
      id: stepItem
      required property var modelData
      required property int index
      readonly property var step: modelData
      width: root.width
      spacing: Theme.Style.space(6)

      Text {
        width: parent.width
        text: (stepItem.step.done ? "✓ " : (stepItem.index + 1) + ". ") + stepItem.step.title
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: stepItem.step.done ? Theme.Color.accent : root.foreground
        font.family: Theme.Style.font.family
        font.pixelSize: Theme.Style.font.body
        font.bold: true
      }
      Text {
        visible: !stepItem.step.done || stepItem.step.id === "pair"
        width: parent.width
        text: stepItem.step.body || ""
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.foreground
        opacity: 0.75
        font.family: Theme.Style.font.family
        font.pixelSize: Theme.Style.font.bodySmall
      }
      Flow {
        visible: !stepItem.step.done
        width: parent.width
        spacing: Theme.Style.space(6)
        // In step order: get the token, then run, then the copy fallback.
        Button {
          visible: !!stepItem.step.url
          text: stepItem.step.url_label || "Open page"
          foreground: root.foreground
          bordered: true
          focusable: true
          onClicked: root.openMuse(stepItem.step.url)
        }
        Button {
          visible: !!stepItem.step.run
          text: stepItem.step.run_label || "Run"
          foreground: root.foreground
          background: Theme.Util.alpha(Theme.Color.accent, 0.25)
          bordered: true
          focusable: true
          enabled: root.service !== null
          onClicked: {
            root.terminalRequested()
            root.service.runInTerminal(stepItem.step.run)
          }
        }
        Button {
          visible: !!stepItem.step.command
          text: stepItem.step.command_label || "Copy command"
          foreground: root.foreground
          focusable: true
          enabled: root.service !== null && stepItem.step.command_enabled === true
          opacity: enabled ? 0.8 : 0.4
          onClicked: root.service.copyCommand(stepItem.step.command)
        }
        Button {
          visible: !!stepItem.step.secondary_url
          text: stepItem.step.secondary_url_label || "Open page"
          foreground: root.foreground
          focusable: true
          opacity: 0.8
          onClicked: root.openMuse(stepItem.step.secondary_url)
        }
      }
      Text {
        visible: !stepItem.step.done && !!stepItem.step.disabled_hint
        width: parent.width
        text: stepItem.step.disabled_hint || ""
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.foreground
        opacity: 0.6
        font.family: Theme.Style.font.family
        font.pixelSize: Theme.Style.font.caption
      }
    }
  }

  Text {
    visible: !!root.service && root.service.message !== ""
    width: parent.width
    text: root.service ? root.service.message : ""
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.foreground
    font.family: Theme.Style.font.family
    font.pixelSize: Theme.Style.font.caption
  }
}
