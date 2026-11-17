import QtQuick

// In-memory conversation state only. Kept separate from processes so delivery,
// interrupted replies, and multi-monitor read tracking can be tested offline.
//
// Threads are client-side sessions: the muse_bridge socket is single-turn, so
// only one pending turn exists at a time and switching threads while pending
// is refused. `store` always holds the active thread's messages; switching
// archives the current messages into the thread list and restores the target.
// Everything stays in memory, cleared when the plugin reloads — no disk
// writes, matching the rest of Omuseful.
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

  // Threads: {threadId, title, updatedAt, draft, unread, messagesJson}
  property alias threads: threadStore
  readonly property int threadCount: threadStore.count
  property string activeThreadId: ""
  readonly property string activeThreadTitle: {
    var index = root.threadIndex(root.activeThreadId)
    return index >= 0 ? threadStore.get(index).title : "New chat"
  }
  readonly property string activeThreadDraft: {
    var index = root.threadIndex(root.activeThreadId)
    return index >= 0 ? threadStore.get(index).draft : ""
  }

  readonly property int maxThreads: 20
  readonly property int maxMessagesPerThread: 20

  function threadIndex(id) {
    var wanted = String(id)
    for (var i = 0; i < threadStore.count; i++)
      if (threadStore.get(i).threadId === wanted) return i
    return -1
  }

  function threadTitle(id) {
    var index = root.threadIndex(id)
    return index >= 0 ? threadStore.get(index).title : ""
  }

  // ListModel setProperty does not re-evaluate bindings that read via get().
  // Toggling the active id forces Panel's header/draft bindings to re-read.
  function touchActive() {
    var keep = root.activeThreadId
    if (!keep) return
    root.activeThreadId = ""
    root.activeThreadId = keep
  }

  function storeSnapshot() {
    var snap = []
    for (var i = 0; i < store.count; i++) {
      var row = store.get(i)
      snap.push({ role: row.role, messageId: row.messageId, messageText: row.messageText,
                  messageHtml: row.messageHtml, messageLinks: row.messageLinks,
                  partial: row.partial, deliveryState: row.deliveryState })
    }
    return snap
  }

  function storeRestore(snap) {
    store.clear()
    if (!Array.isArray(snap)) return
    for (var j = 0; j < snap.length; j++) store.append(snap[j])
  }

  function recalcUnread() {
    var total = 0
    for (var i = 0; i < threadStore.count; i++) total += threadStore.get(i).unread || 0
    root.unreadCount = total
  }

  function bumpActiveUnread() {
    var index = root.threadIndex(root.activeThreadId)
    if (index < 0) return
    threadStore.setProperty(index, "unread", (threadStore.get(index).unread || 0) + 1)
    root.recalcUnread()
  }

  function clearAllUnread() {
    for (var i = 0; i < threadStore.count; i++) threadStore.setProperty(i, "unread", 0)
    root.unreadCount = 0
  }

  function ensureThread() {
    if (threadStore.count > 0) return true
    root.sequence += 1
    var id = "genesis-" + String(Date.now()) + "-" + String(root.sequence)
    threadStore.append({ threadId: id, title: "New chat", updatedAt: Date.now(),
                         draft: "", unread: 0, messagesJson: "[]" })
    root.activeThreadId = id
    return true
  }

  function saveActiveThread() {
    var index = root.threadIndex(root.activeThreadId)
    if (index < 0) return
    threadStore.setProperty(index, "messagesJson", JSON.stringify(root.storeSnapshot()))
    threadStore.setProperty(index, "updatedAt", Date.now())
  }

  function loadThread(index) {
    var row = threadStore.get(index)
    var snap = []
    try { snap = JSON.parse(row.messagesJson || "[]") } catch (e) { snap = [] }
    root.storeRestore(Array.isArray(snap) ? snap : [])
    root.activeThreadId = row.threadId
    root.turn = ""
    root.phase = ""
    root.message = ""
    root.finished = true
    root.pending = false
    root.accepted = false
    root.streamed = false
    root.turnUnread = false
    root.recalcUnread()
  }

  function newThread() {
    if (root.pending) return false
    root.ensureThread()
    var activeIndex = root.threadIndex(root.activeThreadId)
    // Reuse a pristine active thread instead of stacking empty ones.
    if (activeIndex >= 0 && store.count === 0
        && String(threadStore.get(activeIndex).draft || "").trim() === "")
      return true
    root.saveActiveThread()
    root.sequence += 1
    var id = "thread-" + String(Date.now()) + "-" + String(root.sequence)
    threadStore.append({ threadId: id, title: "New chat", updatedAt: Date.now(),
                         draft: "", unread: 0, messagesJson: "[]" })
    // Evict the oldest non-active thread when over the cap.
    while (threadStore.count > root.maxThreads) {
      var victim = -1
      var oldest = Infinity
      for (var i = 0; i < threadStore.count; i++) {
        var row = threadStore.get(i)
        if (row.threadId !== id && row.threadId !== root.activeThreadId
            && row.updatedAt < oldest) { oldest = row.updatedAt; victim = i }
      }
      if (victim < 0) break
      threadStore.remove(victim, 1)
    }
    store.clear()
    root.activeThreadId = id
    root.turn = ""
    root.phase = ""
    root.message = ""
    root.finished = true
    root.pending = false
    root.accepted = false
    root.streamed = false
    root.turnUnread = false
    root.recalcUnread()
    return true
  }

  function switchThread(id) {
    if (root.pending) return false
    root.ensureThread()
    var target = String(id)
    if (target === root.activeThreadId) return true
    var index = root.threadIndex(target)
    if (index < 0) return false
    root.saveActiveThread()
    root.loadThread(index)
    return true
  }

  function renameThread(id, title) {
    var index = root.threadIndex(id)
    if (index < 0) return false
    var clean = String(title).trim().split("\n")[0].trim().slice(0, 80)
    if (!clean) return false
    threadStore.setProperty(index, "title", clean)
    threadStore.setProperty(index, "updatedAt", Date.now())
    if (String(id) === root.activeThreadId) root.touchActive()
    return true
  }

  function deleteThread(id) {
    if (root.pending) return false
    var index = root.threadIndex(id)
    if (index < 0) return false
    var wasActive = String(id) === root.activeThreadId
    threadStore.remove(index, 1)
    if (threadStore.count === 0) {
      root.activeThreadId = ""
      root.ensureThread()
      return true
    }
    if (wasActive) root.loadThread(0)
    else root.recalcUnread()
    return true
  }

  function setActiveDraft(text) {
    var index = root.threadIndex(root.activeThreadId)
    if (index >= 0) threadStore.setProperty(index, "draft", String(text))
  }

  function append(role, id, text, partial, html, links, delivery) {
    root.ensureThread()
    store.append({ role: role, messageId: id, messageText: text, messageHtml: html || "",
                   messageLinks: JSON.stringify(Array.isArray(links) ? links : []),
                   partial: partial, deliveryState: delivery || "" })
    if (store.count > root.maxMessagesPerThread) store.remove(0, store.count - root.maxMessagesPerThread)
    // Auto-title from the first user message of an untitled thread.
    if (role === "user") {
      var index = root.threadIndex(root.activeThreadId)
      if (index >= 0 && threadStore.get(index).title === "New chat" && store.count === 1) {
        var title = String(text).trim().split("\n")[0].trim().replace(/\s+/g, " ").slice(0, 48)
        if (title) { threadStore.setProperty(index, "title", title); root.touchActive() }
      }
    }
    var activeIndex = root.threadIndex(root.activeThreadId)
    if (activeIndex >= 0) threadStore.setProperty(activeIndex, "updatedAt", Date.now())
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
      root.clearAllUnread()
      root.turnUnread = false
    }
  }

  function markIncoming() {
    if (root.reading || root.turnUnread) return
    root.turnUnread = true
    root.bumpActiveUnread()
  }

  function begin(text) {
    var clean = String(text).trim()
    if (!clean || root.pending) return false
    root.ensureThread()
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
  ListModel { id: threadStore }
}
