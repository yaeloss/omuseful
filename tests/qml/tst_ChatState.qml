import QtQuick
import QtTest
import "../.." as Omuseful

TestCase {
  id: testCase
  name: "ChatState"
  property var state: null

  Component { id: stateComponent; Omuseful.ChatState { } }
  SignalSpy { id: ready; target: testCase.state; signalName: "replyReady" }

  function init() {
    state = createTemporaryObject(stateComponent, testCase)
    verify(state !== null)
    ready.clear()
    verify(ready.valid)
  }

  function delivery() {
    return state.messages.get(state.indexOf(state.turn)).deliveryState
  }

  function partial(text, done) {
    state.receive({ event: "reply", id: "reply-1", text: text, done: done === true })
  }

  function complete(text) {
    state.receive({ ok: true, delivery: "complete", reply: text })
  }

  function test_delivery_lifecycle() {
    state.setReader("screen", true)
    verify(state.begin("  hello  "))
    compare(state.messages.get(0).messageText, "hello")
    compare(delivery(), "sending")
    verify(!state.begin("another pending message"))
    state.receive({ event: "accepted" })
    compare(delivery(), "accepted")
    partial("Hello")
    compare(delivery(), "replying")
    partial("Hello world", true)
    complete("Hello world")
    compare(delivery(), "complete")
    compare(state.count, 2)
    compare(state.messages.get(1).messageText, "Hello world")
    verify(!state.messages.get(1).partial)
    verify(!state.pending)
    compare(ready.count, 0)
  }

  function test_late_ack_does_not_downgrade_a_streaming_reply() {
    state.begin("hello")
    partial("Hello")
    state.receive({ event: "accepted" })
    compare(delivery(), "replying")
  }

  function test_confirmed_failure_keeps_an_editable_message_without_retrying() {
    state.begin("original draft")
    var id = state.turn
    state.receive({ ok: false, delivery: "failed", error: "Not sent" })
    compare(delivery(), "failed")
    compare(state.retryText(id), "original draft")
    compare(state.count, 1)
    verify(!state.pending)
    compare(ready.count, 0)
  }

  function test_missing_acknowledgement_is_uncertain_not_failed() {
    state.begin("hello")
    state.interrupt("Check Muse before resending")
    compare(delivery(), "uncertain")
    verify(state.deliveryLabel(delivery()).indexOf("check Muse") >= 0)
    compare(state.count, 1)
    compare(ready.count, 0)
  }

  function test_stopping_after_acceptance_preserves_partial_text_and_ignores_late_reply() {
    state.begin("hello")
    state.receive({ event: "accepted" })
    partial("Partial answer")
    state.interrupt("Stopped waiting")
    compare(delivery(), "incomplete")
    compare(state.retryText(state.turn), "")
    verify(state.messages.get(1).partial)
    complete("Too late")
    compare(state.messages.get(1).messageText, "Partial answer")
    compare(ready.count, 0)
  }

  function test_failure_cannot_undo_an_acceptance_acknowledgement() {
    state.begin("hello")
    state.receive({ event: "accepted" })
    state.receive({ ok: false, delivery: "failed" })
    compare(delivery(), "incomplete")
    compare(state.retryText(state.turn), "")
  }

  function test_reply_timeout_is_not_a_completed_answer() {
    state.begin("hello")
    partial("Partial answer")
    state.receive({ ok: true, delivery: "accepted", reply: "Partial answer", warning: "Timed out" })
    compare(delivery(), "incomplete")
    verify(state.messages.get(1).partial)
    compare(ready.count, 0)
  }

  function test_background_deltas_count_as_one_unread_turn_and_notify_once_on_completion() {
    state.begin("hello")
    partial("H")
    partial("Hello")
    compare(state.unreadCount, 1)
    compare(ready.count, 0)
    partial("Hello world", true)
    complete("Hello world")
    complete("Duplicate final")
    compare(state.count, 2)
    compare(state.unreadCount, 1)
    compare(ready.count, 1)
    state.setReader("screen", true)
    compare(state.unreadCount, 0)
  }

  function test_a_reader_on_any_monitor_suppresses_unread_and_notifications() {
    state.setReader("left", true)
    state.setReader("right", true)
    state.setReader("left", false)
    state.begin("hello")
    complete("Answer")
    compare(state.unreadCount, 0)
    compare(ready.count, 0)
    state.setReader("right", false)
    state.begin("another question")
    complete("Another answer")
    compare(state.unreadCount, 1)
    compare(ready.count, 1)
  }

  function test_hiding_the_panel_mid_reply_tracks_the_unseen_remainder() {
    state.setReader("screen", true)
    state.begin("hello")
    partial("First part")
    compare(state.unreadCount, 0)
    state.setReader("screen", false)
    partial("First part, more text")
    compare(state.unreadCount, 1)
    state.setReader("screen", true)
    complete("First part, more text")
    compare(state.unreadCount, 0)
    compare(ready.count, 0)
  }

  function test_card_only_reply_is_a_complete_unread_answer() {
    state.begin("find a listing")
    state.receive({ ok: true, delivery: "complete", links: [
      { url: "https://shop.example/item", title: "Listing", domain: "shop.example" }
    ] })
    compare(delivery(), "complete")
    compare(state.count, 2)
    compare(state.messages.get(1).messageText, "")
    compare(JSON.parse(state.messages.get(1).messageLinks).length, 1)
    compare(state.unreadCount, 1)
    compare(ready.count, 1)
  }

  function test_acceptance_without_a_reply_does_not_notify() {
    state.begin("hello")
    state.receive({ ok: true, delivery: "accepted" })
    compare(delivery(), "accepted")
    compare(state.unreadCount, 0)
    compare(ready.count, 0)
  }

  function test_history_is_bounded_and_turn_ids_are_unique() {
    var previous = ""
    for (var i = 0; i < 25; i++) {
      verify(state.begin("question " + i))
      verify(state.turn !== previous)
      previous = state.turn
      complete("answer " + i)
    }
    compare(state.count, 20)
    compare(state.messages.get(19).messageText, "answer 24")
  }
}
