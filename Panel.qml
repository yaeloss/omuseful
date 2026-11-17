import QtQuick
import QtQuick.Controls as Controls
import qs.Ui
import qs.Commons as Theme

Panel {
  id: root
  moduleName: "omuseful"
  manageIpc: false
  property Item anchorItem: null
  property var hostWidget: null
  property var service: null
  property var readerService: null
  property string currentView: "chat"
  property string draftNotice: ""
  property var sentHistory: []
  property int historyIndex: -1
  property string historyStash: ""
  property string renamingThreadId: ""
  property bool emojiPickerOpen: false
  readonly property int maxMessageChars: 60000
  readonly property var emojis: Array.from("😀😄😁😂😊🙂🙃😉😎🤔😐😕😮😲😴👍👎👏🙏💪🔥⭐♥🎉👀💡📎✅❌⏰📌")
  readonly property color foreground: Theme.Color.popups.text
  readonly property bool connected: service && service.connected
  readonly property bool sending: service && service.chatBusy
  readonly property var chatMessages: service ? service.chatMessages : []
  readonly property int chatCount: service ? service.chatCount : 0

  function pushHistory(text) {
    var clean = String(text)
    if (!clean.trim()) return
    if (root.sentHistory.length && root.sentHistory[root.sentHistory.length - 1] === clean) {
      root.historyIndex = -1
      return
    }
    root.sentHistory = root.sentHistory.concat(clean).slice(-50)
    root.historyIndex = -1
    root.historyStash = ""
  }

  function recallHistory(delta) {
    if (!root.sentHistory.length) return false
    if (root.historyIndex < 0) {
      if (delta > 0) return true  // Already on the live draft.
      root.historyStash = messageField.text
    }
    var next = root.historyIndex < 0
      ? root.sentHistory.length - 1
      : Math.min(root.sentHistory.length - 1, Math.max(-1, root.historyIndex + delta))
    if (next === root.historyIndex) return true
    root.historyIndex = next
    if (next < 0) {
      messageField.text = root.historyStash
    } else {
      messageField.text = root.sentHistory[next]
    }
    messageField.cursorPosition = messageField.text.length
    return true
  }

  function insertEmoji(emoji) {
    var position = messageField.cursorPosition
    messageField.insert(position, emoji)
    messageField.cursorPosition = position + emoji.length
    messageField.forceActiveFocus()
  }

  function saveDraft() {
    if (root.service) root.service.setThreadDraft(messageField.text)
  }

  function switchToThread(id) {
    if (!root.service) return
    root.saveDraft()
    if (root.service.switchThread(id)) {
      root.draftNotice = ""
      messageField.text = root.service.activeThreadDraft || ""
      root.historyIndex = -1
      Qt.callLater(scrollToLatest)
    } else {
      root.draftNotice = "Stop waiting or let Muse finish before switching chats."
      root.chooseView("chat")
    }
  }

  function startNewThread() {
    if (!root.service) return
    root.saveDraft()
    if (root.service.newThread()) {
      root.draftNotice = ""
      messageField.text = root.service.activeThreadDraft || ""
      root.historyIndex = -1
      root.chooseView("chat")
      Qt.callLater(scrollToLatest)
      messageField.forceActiveFocus()
    } else {
      root.draftNotice = "Stop waiting or let Muse finish before starting a new chat."
      root.chooseView("chat")
    }
  }

  function handleSlashCommand(text) {
    // Returns true if handled. Unknown /commands send normally, so ordinary
    // slash-leading prompts are never hijacked.
    var clean = String(text).trim()
    if (clean.charAt(0) !== "/") return false
    var command = clean.split(/\s+/)[0].toLowerCase()
    if (command === "/new" && clean.length <= 5 && clean.indexOf("\n") < 0) {
      messageField.text = ""
      root.startNewThread()
      return true
    }
    if (command === "/stop" && clean.length <= 5 && clean.indexOf("\n") < 0) {
      if (root.service) root.service.stopChat()
      messageField.text = ""
      return true
    }
    if (command === "/copy" && clean.length <= 5 && clean.indexOf("\n") < 0) {
      messageField.text = ""
      var lastAssistant = ""
      for (var i = root.chatCount - 1; i >= 0; i--) {
        var row = root.chatMessages.get(i)
        if (row.role === "assistant" && row.messageText) { lastAssistant = row.messageText; break }
      }
      if (lastAssistant && root.service) root.service.copyMessage(lastAssistant)
      else root.draftNotice = "There is no Muse reply in this chat to copy yet."
      return true
    }
    if (command === "/help" && clean.length <= 5 && clean.indexOf("\n") < 0) {
      messageField.text = "/new  Start a new chat\n/stop  Stop waiting for Muse\n/copy  Copy Muse's last reply\nHelp: Enter sends, Shift+Enter adds a line, Ctrl+Up/Ctrl+Down recalls messages, Ctrl+K opens chats, Ctrl+E emoji."
      root.draftNotice = ""
      messageField.forceActiveFocus()
      return true
    }
    return false
  }

  function submitMessage() {
    if (!root.service) return
    // Slash commands are local operations (/new, /stop, /copy, /help) and
    // work while busy or offline; only actual sends need a connection.
    if (String(messageField.text).trim().charAt(0) === "/"
        && root.handleSlashCommand(messageField.text)) return
    if (!root.connected || root.service.chatBusy) return
    if (messageField.text.length > root.maxMessageChars) {
      root.draftNotice = "That message is too long. Shorten it and try again."
      return
    }
    if (String(messageField.text).trim() === "") return
    if (root.handleSlashCommand(messageField.text)) return
    var sent = messageField.text
    if (root.service.send(sent)) {
      root.pushHistory(sent)
      root.saveDraft()
      root.draftNotice = ""
      messageField.text = ""
      root.emojiPickerOpen = false
      Qt.callLater(scrollToLatest)
    }
  }

  function restoreMessage(id, uncertain) {
    if (!root.service) return
    if (messageField.text.trim() !== "") {
      root.draftNotice = "Your current draft is kept. Send or clear it before editing another message."
      messageField.forceActiveFocus()
      return
    }
    var text = root.service.messageForRetry(id)
    if (!text) return
    messageField.text = text
    root.draftNotice = uncertain ? "Delivery was uncertain. Check the Muse app before sending this draft again."
      : "Message restored as a draft. Edit it, then press Send to retry."
    messageField.forceActiveFocus()
  }

  function updateReader() {
    if (root.readerService && root.readerService !== root.service)
      root.readerService.setReader(root, false)
    root.readerService = root.service
    if (root.service) root.service.setReader(root,
      root.opened && root.currentView === "chat" && conversation.followLatest)
  }

  function openLink(link) {
    // Only web links from Muse's replies; the label shows the real domain.
    // LinkSafety mirrors the helper's safe_url; tests/link_vectors.json pins
    // both, so the two checks cannot drift apart.
    if (linkSafety.isSafeLink(link)) Qt.openUrlExternally(String(link))
  }

  function styleLinks(html) {
    // Tint the helper's anchors. It emits exactly '<a href="...">' (pinned by
    // test_bridge.py), so anything else passes through unstyled, never broken.
    return String(html).split("<a href=").join('<a style="color:' + Theme.Color.accent + '" href=')
  }

  function scrollToLatest() {
    conversation.followLatest = true
    conversation.contentY = Math.max(0, conversation.contentHeight - conversation.height)
  }

  function chooseView(view) {
    root.currentView = view
    Qt.callLater(function() {
      if (view === "chat") {
        root.scrollToLatest()
        if (messageField.enabled) messageField.forceActiveFocus()
        else conversation.forceActiveFocus()
      } else details.forceActiveFocus()
    })
  }

  // Follow incoming replies only if the reader is already near the latest
  // message. Scrolling up to read an older reply must not jump them to the end.
  onChatCountChanged: if (conversation.followLatest) Qt.callLater(scrollToLatest)
  onSendingChanged: if (conversation.followLatest) Qt.callLater(scrollToLatest)
  onOpenedChanged: {
    root.updateReader()
    if (opened && currentView === "chat") Qt.callLater(scrollToLatest)
  }
  onCurrentViewChanged: root.updateReader()
  onServiceChanged: Qt.callLater(root.updateReader)
  Component.onDestruction: if (root.readerService) root.readerService.setReader(root, false)

  LinkSafety { id: linkSafety }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    bar: root.bar
    owner: root.hostWidget || root
    open: root.opened
    focusTarget: root.currentView === "chat" ? (messageField.enabled ? messageField : conversation) : details
    contentWidth: panel.fittedContentWidth(Theme.Style.space(440))
    contentHeight: panel.cappedContentHeight(Theme.Style.space(620))

    Item {
      id: layout
      anchors.fill: parent
      Keys.onEscapePressed: root.close()

      Column {
        id: header
        width: parent.width
        spacing: Theme.Style.space(14)

        Row {
          width: parent.width
          spacing: Theme.Style.space(10)
          Rectangle {
            width: Theme.Style.space(44)
            height: width
            radius: width / 2
            color: Theme.Util.alpha(Theme.Color.accent, 0.09)
            OmusefulMark {
              anchors.centerIn: parent
              width: Theme.Style.space(34)
              height: width
              foreground: root.foreground
            }
          }
          Column {
            width: parent.width - Theme.Style.space(44) - newChatButton.implicitWidth - closeButton.implicitWidth - parent.spacing * 3
            anchors.verticalCenter: parent.verticalCenter
            spacing: Theme.Style.space(3)
            Text {
              width: parent.width
              text: root.service && root.service.activeThreadTitle ? root.service.activeThreadTitle : "Muse"
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: root.foreground
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.subtitle
              font.bold: true
            }
            Row {
              width: parent.width
              spacing: Theme.Style.space(5)
              Rectangle {
                width: Theme.Style.space(5)
                height: width
                radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                color: root.connected ? Theme.Color.accent : Theme.Color.muted
              }
              Text {
                width: parent.width - Theme.Style.space(10)
                text: root.service ? root.service.statusLabel : "Checking connection…"
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: root.foreground
                opacity: 0.7
                font.family: Theme.Style.font.family
                font.pixelSize: Theme.Style.font.caption
              }
            }
          }
          Button {
            id: newChatButton
            anchors.verticalCenter: parent.verticalCenter
            text: "＋"
            fontSize: Theme.Style.font.subtitle
            radius: height / 2
            foreground: root.foreground
            focusable: true
            tooltipText: "New chat · /new"
            onClicked: root.startNewThread()
          }
          Button {
            id: closeButton
            anchors.verticalCenter: parent.verticalCenter
            text: "×"
            fontSize: Theme.Style.font.subtitle
            radius: height / 2
            foreground: root.foreground
            focusable: true
            tooltipText: "Close · Escape"
            onClicked: root.close()
          }
        }

      }

      // Bottom navigation, like the Muse app: a pill with the panel's views.
      Rectangle {
        id: navBar
        objectName: "nav-bar"
        anchors.bottom: parent.bottom
        width: parent.width
        height: Theme.Style.space(48)
        radius: height / 2
        color: Theme.Util.alpha(root.foreground, 0.055)
        Row {
          id: navRow
          anchors.fill: parent
          anchors.margins: Theme.Style.space(4)
          Repeater {
            model: [
              { view: "chat", icon: 0xF0365, label: "Chat" },
              { view: "threads", icon: 0xF043D, label: "Chats" },
              { view: "setup", icon: 0xF08BB, label: "Setup" }
            ]
            delegate: Button {
              required property var modelData
              width: navRow.width / 3
              height: navRow.height
              text: String.fromCodePoint(modelData.icon)
              fontSize: Theme.Style.font.subtitle
              tooltipText: modelData.label
              selected: root.currentView === modelData.view
              radius: height / 2
              foreground: root.foreground
              focusable: true
              onClicked: root.chooseView(modelData.view)
            }
          }
        }
      }

      Item {
        id: chatPage
        visible: root.currentView === "chat"
        anchors.top: header.bottom
        anchors.topMargin: Theme.Style.space(14)
        anchors.bottom: navBar.top
        anchors.bottomMargin: Theme.Style.space(10)
        width: parent.width

        Rectangle {
          id: connectionNotice
          visible: !root.connected
          width: parent.width
          height: visible ? noticeRow.implicitHeight + Theme.Style.space(16) : 0
          radius: Theme.Style.space(12)
          color: Theme.Util.alpha(root.foreground, 0.055)
          Row {
            id: noticeRow
            width: parent.width - Theme.Style.space(20)
            anchors.centerIn: parent
            spacing: Theme.Style.space(8)
            Text {
              width: parent.width - setupButton.implicitWidth - parent.spacing
              anchors.verticalCenter: parent.verticalCenter
              text: root.service && root.service.status.paired ? "Muse is offline. Your messages stay here." : "Connect this computer to start chatting."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.foreground
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.bodySmall
            }
          Button {
              id: setupButton
              text: root.service && root.service.status.paired ? "Details" : "Set up"
              foreground: root.foreground
              focusable: true
              onClicked: root.chooseView("setup")
            }
          }
        }

        Flickable {
          id: conversation
          property bool followLatest: true
          onFollowLatestChanged: root.updateReader()
          readonly property bool nearLatest: contentY >= contentHeight - height - Theme.Style.space(48)
          anchors.top: connectionNotice.bottom
          anchors.topMargin: connectionNotice.visible ? Theme.Style.space(12) : 0
          anchors.bottom: feedback.top
          anchors.bottomMargin: Theme.Style.space(10)
          width: parent.width
          contentWidth: width
          contentHeight: transcript.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          onMovementStarted: followLatest = false
          onMovementEnded: followLatest = nearLatest
          onContentHeightChanged: if (followLatest) Qt.callLater(root.scrollToLatest)
          Controls.ScrollBar.vertical: Controls.ScrollBar {
            onPressedChanged: conversation.followLatest = pressed ? false : conversation.nearLatest
          }

          Column {
            id: transcript
            width: conversation.width
            spacing: Theme.Style.space(10)

            Column {
              visible: root.chatCount === 0
              width: parent.width
              topPadding: Theme.Style.space(24)
              bottomPadding: Theme.Style.space(12)
              spacing: Theme.Style.space(12)
              OmusefulMark {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Theme.Style.space(76)
                height: width
                foreground: root.foreground
              }
              Text {
                width: parent.width
                text: "What can I help with?"
                textFormat: Text.PlainText
                horizontalAlignment: Text.AlignHCenter
                color: root.foreground
                font.family: Theme.Style.font.family
                font.pixelSize: Theme.Style.font.subtitle
                font.bold: true
              }
              Text {
                width: parent.width
                text: "Talk to your Muse from the Omarchy bar.\nReplies come from the Muse app."
                textFormat: Text.PlainText
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                color: root.foreground
                opacity: 0.65
                font.family: Theme.Style.font.family
                font.pixelSize: Theme.Style.font.bodySmall
              }
            }

            Repeater {
              model: root.chatMessages
              delegate: Item {
                id: chatRow
                required property string role
                required property string messageId
                required property string deliveryState
                required property string messageText
                required property string messageHtml
                required property string messageLinks
                required property bool partial
                readonly property var links: {
                  try { return chatRow.fromMuse ? JSON.parse(chatRow.messageLinks || "[]") : [] }
                  catch (e) { return [] }
                }
                readonly property bool fromMuse: role === "assistant"
                width: transcript.width
                height: messageActions.y + messageActions.implicitHeight
                Rectangle {
                  id: bubble
                  // A reply can be only a link card, with no text bubble.
                  visible: chatRow.messageText !== ""
                  width: Math.min(chatRow.width * 0.88, bubbleText.implicitWidth + Theme.Style.space(28))
                  height: visible ? bubbleText.height + Theme.Style.space(22) : 0
                  anchors.left: chatRow.fromMuse ? parent.left : undefined
                  anchors.right: chatRow.fromMuse ? undefined : parent.right
                  radius: Theme.Style.space(18)
                  color: Theme.Util.alpha(chatRow.fromMuse ? root.foreground : Theme.Color.accent, chatRow.fromMuse ? 0.065 : 0.18)
                  TextEdit {
                    id: bubbleText
                    width: Math.min(implicitWidth, chatRow.width * 0.88 - Theme.Style.space(28))
                    height: contentHeight
                    x: Theme.Style.space(14)
                    y: Theme.Style.space(11)
                    // Muse's replies use the helper's escaped HTML subset (links,
                    // bold, italic, code, code blocks, lists, tables); your own
                    // messages stay plain.
                    readonly property bool rich: chatRow.fromMuse && chatRow.messageHtml !== ""
                    text: rich ? root.styleLinks(chatRow.messageHtml) : chatRow.messageText
                    textFormat: rich ? TextEdit.RichText : TextEdit.PlainText
                    onLinkActivated: function(link) { root.openLink(link) }
                    HoverHandler {
                      cursorShape: bubbleText.hoveredLink !== "" ? Qt.PointingHandCursor : Qt.IBeamCursor
                    }
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    color: root.foreground
                    selectionColor: Theme.Util.alpha(Theme.Color.accent, 0.35)
                    selectedTextColor: root.foreground
                    font.family: Theme.Style.font.family
                    font.pixelSize: Theme.Style.font.body
                  }
                }
                Column {
                  id: cards
                  y: bubble.height + (chatRow.links.length && bubble.visible ? Theme.Style.space(6) : 0)
                  width: Math.min(chatRow.width * 0.88, Theme.Style.space(300))
                  height: chatRow.links.length ? implicitHeight + Theme.Style.space(6) : 0
                  spacing: Theme.Style.space(6)
                  Repeater {
                    model: chatRow.links
                    delegate: Rectangle {
                      id: card
                      required property var modelData
                      objectName: "link-card"
                      width: cards.width
                      height: cardText.implicitHeight + Theme.Style.space(20)
                      radius: Theme.Style.space(16)
                      color: cardMouse.containsMouse ? Theme.Util.alpha(root.foreground, 0.1)
                        : Theme.Util.alpha(root.foreground, 0.065)
                      Text {
                        id: cardIcon
                        anchors.left: parent.left
                        anchors.leftMargin: Theme.Style.space(14)
                        anchors.verticalCenter: parent.verticalCenter
                        text: String.fromCodePoint(0xF03CC)
                        color: Theme.Color.accent
                        font.family: Theme.Style.font.family
                        font.pixelSize: Theme.Style.font.subtitle
                      }
                      Column {
                        id: cardText
                        anchors.left: cardIcon.right
                        anchors.leftMargin: Theme.Style.space(10)
                        anchors.right: parent.right
                        anchors.rightMargin: Theme.Style.space(14)
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                          width: parent.width
                          text: card.modelData.title
                          textFormat: Text.PlainText
                          elide: Text.ElideRight
                          color: root.foreground
                          font.family: Theme.Style.font.family
                          font.pixelSize: Theme.Style.font.body
                          font.bold: true
                        }
                        Text {
                          width: parent.width
                          text: card.modelData.domain
                          textFormat: Text.PlainText
                          // Keep the registrable end visible: "…evil.example".
                          elide: Text.ElideLeft
                          color: root.foreground
                          opacity: 0.6
                          font.family: Theme.Style.font.family
                          font.pixelSize: Theme.Style.font.caption
                        }
                      }
                      MouseArea {
                        id: cardMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.openLink(card.modelData.url)
                      }
                    }
                  }
                }
                Text {
                  id: deliveryLabel
                  y: bubble.height + cards.height + Theme.Style.space(3)
                  width: chatRow.width * 0.88
                  anchors.right: parent.right
                  visible: !chatRow.fromMuse
                  height: visible ? implicitHeight : 0
                  text: root.service ? root.service.deliveryLabel(chatRow.deliveryState) : ""
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  horizontalAlignment: Text.AlignRight
                  color: chatRow.deliveryState === "failed" ? Theme.Color.urgent : root.foreground
                  opacity: 0.7
                  font.family: Theme.Style.font.family
                  font.pixelSize: Theme.Style.font.caption
                }
                Row {
                  id: messageActions
                  y: deliveryLabel.y + deliveryLabel.height + Theme.Style.space(3)
                  anchors.left: chatRow.fromMuse ? parent.left : undefined
                  anchors.right: chatRow.fromMuse ? undefined : parent.right
                  spacing: Theme.Style.space(8)
                  Button {
                    id: copyButton
                    text: "Copy"
                    fontSize: Theme.Style.font.caption
                    verticalPadding: Theme.Style.space(3)
                    foreground: root.foreground
                    focusable: true
                    tooltipText: bubbleText.selectedText ? "Copy selected text" : "Copy this message"
                    onClicked: if (root.service) root.service.copyMessage(bubbleText.selectedText || chatRow.messageText
                      || chatRow.links.map(function(link) { return link.url }).join("\n"))
                  }
                  Button {
                    visible: !chatRow.fromMuse && (chatRow.deliveryState === "failed" || chatRow.deliveryState === "uncertain")
                    text: chatRow.deliveryState === "failed" ? "Edit & retry" : "Copy to draft"
                    fontSize: Theme.Style.font.caption
                    verticalPadding: Theme.Style.space(3)
                    foreground: root.foreground
                    focusable: true
                    tooltipText: chatRow.deliveryState === "failed" ? "Restore as an editable draft; nothing is sent automatically"
                      : "Check Muse before resending; delivery is uncertain"
                    onClicked: root.restoreMessage(chatRow.messageId, chatRow.deliveryState === "uncertain")
                  }
                  Text {
                    visible: chatRow.partial && !root.sending
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Partial reply"
                    textFormat: Text.PlainText
                    color: root.foreground
                    opacity: 0.6
                    font.family: Theme.Style.font.family
                    font.pixelSize: Theme.Style.font.caption
                  }
                }
              }
            }

            Row {
              visible: root.sending
              spacing: Theme.Style.space(8)
              OmusefulMark {
                width: Theme.Style.space(24)
                height: width
                foreground: root.foreground
              }
              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.service ? root.service.chatStatus : "Muse is replying…"
                textFormat: Text.PlainText
                color: root.foreground
                opacity: 0.65
                font.family: Theme.Style.font.family
                font.pixelSize: Theme.Style.font.bodySmall
              }
            }
          }
        }

        Item {
          id: composerTools
          width: parent.width
          anchors.bottom: composer.top
          anchors.bottomMargin: Theme.Style.space(4)
          height: Theme.Style.space(28)
          Row {
            anchors.fill: parent
            spacing: Theme.Style.space(8)
            Button {
              id: emojiButton
              anchors.verticalCenter: parent.verticalCenter
              text: "☺"
              fontSize: Theme.Style.font.body
              verticalPadding: Theme.Style.space(2)
              foreground: root.foreground
              focusable: true
              tooltipText: "Emoji · Ctrl+E"
              selected: root.emojiPickerOpen
              onClicked: root.emojiPickerOpen = !root.emojiPickerOpen
            }
            Text {
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - emojiButton.implicitWidth - toolsHint.implicitWidth - parent.spacing * 2
              text: messageField.text.length > 0
                ? String(messageField.text.length) + " / " + String(root.maxMessageChars)
                : "Enter sends · Shift+Enter new line"
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: messageField.text.length > root.maxMessageChars ? Theme.Color.urgent : root.foreground
              opacity: 0.55
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.caption
            }
            Text {
              id: toolsHint
              anchors.verticalCenter: parent.verticalCenter
              text: "Ctrl+K chats · /help"
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.55
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.caption
            }
          }
        }

        Rectangle {
          id: emojiPicker
          visible: root.emojiPickerOpen
          width: parent.width
          anchors.bottom: composerTools.top
          anchors.bottomMargin: Theme.Style.space(4)
          height: visible ? Theme.Style.space(96) : 0
          radius: Theme.Style.space(12)
          color: Theme.Util.alpha(root.foreground, 0.055)
          Grid {
            anchors.fill: parent
            anchors.margins: Theme.Style.space(6)
            columns: 8
            columnSpacing: Theme.Style.space(2)
            rowSpacing: Theme.Style.space(2)
            Repeater {
              model: root.emojis
              delegate: Button {
                required property var modelData
                width: (emojiPicker.width - Theme.Style.space(12) - 7 * Theme.Style.space(2)) / 8
                height: Theme.Style.space(30)
                text: modelData
                fontSize: Theme.Style.font.subtitle
                foreground: root.foreground
                focusable: true
                onClicked: root.insertEmoji(modelData)
              }
            }
          }
        }

        Item {
          id: feedback
          readonly property string message: root.draftNotice || (root.service ? (root.service.error || root.service.chatMessage || root.service.message) : "")
          width: parent.width
          anchors.bottom: emojiPicker.visible ? emojiPicker.top : composerTools.top
          anchors.bottomMargin: visible ? Theme.Style.space(4) : 0
          height: visible ? feedbackText.implicitHeight : 0
          visible: message !== ""
          Text {
            id: feedbackText
            width: parent.width
            text: feedback.message
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: root.service && root.service.error ? Theme.Color.urgent : root.foreground
            font.family: Theme.Style.font.family
            font.pixelSize: Theme.Style.font.caption
          }
        }

        Rectangle {
          id: composer
          width: parent.width
          anchors.bottom: chatFootnote.top
          anchors.bottomMargin: Theme.Style.space(8)
          height: Math.max(Theme.Style.space(48), Math.min(Theme.Style.space(150), messageField.contentHeight + Theme.Style.space(26)))
          radius: Theme.Style.space(24)
          color: Theme.Util.alpha(root.foreground, 0.055)
          border.width: messageField.activeFocus ? 1 : 0
          border.color: Theme.Util.alpha(Theme.Color.accent, 0.45)
          Controls.ScrollView {
            id: composerScroll
            clip: true
            anchors.left: parent.left
            anchors.leftMargin: Theme.Style.space(10)
            anchors.right: sendButton.left
            anchors.rightMargin: Theme.Style.space(6)
            anchors.top: parent.top
            anchors.topMargin: Theme.Style.space(4)
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Theme.Style.space(4)
            contentWidth: availableWidth
            Controls.TextArea {
              id: messageField
              width: composerScroll.availableWidth
              placeholderText: root.connected ? "Message Muse…" : "Draft a message while Muse is offline…"
              textFormat: TextEdit.PlainText
              wrapMode: TextEdit.Wrap
              selectByMouse: true
              color: root.foreground
              placeholderTextColor: Theme.Util.alpha(root.foreground, 0.5)
              selectionColor: Theme.Util.alpha(Theme.Color.accent, 0.35)
              selectedTextColor: root.foreground
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.body
              background: Item { }
              onTextChanged: root.saveDraft()
              // Enter sends, Shift+Enter adds a line; Ctrl+V pastes.
              // Ctrl+Up/Ctrl+Down recall sent messages; plain Up/Down recalls
              // too while the draft is single-line, keeping multiline cursor
              // travel intact. Ctrl+K opens chats, Ctrl+E toggles emoji.
              Keys.onPressed: function(event) {
                var ctrl = event.modifiers & Qt.ControlModifier
                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                  root.submitMessage()
                  event.accepted = true
                } else if (ctrl && event.key === Qt.Key_K) {
                  root.emojiPickerOpen = false
                  root.chooseView("threads")
                  event.accepted = true
                } else if (ctrl && event.key === Qt.Key_E) {
                  root.emojiPickerOpen = !root.emojiPickerOpen
                  event.accepted = true
                } else if (ctrl && event.key === Qt.Key_Up) {
                  root.recallHistory(-1)
                  event.accepted = true
                } else if (ctrl && event.key === Qt.Key_Down) {
                  root.recallHistory(1)
                  event.accepted = true
                } else if (event.key === Qt.Key_Up && messageField.text.indexOf("\n") < 0 && root.sentHistory.length) {
                  root.recallHistory(-1)
                  event.accepted = true
                } else if (event.key === Qt.Key_Down && messageField.text.indexOf("\n") < 0 && root.sentHistory.length && root.historyIndex >= 0) {
                  root.recallHistory(1)
                  event.accepted = true
                }
              }
            }
          }
          Button {
            id: sendButton
            anchors.right: parent.right
            anchors.rightMargin: Theme.Style.space(6)
            anchors.bottom: parent.bottom
            anchors.bottomMargin: Theme.Style.space(6)
            text: root.sending ? "■" : "↑"
            fontSize: Theme.Style.font.subtitle
            horizontalPadding: Theme.Style.space(12)
            radius: height / 2
            background: Theme.Util.alpha(Theme.Color.accent, 0.2)
            foreground: root.foreground
            focusable: true
            tooltipText: root.sending ? "Stop waiting · Muse may continue in its app" : "Send · Enter"
            enabled: root.sending || (root.connected && root.service && messageField.text.trim() !== "")
            opacity: enabled ? 1 : 0.4
            onClicked: root.sending ? root.service.stopChat() : root.submitMessage()
          }
        }
        Text {
          id: chatFootnote
          width: parent.width
          anchors.bottom: parent.bottom
          text: "Muse replies from its own app · it can't run anything on this computer"
          textFormat: Text.PlainText
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.Wrap
          color: root.foreground
          opacity: 0.5
          font.family: Theme.Style.font.family
          font.pixelSize: Theme.Style.font.caption
        }
      }

      Flickable {
        id: details
        visible: root.currentView !== "chat"
        anchors.top: header.bottom
        anchors.topMargin: Theme.Style.space(16)
        anchors.bottom: navBar.top
        anchors.bottomMargin: Theme.Style.space(10)
        width: parent.width
        contentWidth: width
        contentHeight: detailContent.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        Controls.ScrollBar.vertical: Controls.ScrollBar { }

        Column {
          id: detailContent
          width: details.width
          spacing: Theme.Style.space(14)

          Column {
            id: threadsColumn
            visible: root.currentView === "threads"
            width: parent.width
            spacing: Theme.Style.space(10)
            Text {
              width: parent.width
              text: "Chats stay on this computer in memory. Muse is one conversation in its app; threads here only organize this panel. Switch is paused while Muse is replying."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.foreground
              opacity: 0.65
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.caption
            }
            Button {
              text: "＋ New chat"
              foreground: root.foreground
              background: Theme.Util.alpha(Theme.Color.accent, 0.25)
              bordered: true
              focusable: true
              onClicked: root.startNewThread()
            }
            Repeater {
              model: root.service ? root.service.threads : []
              delegate: Column {
                id: threadBlock
                required property var modelData
                required property string threadId
                required property string title
                required property int unread
                width: threadsColumn.width
                spacing: Theme.Style.space(6)
                Rectangle {
                  id: threadRow
                  width: parent.width
                  height: Theme.Style.space(52)
                  radius: Theme.Style.space(12)
                  color: root.service && threadBlock.threadId === root.service.activeThreadId
                    ? Theme.Util.alpha(Theme.Color.accent, 0.14)
                    : Theme.Util.alpha(root.foreground, 0.055)
                  Row {
                    anchors.fill: parent
                    anchors.margins: Theme.Style.space(8)
                    spacing: Theme.Style.space(8)
                    Button {
                      anchors.verticalCenter: parent.verticalCenter
                      width: parent.width - threadUnread.width - threadRename.width - threadDelete.width - parent.spacing * 3
                      text: threadBlock.title
                      foreground: root.foreground
                      focusable: true
                      radius: height / 2
                      selected: root.service && threadBlock.threadId === root.service.activeThreadId
                      onClicked: root.switchToThread(threadBlock.threadId)
                    }
                    Rectangle {
                      id: threadUnread
                      visible: threadBlock.unread > 0
                      anchors.verticalCenter: parent.verticalCenter
                      width: visible ? Math.max(Theme.Style.space(20), threadUnreadText.implicitWidth + Theme.Style.space(10)) : 0
                      height: Theme.Style.space(20)
                      radius: height / 2
                      color: Theme.Color.accent
                      Text {
                        id: threadUnreadText
                        anchors.centerIn: parent
                        text: threadBlock.unread > 9 ? "9+" : String(threadBlock.unread)
                        textFormat: Text.PlainText
                        color: Theme.Color.background
                        font.family: Theme.Style.font.family
                        font.pixelSize: Theme.Style.font.caption
                        font.bold: true
                      }
                    }
                    Button {
                      id: threadRename
                      anchors.verticalCenter: parent.verticalCenter
                      text: "✎"
                      fontSize: Theme.Style.font.bodySmall
                      foreground: root.foreground
                      focusable: true
                      tooltipText: "Rename chat"
                      onClicked: root.renamingThreadId = root.renamingThreadId === threadBlock.threadId ? "" : threadBlock.threadId
                    }
                    Button {
                      id: threadDelete
                      anchors.verticalCenter: parent.verticalCenter
                      text: "×"
                      fontSize: Theme.Style.font.body
                      foreground: root.foreground
                      focusable: true
                      tooltipText: "Delete chat"
                      enabled: root.service && root.service.threadCount > 1 && !root.service.chatBusy
                      opacity: enabled ? 0.8 : 0.4
                      onClicked: {
                        root.saveDraft()
                        if (root.renamingThreadId === threadBlock.threadId) root.renamingThreadId = ""
                        if (!root.service.deleteThread(threadBlock.threadId)) {
                          root.draftNotice = "Stop waiting or let Muse finish before deleting chats."
                        }
                      }
                    }
                  }
                }
                Row {
                  visible: root.renamingThreadId === threadBlock.threadId
                  width: parent.width
                  height: visible ? Theme.Style.space(40) : 0
                  spacing: Theme.Style.space(6)
                  Controls.TextField {
                    id: renameField
                    width: parent.width - renameSave.implicitWidth - parent.spacing
                    anchors.verticalCenter: parent.verticalCenter
                    text: threadBlock.title
                    color: root.foreground
                    font.family: Theme.Style.font.family
                    font.pixelSize: Theme.Style.font.body
                    background: Item { }
                    onAccepted: renameSave.onClicked()
                  }
                  Button {
                    id: renameSave
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Save"
                    fontSize: Theme.Style.font.caption
                    foreground: root.foreground
                    focusable: true
                    onClicked: {
                      if (root.service && root.service.renameThread(threadBlock.threadId, renameField.text))
                        root.renamingThreadId = ""
                      else if (renameField.text.trim() !== "")
                        root.draftNotice = "Name can't be empty."
                    }
                  }
                }
              }
            }
            Text {
              width: parent.width
              text: root.service && root.service.threadCount > 0
                ? String(root.service.threadCount) + (root.service.threadCount === 1 ? " chat" : " chats") + " · each keeps its last 20 messages."
                : "No chats yet."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.foreground
              opacity: 0.5
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.caption
            }
          }

          Column {
            visible: root.currentView === "setup"
            width: parent.width
            spacing: Theme.Style.space(14)
            SetupGuide {
              width: parent.width
              service: root.service
              foreground: root.foreground
              onTerminalRequested: root.close()
            }
            Column {
              width: parent.width
              spacing: Theme.Style.space(6)
              Button {
                text: root.service && root.service.notificationsEnabled ? "Reply notifications: On" : "Reply notifications: Off"
                selected: root.service && root.service.notificationsEnabled
                foreground: root.foreground
                focusable: true
                enabled: !!root.hostWidget && !!root.service
                onClicked: root.hostWidget.setReplyNotifications(!root.service.notificationsEnabled)
              }
              Text {
                width: parent.width
                text: "Optional desktop alerts say only ‘Muse replied’, never show chat text, and respect Do Not Disturb. The bar always shows unread replies."
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: root.foreground
                opacity: 0.65
                font.family: Theme.Style.font.family
                font.pixelSize: Theme.Style.font.caption
              }
            }
            Text {
              width: parent.width
              text: "Omuseful is an independent chat interface to the Muse app, not an official Meta product. Muse does its work in its own app and can't run anything on this computer. Chat shown here is cleared when the shell or this plugin reloads."
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              color: root.foreground
              opacity: 0.5
              font.family: Theme.Style.font.family
              font.pixelSize: Theme.Style.font.caption
            }
          }
        }
      }
    }
  }
}
