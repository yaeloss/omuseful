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
  readonly property color foreground: Theme.Color.popups.text
  readonly property bool connected: service && service.connected
  readonly property bool sending: service && service.chatBusy
  readonly property var chatMessages: service ? service.chatMessages : []
  readonly property int chatCount: service ? service.chatCount : 0

  function submitMessage() {
    if (!root.connected || !root.service || root.service.chatBusy) return
    if (root.service.send(messageField.text)) {
      root.draftNotice = ""
      messageField.text = ""
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
    // Same rules as the helper's safe_url: at most 2048 chars of printable
    // ASCII, a real host (or a valid bracketed IPv6 literal), a port within
    // range, no backslash, and no user@ part hiding the host.
    var url = String(link)
    if (url.length > 2048 || url.indexOf("\\") >= 0) return
    for (var i = 0; i < url.length; i++) {
      var code = url.charCodeAt(i)
      if (code < 0x21 || code > 0x7E) return
    }
    var parts = /^https?:\/\/(\[[^\]]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::([0-9]{1,5}))?(?:[\/?#]\S*)?$/.exec(url)
    if (!parts) return
    if (parts[2] !== undefined && parseInt(parts[2], 10) > 65535) return
    if (parts[1].charAt(0) === '[') {
      // Bracketed IPv6: "::" at most once, no ":::", 1-4 hex digits a group,
      // 8 groups without compression, fewer with it.
      var inner = parts[1].substring(1, parts[1].length - 1)
      if (!/^[0-9A-Fa-f:]+$/.test(inner) || inner.indexOf(":::") >= 0) return
      var halves = inner.split("::")
      if (halves.length > 2) return
      var groups = inner.split(":").filter(function(group) { return group.length > 0 })
      for (var g = 0; g < groups.length; g++) if (groups[g].length > 4) return
      if (halves.length === 1 ? groups.length !== 8 : groups.length >= 8) return
    }
    Qt.openUrlExternally(url)
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
            width: parent.width - Theme.Style.space(44) - closeButton.implicitWidth - parent.spacing * 2
            anchors.verticalCenter: parent.verticalCenter
            spacing: Theme.Style.space(3)
            Text {
              text: "Muse"
              textFormat: Text.PlainText
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
              { view: "setup", icon: 0xF08BB, label: "Setup" }
            ]
            delegate: Button {
              required property var modelData
              width: navRow.width / 2
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
                    // bold, italic, code, bullets); your own messages stay plain.
                    readonly property bool rich: chatRow.fromMuse && chatRow.messageHtml !== ""
                    text: rich ? chatRow.messageHtml.split("<a href=").join(
                        '<a style="color:' + Theme.Color.accent + '" href=') : chatRow.messageText
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
          id: feedback
          readonly property string message: root.draftNotice || (root.service ? (root.service.error || root.service.chatMessage || root.service.message) : "")
          width: parent.width
          anchors.bottom: composer.top
          anchors.bottomMargin: visible ? Theme.Style.space(8) : 0
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
              // Enter sends, Shift+Enter adds a line; Ctrl+V pastes.
              Keys.onPressed: function(event) {
                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                  root.submitMessage()
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
