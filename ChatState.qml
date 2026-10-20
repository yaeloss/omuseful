import QtQuick

// In-memory conversation state only. Kept separate from processes so delivery,
// interrupted replies, and multi-monitor read tracking can be tested offline.
Item {
  id: root
  property alias messages: store
  readonly property int count: store.count
  property bool pending: false
  property bool finished: true
  property bool accepted: false
  property bool streamed: false
  property string turn: ""
  property string phase: ""
  property string message: ""
  property int sequence: 0
  property int unreadCount: 0
  property bool turnUnread: false
  property var readers: []
  readonly property bool reading: readers.length > 0
  signal replyReady()

  function append(role, id, text, partial, html, links, delivery) {
    store.append({ role: role, messageId: id, messageText: text, messageHtml: html || "",
                   messageLinks: JSON.stringify(Array.isArray(links) ? links : []),
                   partial: partial, deliveryState: delivery || "" })
    if (store.count > 20) store.remove(0, store.count - 20)
  }

  function indexOf(id) {
    for (var i = 0; i < store.count; i++) if (store.get(i).messageId === id) return i
    return -1
  }

  function setDelivery(state) {
    var index = indexOf(root.turn)
    if (index >= 0) store.setProperty(index, "deliveryState", state)
  }

  function deliveryLabel(state) {
    switch (state) {
    case "sending": return "Sending…"
    case "accepted": return "Accepted by Muse"
    case "replying": return "Muse is replying…"
    case "complete": return "Complete"
    case "failed": return "Not sent · edit and retry"
    case "uncertain": return "Delivery uncertain · check Muse before resending"
    case "incomplete": return "Sent · reply incomplete here"
    default: return ""
    }
  }

  function retryText(id) {
    var index = indexOf(id)
    if (index < 0) return ""
    var row = store.get(index)
    return row.role === "user" && (row.deliveryState === "failed" || row.deliveryState === "uncertain")
      ? row.messageText : ""
  }

  function setReader(reader, active) {
    var next = root.readers.filter(function(item) { return item !== reader })
    if (active) next.push(reader)
    root.readers = next
    if (root.reading) {
      root.unreadCount = 0
      root.turnUnread = false
    }
  }

  function markIncoming() {
    if (root.reading || root.turnUnread) return
    root.turnUnread = true
    root.unreadCount = Math.min(99, root.unreadCount + 1)
  }

  function begin(text) {
    var clean = String(text).trim()
    if (!clean || root.pending) return false
    root.sequence += 1
    root.turn = String(Date.now()) + "-" + String(root.sequence)
    root.phase = "sending"
    root.message = ""
    root.pending = true
    root.finished = false
    root.accepted = false
    root.streamed = false
    root.turnUnread = false
    root.append("user", root.turn, clean, false, "", [], "sending")
    return true
  }

  function receive(reply) {
    if (root.finished || !reply || typeof reply !== "object") return
    if (reply.event === "accepted") {
      root.accepted = true
      if (!root.streamed) {
        root.phase = "waiting"
        root.setDelivery("accepted")
      }
    } else if (reply.event === "progress") {
      root.phase = reply.phase === "working" ? "working" : "waiting"
    } else if (reply.event === "reply" && typeof reply.text === "string" && typeof reply.id === "string") {
      var links = Array.isArray(reply.links) ? reply.links : []
      if (!reply.text && !links.length) return
      root.accepted = true
      root.streamed = true
      root.phase = reply.done === true ? "working" : "replying"
      root.setDelivery("replying")
      var id = root.turn + ":" + reply.id
      var index = root.indexOf(id)
      var html = typeof reply.html === "string" ? reply.html : ""
      if (index < 0) root.append("assistant", id, reply.text, reply.done !== true, html, links)
      else {
        store.setProperty(index, "messageText", reply.text)
        store.setProperty(index, "messageHtml", html)
        store.setProperty(index, "messageLinks", JSON.stringify(links))
        store.setProperty(index, "partial", reply.done !== true)
      }
      root.markIncoming()
    } else if (typeof reply.ok === "boolean") {
      root.finish(reply)
    }
  }

  function finish(reply) {
    if (root.finished) return
    var text = typeof reply.reply === "string" ? reply.reply : ""
    var links = Array.isArray(reply.links) ? reply.links : []
    if (reply.ok && !root.streamed && (text || links.length)) {
      root.streamed = true
      root.append("assistant", root.turn + ":final", text, reply.delivery !== "complete",
                  typeof reply.html === "string" ? reply.html : "", links)
      root.markIncoming()
    }
    var complete = reply.ok && reply.delivery === "complete" && root.streamed && !reply.warning
    if (reply.ok || root.accepted || reply.delivery === "accepted") {
      root.accepted = true
      root.setDelivery(complete ? "complete" : root.streamed || !reply.ok ? "incomplete" : "accepted")
    } else {
      root.setDelivery(reply.delivery === "failed" ? "failed" : "uncertain")
    }
    if (complete) {
      for (var i = 0; i < store.count; i++) {
        var row = store.get(i)
        if (row.role === "assistant" && row.messageId.indexOf(root.turn + ":") === 0)
          store.setProperty(i, "partial", false)
      }
    }
    root.message = reply.ok
      ? (reply.warning || (!root.streamed ? "Sent to Muse, but no reply arrived here. Check the Muse app." : ""))
      : (reply.error || "The reply connection stopped. Check the Muse app before resending.")
    root.finished = true
    root.pending = false
    root.phase = ""
    // One notification per completed turn, never per delta, partial reply, or
    // timeout. Readers on any monitor suppress it while following this chat.
    if (complete && !root.reading) {
      root.markIncoming()
      root.replyReady()
    }
  }

  function interrupt(reason) {
    if (root.finished) return
    root.finished = true
    root.pending = false
    root.phase = ""
    root.setDelivery(root.accepted ? "incomplete" : "uncertain")
    root.message = reason
  }

  ListModel { id: store }
}
