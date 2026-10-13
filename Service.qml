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
  property alias chatMessages: chatStore
  readonly property int chatCount: chatStore.count
  property string chatMessage: ""
  property string chatPhase: ""
  property string chatTurn: ""
  property string chatPayload: ""
  property bool chatFinished: false
  property bool chatStreamed: false
  property string clipboardText: ""
  property string clipboardPurpose: ""
  property bool shuttingDown: false

  // -I: isolated mode (no user site-packages, PYTHON* variables, or script-dir imports).
  readonly property var helperCommand: ["/usr/bin/python3", "-I", "-B",
    decodeURIComponent(Qt.resolvedUrl("helpers/muse_bridge.py").toString().replace("file://", ""))]
  readonly property var status: snapshot.status || ({})
  readonly property bool chatBusy: chatProcess.running
  readonly property string chatStatus: chatPhase === "sending" ? "Sending to Muse…"
    : chatPhase === "replying" ? "Muse is replying…"
    : chatPhase === "working" ? "Muse is working…"
    : chatBusy ? "Waiting for Muse…" : ""
  readonly property bool connected: status.available === true && status.connected === true && error === ""
  readonly property string statusLabel: status.label || "Checking companion…"
  readonly property string error: snapshot.error || ""
  readonly property var setup: snapshot.setup || ({ steps: [], current_step: 0, complete: false })

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
    root.chatTurn = String(Date.now())
    root.chatPayload = clean
    root.chatMessage = ""
    root.chatPhase = "sending"
    root.chatFinished = false
    root.chatStreamed = false
    root.appendChat("user", root.chatTurn, clean, false)
    chatProcess.command = root.helperCommand.concat(["send"])
    chatProcess.running = true
    return true
  }

  function stopChat() {
    if (!root.chatBusy) return
    // Closing the helper closes the socket; the companion then cancels the turn.
    root.chatFinished = true
    root.chatMessage = "Stopped waiting. Muse's reply will still appear in the Muse app."
    chatProcess.running = false
  }

  function appendChat(role, id, text, partial, html, links) {
    chatStore.append({ role: role, messageId: id, messageText: text, messageHtml: html || "",
                       messageLinks: JSON.stringify(Array.isArray(links) ? links : []), partial: partial })
    if (chatStore.count > 20) chatStore.remove(0, chatStore.count - 20)
  }

  function receiveChat(line) {
    try {
      var reply = JSON.parse(line)
      if (!reply || typeof reply !== "object") return
      if (reply.event === "accepted") {
        root.chatPhase = "waiting"
      } else if (reply.event === "progress") {
        root.chatPhase = reply.phase === "working" ? "working" : "waiting"
      } else if (reply.event === "reply" && typeof reply.text === "string" && typeof reply.id === "string") {
        root.chatStreamed = true
        root.chatPhase = reply.done === true ? "working" : "replying"
        var id = root.chatTurn + ":" + reply.id
        var found = -1
        for (var i = 0; i < chatStore.count; i++) {
          if (chatStore.get(i).messageId === id) { found = i; break }
        }
        var html = typeof reply.html === "string" ? reply.html : ""
        var links = Array.isArray(reply.links) ? reply.links : []
        if (found < 0) root.appendChat("assistant", id, reply.text, reply.done !== true, html, links)
        else {
          chatStore.setProperty(found, "messageText", reply.text)
          chatStore.setProperty(found, "messageHtml", html)
          chatStore.setProperty(found, "messageLinks", JSON.stringify(links))
          chatStore.setProperty(found, "partial", reply.done !== true)
        }
      } else if (typeof reply.ok === "boolean") {
        root.chatFinished = true
        var text = typeof reply.reply === "string" ? reply.reply : ""
        if (reply.ok && text && !root.chatStreamed)
          root.appendChat("assistant", root.chatTurn + ":final", text, false,
                          typeof reply.html === "string" ? reply.html : "",
                          Array.isArray(reply.links) ? reply.links : [])
        root.chatMessage = reply.ok
          ? (reply.warning || (!text && !root.chatStreamed ? "Sent to Muse, but no reply text arrived. Check the Muse app." : ""))
          : (reply.error || "Could not send the message. Your chat is kept here.")
        root.chatPhase = ""
      }
    } catch (e) {
      root.chatMessage = "The chat bridge returned an invalid update. Any partial reply is kept."
    }
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

  ListModel { id: chatStore }

  Process {
    id: chatProcess
    stdinEnabled: true
    onStarted: {
      chatProcess.write(JSON.stringify({ message: root.chatPayload }) + "\n")
      chatProcess.stdinEnabled = false
      root.chatPayload = ""
      chatTimeout.restart()
    }
    stdout: SplitParser { onRead: function(line) { root.receiveChat(line) } }
    onExited: {
      chatTimeout.stop()
      chatProcess.stdinEnabled = true
      if (!root.chatFinished)
        root.chatMessage = root.chatMessage || "The reply connection stopped. Any partial text is kept; check the Muse app."
      root.chatPhase = ""
    }
  }

  Timer {
    id: chatTimeout
    interval: 370000
    onTriggered: {
      root.chatMessage = "Muse is taking longer than expected. Any partial text is kept; check the Muse app."
      root.chatFinished = true
      chatProcess.running = false
    }
  }

  Component.onDestruction: {
    root.shuttingDown = true
    retryTimer.stop()
    chatTimeout.stop()
  }
}
