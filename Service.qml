import QtQuick
import Quickshell
import Quickshell.Io

// One instance per plugin. Omuseful is only a chat interface to the Muse app:
// it reads the companion's status and relays chat. Muse can't run anything here.
Item {
  id: root
  property var shell: null
  property var snapshot: ({ status: {}, error: "" })
  property string message: ""
  property alias chatMessages: chatState.messages
  readonly property int chatCount: chatState.count
  readonly property string chatMessage: chatState.message
  readonly property string chatPhase: chatState.phase
  property string chatPayload: ""
  property bool chatStarted: false
  property bool notificationsEnabled: false
  readonly property int unreadCount: chatState.unreadCount
  property string clipboardText: ""
  property string clipboardPurpose: ""
  property bool shuttingDown: false

  // -I: isolated mode (no user site-packages, PYTHON* variables, or script-dir imports).
  readonly property var helperCommand: ["/usr/bin/python3", "-I", "-B",
    decodeURIComponent(Qt.resolvedUrl("helpers/muse_bridge.py").toString().replace("file://", ""))]
  readonly property var status: snapshot.status || ({})
  readonly property bool chatBusy: chatProcess.running || chatState.pending
  readonly property string chatStatus: chatPhase === "sending" ? "Sending to Muse…"
    : chatPhase === "replying" ? "Muse is replying…"
    : chatPhase === "working" ? "Muse is working…"
    : chatBusy ? "Waiting for Muse…" : ""
  readonly property bool connected: status.available === true && status.connected === true && error === ""
  readonly property string statusLabel: status.label || "Checking companion…"
  readonly property string error: snapshot.error || ""
  readonly property var setup: snapshot.setup || ({ steps: [], current_step: 0, complete: false })
  readonly property string barStatus: root.chatBusy ? root.chatStatus
    : root.unreadCount > 0 ? "Unread Muse reply · " + root.statusLabel : root.statusLabel

  function setReader(reader, active) { chatState.setReader(reader, active) }
  function deliveryLabel(state) { return chatState.deliveryLabel(state) }
  function messageForRetry(id) { return chatState.retryText(id) }

  function copyCommand(text) { root.copyText(text, "command") }
  function copyMessage(text) { root.copyText(text, "message") }

  function copyText(text, purpose) {
    if (!String(text).trim() || clipboardProcess.running) return
    // Clipboard contents stay on stdin, not in process arguments or a shell.
    root.clipboardText = String(text)
    root.clipboardPurpose = purpose
    clipboardProcess.command = ["wl-copy", "--type", "text/plain"]
    clipboardProcess.running = true
  }

  function runInTerminal(command) {
    // Only a command the helper's setup guide offers right now (fixed scripts
    // in this plugin's companion/ folder, already shell-quoted). It opens in
    // Omarchy's floating installer terminal, where the script asks to
    // continue and sudo asks for your password.
    var steps = root.setup.steps || []
    var offered = steps.some(function(step) { return step.run && step.run === command })
    if (!offered) return
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", command])
  }

  Process {
    id: clipboardProcess
    stdinEnabled: true
    onStarted: {
      clipboardProcess.write(root.clipboardText)
      clipboardProcess.stdinEnabled = false
      root.clipboardText = ""
    }
    onExited: function(code) {
      clipboardProcess.stdinEnabled = true
      root.message = code === 0
        ? (root.clipboardPurpose === "command" ? "Command copied. Review it, then run it in your terminal." : "Message copied.")
        : "Clipboard is unavailable. Copy the text manually."
    }
  }

  function receive(line) {
    try {
      var next = JSON.parse(line)
      if (!next || !next.status) return
      // Keep stable step objects between identical heartbeats, so keyboard
      // focus on the guide's buttons is not recreated every second.
      if (JSON.stringify(next.setup || {}) === JSON.stringify(root.snapshot.setup || {}))
        next.setup = root.snapshot.setup
      root.snapshot = next
    } catch (e) {
      root.message = "Could not read the companion status."
    }
  }

  function send(text) {
    if (!String(text).trim() || !root.connected || root.chatBusy) return false
    var clean = String(text).trim()
    if (!chatState.begin(clean)) return false
    root.chatStarted = false
    root.chatPayload = clean
    chatTimeout.restart()
    chatProcess.command = root.helperCommand.concat(["send"])
    chatProcess.running = true
    return true
  }

  function stopChat() {
    if (!root.chatBusy) return
    // Closing the helper closes the socket; the companion then cancels the turn.
    chatState.interrupt("Stopped waiting. Muse may continue in its app. Check there before resending.")
    chatProcess.running = false
  }

  function receiveChat(line) {
    try {
      chatState.receive(JSON.parse(line))
    } catch (e) {
      chatState.message = "The chat bridge returned an invalid update. Any partial reply is kept."
    }
  }

  function finishChatProcess() {
    chatTimeout.stop()
    chatProcess.stdinEnabled = true
    root.chatPayload = ""
    if (!root.chatStarted)
      chatState.receive({ ok: false, delivery: "failed", error: "The local chat bridge could not start. Your message was not sent." })
    else
      chatState.interrupt("The reply connection stopped. Any partial text is kept; check the Muse app before resending.")
  }

  ChatState {
    id: chatState
    onReplyReady: {
      if (root.notificationsEnabled && !root.shuttingDown && !notificationProcess.running)
        notificationProcess.running = true
    }
  }

  Process {
    id: notificationProcess
    // Fixed text and a fixed click action only: no chat, link, credential, or
    // remote-provided command ever becomes a notification or process argument.
    // Normal urgency goes through Omarchy's own DND/snooze handling.
    command: ["omarchy", "notification", "send", "--app-name", "Omuseful", "-u", "normal",
      "-t", "8000", "Muse replied", "Open Omuseful to read the reply.",
      "--exec", "omarchy-shell", "shell", "summon", "omuseful"]
  }

  Process {
    id: watcher
    command: root.helperCommand.concat(["watch"])
    running: true
    stdout: SplitParser { onRead: function(line) { root.receive(line) } }
    onExited: function(code, status) {
      root.snapshot = ({ status: { available: false, label: "Local bridge stopped" }, error: "" })
      if (!root.shuttingDown) retryTimer.restart()
    }
  }

  Timer {
    id: retryTimer
    interval: 5000
    onTriggered: if (!root.shuttingDown) watcher.running = true
  }

  Process {
    id: chatProcess
    stdinEnabled: true
    onStarted: {
      root.chatStarted = true
      chatProcess.write(JSON.stringify({ message: root.chatPayload }) + "\n")
      chatProcess.stdinEnabled = false
      root.chatPayload = ""
      chatTimeout.restart()
    }
    stdout: SplitParser { onRead: function(line) { root.receiveChat(line) } }
    onExited: root.finishChatProcess()
    onRunningChanged: {
      // A failed process start may not produce exited. Defer until stdout and
      // the normal exit handler have drained, and never affect a newer turn.
      if (!running && chatState.pending) {
        var turn = chatState.turn
        Qt.callLater(function() {
          if (!chatProcess.running && chatState.turn === turn) root.finishChatProcess()
        })
      }
    }
  }

  Timer {
    id: chatTimeout
    interval: 420000
    onTriggered: {
      chatState.interrupt("Muse is taking longer than expected. Any partial text is kept; check the Muse app before resending.")
      chatProcess.running = false
    }
  }

  Component.onDestruction: {
    root.shuttingDown = true
    retryTimer.stop()
    chatTimeout.stop()
  }
}
