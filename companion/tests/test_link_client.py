# Copyright (c) Meta Platforms, Inc. and affiliates.
# Modified by yaeloss to test Omuseful's bounded, correlated chat streaming.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import json
import struct

import pytest

from musegadget.link_client import (
    DeviceDescription, LinkSession, MessageDecoder, Outcome, _ChatReply, _Subscription, describe_result, encode_message,
    noise_url, printable,
)
from musegadget.noise import (
    ApplicationResponse, BodyChunk, NoiseFrameDecoder, NoiseXXResponder, ServiceFrame,
    encode_noise_frames,
)
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope


@pytest.fixture(autouse=True)
def fast_chat_settle(monkeypatch):
    """Production waits ESP32-like seconds before ending a turn; tests don't."""
    from musegadget import link_client
    monkeypatch.setattr(link_client, "SETTLE_S", 0.05)
    monkeypatch.setattr(link_client, "BUSY_HOLD_S", 0.3)

DEVICE = DeviceDescription(
    node_id="homelink-abcdef", display_name="pi", version="0.1.0",
    commands={"system.run": {"description": "run", "required": {}, "optional": {}}},
)


class Pipe:
    """One end of an in-memory WebSocket."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self._inbox, self._outbox = inbox, outbox

    async def send(self, data) -> None:
        await self._outbox.put(data)

    async def recv(self):
        data = await self._inbox.get()
        if data is None:
            raise ConnectionError("closed")
        return data

    async def close(self) -> None:
        await self._outbox.put(None)


class FakeVm:
    """Just enough of the Muse VM: Noise responder plus the control stream."""

    def __init__(self, ws: Pipe) -> None:
        self.ws = ws
        self.decoder = NoiseFrameDecoder()
        self.messages = MessageDecoder()
        self.stream_id = 0

    async def handshake(self) -> None:
        responder = NoiseXXResponder()
        responder.initialize()
        await self.ws.send(responder.read_message1_and_write_message2(await self.ws.recv()))
        responder.read_message3(await self.ws.recv())
        self.send_cipher, self.recv_cipher = responder.split()

    async def next_frame(self) -> ServiceFrame:
        while True:
            plain = self.recv_cipher.decrypt_with_ad(b"", await self.ws.recv())
            assembled = self.decoder.decode(plain)
            if assembled is not None:
                return decode_request_envelope(assembled)

    async def next_message(self) -> dict:
        while True:
            frame = await self.next_frame()
            assert frame.kind == "body_chunk"
            messages = self.messages.feed(frame.value.data)
            if messages:
                return messages[0]

    async def send_frame(self, frame: ServiceFrame) -> None:
        for chunk in encode_noise_frames(encode_response_envelope(frame)):
            await self.ws.send(self.send_cipher.encrypt_with_ad(b"", chunk))

    async def accept_control_stream(self, status: int = 200) -> ServiceFrame:
        request = await self.next_frame()
        self.stream_id = request.stream_id
        await self.send_frame(ServiceFrame.response(
            self.stream_id, ApplicationResponse(status=status, end_body=status >= 400),
        ))
        return request

    async def send_message(self, message: dict) -> None:
        await self.send_frame(ServiceFrame.body_chunk(
            self.stream_id, BodyChunk(data=encode_message(message)),
        ))


def make_session(run_command, connect_log: list):
    to_device, to_vm = asyncio.Queue(), asyncio.Queue()
    device_ws, vm_ws = Pipe(to_device, to_vm), Pipe(to_vm, to_device)

    async def connect(url, headers):
        connect_log.append((url, headers))
        return device_ws

    session = LinkSession(
        noise_host="gw.example", vm_id="vm 1&x", vm_auth_token="tok",
        device=DEVICE, run_command=run_command, connect=connect,
    )
    return session, FakeVm(vm_ws)


def test_register_invoke_result_and_unpair():
    async def scenario():
        calls, connects = [], []

        def run_command(command, params, timeout_ms):
            calls.append((command, params, timeout_ms))
            return {"ok": True, "payload": {"stdout": "hi\n", "exit_code": 0}}

        session, vm = make_session(run_command, connects)
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))

        await vm.handshake()
        request = await vm.accept_control_stream()
        assert (request.kind, request.value.verb, request.value.path, request.value.end_body) == (
            "request", "POST", "/link-control", False)

        register = await vm.next_message()
        assert register["method"] == "link.register"
        params = register["params"]
        assert params["node_id"] == "homelink-abcdef"
        assert (params["platform"], params["device_family"]) == ("linux", "homehub")
        assert "system.run" in params["commands_v2"]
        await vm.send_message({"type": "res", "id": register["id"], "ok": True})

        await vm.send_message({
            "method": "link.invoke", "id": "inv-1", "command": "system.run",
            "params": {"command": "echo hi"}, "timeout_ms": 5000,
        })
        result = await vm.next_message()
        assert result == {"method": "link.result", "id": "inv-1", "ok": True,
                          "payload": {"stdout": "hi\n", "exit_code": 0}}
        assert calls == [("system.run", {"command": "echo hi"}, 5000)]
        assert session.registered_at is not None

        await vm.send_message({"type": "evt", "event": "link.unpaired"})
        assert await asyncio.wait_for(task, 2) is Outcome.UNPAIRED
        url, headers = connects[0]
        assert url == "wss://gw.example/v1/noise?vm_id=vm%201%26x"
        assert headers == {"Authorization": "Bearer tok"}

    asyncio.run(scenario())


def test_forbidden_control_stream():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream(status=403)
        assert await asyncio.wait_for(task, 2) is Outcome.FORBIDDEN

    asyncio.run(scenario())


def test_stop_ends_the_session():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()
        stop.set()
        assert await asyncio.wait_for(task, 2) is Outcome.STOPPED

    asyncio.run(scenario())


def test_decoder_handles_split_and_batched_messages():
    data = encode_message({"a": 1}) + encode_message({"b": 2}) + struct.pack("<I", 0)
    decoder = MessageDecoder()
    assert decoder.feed(data[:5]) == []
    assert decoder.feed(data[5:]) == [{"a": 1}, {"b": 2}]


def test_decoder_rejects_oversize_messages():
    with pytest.raises(ValueError):
        MessageDecoder().feed(struct.pack("<I", 1 << 30))


def test_decoder_drops_messages_that_are_not_utf8():
    garbage = b'{"a":"\xff"}'
    data = struct.pack("<I", len(garbage)) + garbage + encode_message({"b": 2})
    assert MessageDecoder().feed(data) == [{"b": 2}]


def test_vm_id_is_escaped_like_encode_uri_component():
    assert noise_url("h", "a-b_c.d!~*'()?&=") == "wss://h/v1/noise?vm_id=a-b_c.d!~*'()%3F%26%3D"


def test_send_chat_posts_a_device_attributed_message_on_the_same_session():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()  # link.register

        reply = asyncio.ensure_future(session.send_chat("porch light on", "side-1"))
        request = await vm.next_frame()
        assert (request.kind, request.value.verb, request.value.path, request.value.end_body) == (
            "request", "POST", "/chat/stream", True)
        assert json.loads(request.value.body) == {
            "message": "porch light on", "output_modality": "text", "device_id": "homelink-abcdef",
            "session_id": "side-1"}
        headers = {h.key.lower(): h.value for h in request.value.headers}
        assert headers["content-type"] == "application/json"
        await vm.send_frame(ServiceFrame.response(
            request.stream_id,
            ApplicationResponse(status=200, body=b'{"accepted":true}', end_body=True),
        ))
        assert await asyncio.wait_for(reply, 2) == {
            "ok": True, "status": 200, "response": {"accepted": True}}
        task.cancel()

    asyncio.run(scenario())


def test_send_chat_can_wait_for_the_correlated_muse_reply():
    async def scenario():
        progress = []
        session, vm = make_session(lambda *a: {"ok": True}, [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()  # link.register

        reply = asyncio.ensure_future(session.send_chat("hello", wait_for_reply=True, on_progress=progress.append))
        subscription = await vm.next_frame()
        assert (subscription.kind, subscription.value.verb, subscription.value.path,
                subscription.value.body, subscription.value.end_body) == (
            "request", "POST", "/chat/subscribe", b"{}", True,
        )
        subscription_headers = {h.key.lower(): h.value for h in subscription.value.headers}
        assert subscription_headers["accept"] == "application/x-ndjson"
        assert subscription_headers["x-app-id"] == "hatch-web"
        request = await vm.next_frame()
        request_headers = {h.key.lower(): h.value for h in request.value.headers}
        assert request_headers["x-app-id"] == subscription_headers["x-app-id"]
        await vm.send_frame(ServiceFrame.response(
            request.stream_id,
            ApplicationResponse(
                status=200,
                body=b'{"result":{"message_id":"note-1","reply_to_message_id":"parent-1"}}',
                end_body=True,
            ),
        ))
        await vm.send_frame(ServiceFrame.response(
            subscription.stream_id,
            ApplicationResponse(status=200, body=json.dumps({
                "type": "event",
                "seq": 1,
                "event": "delta.text_append",
                "payload": {
                    "event_name": "message.assistant",
                    "message_id": "reply-1",
                    "parent_message_id": "note-1",
                    "text": "Hello ",
                },
            }).encode() + b"\n", end_body=False),
        ))
        # Give the receiver an actual partial-update interval; in-memory queue
        # writes otherwise batch the acknowledgement and every delta together.
        await asyncio.sleep(0.01)
        await vm.send_frame(ServiceFrame.body_chunk(
            subscription.stream_id,
            BodyChunk(data=json.dumps({
                "type": "event",
                "seq": 2,
                "event": "delta.text_append",
                "payload": {
                    "event_name": "message.assistant",
                    "message_id": "reply-1",
                    "text": "from Muse",
                },
            }).encode() + b"\n", end_body=False),
        ))
        await vm.send_frame(ServiceFrame.body_chunk(
            subscription.stream_id,
            BodyChunk(data=json.dumps({
                "type": "event",
                "seq": 3,
                "event": "delta.message_done",
                "payload": {"event_name": "message.assistant", "message_id": "reply-1"},
            }).encode() + b"\n", end_body=False),
        ))
        result = await asyncio.wait_for(reply, 2)
        assert result["ok"] and result["reply"] == "Hello from Muse"
        assert progress[0] == {"event": "accepted"}
        assert any(row.get("text") == "Hello " and row.get("done") is False for row in progress)
        assert progress[-1]["text"] == "Hello from Muse" and progress[-1]["done"] is True
        task.cancel()

    asyncio.run(scenario())


def test_reply_that_arrives_before_chat_ack_is_correlated():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()

        reply = asyncio.ensure_future(session.send_chat("hello", wait_for_reply=True))
        subscription = await vm.next_frame()
        request = await vm.next_frame()
        await vm.send_frame(ServiceFrame.response(
            subscription.stream_id,
            ApplicationResponse(status=200, body=json.dumps({
                "type": "event",
                "event": "message.assistant",
                "payload": {
                    "message_id": "reply-early",
                    "reply_to_message_id": "note-early",
                    "display_text": "Early reply",
                },
            }).encode() + b"\n", end_body=False),
        ))
        await vm.send_frame(ServiceFrame.response(
            request.stream_id,
            ApplicationResponse(
                status=200,
                body=b'{"result":{"message_id":"note-early"}}',
                end_body=True,
            ),
        ))
        assert (await asyncio.wait_for(reply, 2))["reply"] == "Early reply"
        task.cancel()

    asyncio.run(scenario())


def test_unparented_reply_from_per_turn_subscription_is_correlated():
    async def scenario():
        session, vm = make_session(lambda *a: {"ok": True}, [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()

        reply = asyncio.ensure_future(session.send_chat("hello", wait_for_reply=True))
        subscription = await vm.next_frame()
        request = await vm.next_frame()
        await vm.send_frame(ServiceFrame.response(
            request.stream_id,
            ApplicationResponse(
                status=200, body=b'{"result":{"message_id":"note-unparented"}}',
                end_body=True,
            ),
        ))
        await vm.send_frame(ServiceFrame.response(
            subscription.stream_id,
            ApplicationResponse(status=200, body=(json.dumps({
                "type": "event", "event": "message.user",
                "payload": {"message_id": "note-unparented", "display_text": "hello"},
            }).encode() + b"\n" + json.dumps({
                "type": "event",
                "event": "message.assistant",
                "payload": {
                    "message_id": "reply-unparented",
                    "display_text": "Unparented answer",
                },
            }).encode() + b"\n"), end_body=False),
        ))
        assert (await asyncio.wait_for(reply, 2))["reply"] == "Unparented answer"
        task.cancel()

    asyncio.run(scenario())


def test_streamed_partial_reply_arrives_before_completion():
    async def scenario():
        events = []
        tracker = _ChatReply(events.append)
        tracker.ack("note")
        tracker.event({"event": "delta.text_append", "payload": {
            "message_id": "reply", "reply_to_message_id": "note", "text": "Partial text",
        }})
        assert events[-1] == {"event": "reply", "id": "reply", "text": "Partial text", "done": False}
        assert not tracker.done.done()
        tracker.event({"event": "delta.message_done", "message_id": "reply", "payload": {}})
        assert await asyncio.wait_for(tracker.done, 1) == "Partial text"
        tracker.close()
    asyncio.run(scenario())


def test_empty_display_text_falls_back_to_content_and_envelope_id():
    async def scenario():
        tracker = _ChatReply()
        tracker.ack("note")
        tracker.event({"event": "message.assistant", "message_id": "reply", "payload": {
            "reply_to_message_id": "note", "display_text": "", "content": "Final answer",
        }})
        assert await asyncio.wait_for(tracker.done, 1) == "Final answer"
        tracker.close()
    asyncio.run(scenario())


def test_unrelated_delta_cannot_become_an_unparented_final():
    async def scenario():
        events = []
        tracker = _ChatReply(events.append)
        tracker.ack("note")
        tracker.event({"event": "delta.text_append", "payload": {
            "message_id": "other", "parent_message_id": "other-note", "text": "PRIVATE",
        }})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "other", "display_text": "PRIVATE",
        }})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "ours", "parent_message_id": "note", "display_text": "Our answer",
        }})
        assert await asyncio.wait_for(tracker.done, 1) == "Our answer"
        assert "PRIVATE" not in json.dumps(events)
        tracker.close()
    asyncio.run(scenario())


def test_busy_status_keeps_subscription_for_a_follow_up_reply():
    async def scenario():
        tracker = _ChatReply()
        tracker.ack("note")
        tracker.event({"event": "agent.status", "payload": {"activity_code": "working"}})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "first", "parent_message_id": "note", "display_text": "I’ll check.",
        }})
        await asyncio.sleep(0.1)  # within the busy hold: the turn stays open
        assert not tracker.done.done()
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "second", "parent_message_id": "first", "display_text": "All done.",
        }})
        tracker.event({"event": "agent.status", "payload": {"activity_code": "idle"}})
        assert await asyncio.wait_for(tracker.done, 1) == "I’ll check.\n\nAll done."
        tracker.close()
    asyncio.run(scenario())


def test_ack_without_ids_still_shows_live_reply():
    async def scenario():
        tracker = _ChatReply()
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "early", "display_text": "Early live answer",
        }})
        tracker.ack(None, None)
        tracker.event({"event": "message.user", "payload": {"message_id": "ours"}})  # our echo
        assert await asyncio.wait_for(tracker.done, 1) == "Early live answer"
        tracker.close()
    asyncio.run(scenario())


def test_cyclic_parent_references_are_rejected_without_recursion():
    async def scenario():
        tracker = _ChatReply()
        tracker.event({"event": "delta.message_start", "payload": {
            "message_id": "one", "parent_message_id": "two",
        }})
        tracker.event({"event": "delta.message_start", "payload": {
            "message_id": "two", "parent_message_id": "one",
        }})
        tracker.ack("note")
        assert tracker.text() == ""
        tracker.close()
    asyncio.run(scenario())


def test_subscription_parses_final_line_without_newline():
    events = []
    subscription = _Subscription(events.append)
    event = {"event": "message.assistant", "payload": {"message_id": "last", "display_text": "Answer"}}
    subscription.on_frame(ServiceFrame.response(1, ApplicationResponse(
        status=200, body=json.dumps(event).encode(), end_body=True,
    )))
    assert events == [event]


def test_final_subscription_eof_before_post_ack_keeps_completed_reply():
    async def scenario():
        tracker = _ChatReply()
        subscription = _Subscription(tracker.event, tracker.disconnect)
        echo = {"event": "message.user", "payload": {"message_id": "note"}}
        event = {"event": "message.assistant", "payload": {"message_id": "last", "display_text": "Answer"}}
        subscription.on_frame(ServiceFrame.response(1, ApplicationResponse(
            status=200, body=json.dumps(echo).encode() + b"\n" + json.dumps(event).encode(), end_body=True,
        )))
        tracker.ack("note")
        assert await asyncio.wait_for(tracker.done, 1) == "Answer"
        tracker.close()
    asyncio.run(scenario())


def test_each_invoke_logs_how_it_ended_but_not_what_it_ran(caplog):
    async def scenario():
        results = iter([
            {"ok": True, "payload": {"stdout": "", "exit_code": 3, "timed_out": False}},
            {"ok": False, "error": "no such file: /home/pi/secret"},
        ])
        session, vm = make_session(lambda *a: next(results), [])
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await vm.handshake()
        await vm.accept_control_stream()
        await vm.next_message()  # link.register
        for invoke_id, command in (("inv-1", "system.run"), ("inv-2", "file.read\nforged")):
            await vm.send_message({"method": "link.invoke", "id": invoke_id, "command": command,
                                   "params": {"command": "cat /home/pi/secret"}})
            await vm.next_message()
        task.cancel()

    with caplog.at_level("INFO", logger="musegadget.link_client"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("system.run ok in ") and line.endswith(" ms") for line in lines)
    assert any(line.startswith("file.read?forged failed in ") for line in lines)
    assert not any("\n" in line for line in lines)
    assert not any("secret" in line for line in lines)


@pytest.mark.parametrize("result, described", [
    ({"ok": True, "payload": {"pump": "on"}}, "ok"),
    ({"ok": True}, "ok"),
    ({"ok": True, "payload": {"exit_code": 0, "timed_out": False}}, "ok"),
    ({"ok": False, "error": "no such file: /home/pi/secret"}, "failed"),
    ({"ok": False}, "failed"),
])
def test_describe_result(result, described):
    assert describe_result(result) == described


def test_printable_replaces_control_characters():
    assert printable("system.run") == "system.run"
    assert printable("a\nb\x1b[2Jc") == "a?b?[2Jc"


def test_busy_agent_cannot_hold_a_finished_reply_open_forever():
    async def scenario():
        tracker = _ChatReply()
        tracker.ack("note")
        tracker.event({"event": "agent.status", "payload": {"activity_code": "working"}})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "r", "parent_message_id": "note", "display_text": "Flights found.",
        }})
        # No idle status ever arrives; the busy hold still ends the turn.
        assert await asyncio.wait_for(tracker.done, 2) == "Flights found."
        tracker.close()
    asyncio.run(scenario())


def test_waited_chat_posts_like_the_esp32_client_without_device_id():
    import inspect
    from musegadget.link_client import LinkSession
    source = inspect.getsource(LinkSession._send_chat)
    assert 'request_body = {"message": message, "output_modality": "text"}' in source
    assert 'if not wait_for_reply:\n' in source and 'request_body["device_id"]' in source


def test_reply_parented_to_the_echo_of_our_message_is_correlated():
    async def scenario():
        progress = []
        tracker = _ChatReply(progress.append, "what flights are there?")
        tracker.ack("ack-id")  # the acknowledgement names a different ID
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {
            "message_id": "reply", "reply_to_message_id": "stored-user-msg"}})
        tracker.event({"type": "event", "event": "delta.text_append", "payload": {
            "message_id": "reply", "text": "Two flights"}})
        assert tracker.text() == ""  # unknown parent: not shown yet
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "stored-user-msg", "content": "what flights are there?"}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "reply", "display_text": "Two flights leave tonight."}})
        assert await asyncio.wait_for(tracker.done, 2) == "Two flights leave tonight."
        assert any(event.get("event") == "reply" for event in progress)
        assert tracker.echo_ids == 1
        tracker.close()
    asyncio.run(scenario())


def test_someone_elses_message_is_not_mistaken_for_ours():
    async def scenario():
        tracker = _ChatReply(None, "hello from the desktop")
        tracker.ack("ack-id")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "phone-msg", "content": "hello from the phone"}})
        tracker.event({"type": "event", "event": "message.assistant", "payload": {
            "message_id": "reply", "reply_to_message_id": "phone-msg", "display_text": "Hi phone"}})
        await asyncio.sleep(0.2)
        assert not tracker.done.done() and tracker.text() == ""
        assert "other:message.user" in tracker.summary()
        tracker.close()
    asyncio.run(scenario())


def test_echo_text_is_found_in_nested_fields_with_different_spacing():
    async def scenario():
        tracker = _ChatReply(None, "what  flights\nare there?")
        tracker.ack("ack-id")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "stored", "parts": [{"type": "text", "value": " what flights are there? "}]}})
        tracker.event({"type": "event", "event": "message.assistant", "payload": {
            "message_id": "reply", "reply_to_message_id": "stored", "display_text": "Two."}})
        assert await asyncio.wait_for(tracker.done, 2) == "Two."
        assert tracker.echo_fallback is False and "parts" in tracker.summary()
        tracker.close()
    asyncio.run(scenario())


def test_first_user_message_soon_after_sending_is_bound_without_text():
    async def scenario():
        tracker = _ChatReply(None, "hello")
        tracker.ack("ack-id")
        tracker.event({"type": "event", "event": "message.user", "payload": {"message_id": "stored"}})
        tracker.event({"type": "event", "event": "message.assistant", "payload": {
            "message_id": "reply", "reply_to_message_id": "stored", "display_text": "Hi!"}})
        assert await asyncio.wait_for(tracker.done, 2) == "Hi!"
        assert tracker.echo_fallback is True
        tracker.close()
    asyncio.run(scenario())


def test_session_teardown_tolerates_an_open_reply_subscription():
    import inspect
    from musegadget.link_client import LinkSession
    source = inspect.getsource(LinkSession.run)
    assert 'getattr(request, "done", None)' in source


def test_reply_to_the_message_ours_answers_is_correlated_like_esp32():
    async def scenario():
        tracker = _ChatReply(None, "what's on today?")
        tracker.ack("user-msg")  # Linux ack: only our message_id
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "reply_to_message_id": "previous-assistant-msg",
            "display_text": "what’s on today?"}})  # typographic apostrophe: no text match needed
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {
            "message_id": "reply", "reply_to_message_id": "previous-assistant-msg"}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "reply", "display_text": "A dentist appointment at 3."}})
        assert await asyncio.wait_for(tracker.done, 2) == "A dentist appointment at 3."
        assert "parents=[ours]" in tracker.summary()
        tracker.close()
    asyncio.run(scenario())


def test_first_reply_after_our_echo_is_ours_even_with_an_unknown_parent():
    async def scenario():
        tracker = _ChatReply(None, "hi")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "hi"}})
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {
            "message_id": "reply", "reply_to_message_id": "some-thread-root"}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "reply", "display_text": "Hello!"}})
        assert await asyncio.wait_for(tracker.done, 2) == "Hello!"
        tracker.close()
    asyncio.run(scenario())


def test_another_users_message_disables_the_temporal_match():
    async def scenario():
        tracker = _ChatReply(None, "hi")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "hi"}})
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "phone-msg", "display_text": "from my phone"}})
        tracker.event({"type": "event", "event": "message.assistant", "payload": {
            "message_id": "reply", "reply_to_message_id": "phone-msg", "display_text": "Phone reply"}})
        await asyncio.sleep(0.2)
        assert not tracker.done.done() and tracker.text() == ""
        tracker.close()
    asyncio.run(scenario())


def test_reply_that_starts_before_our_echo_is_still_ours():
    async def scenario():
        tracker = _ChatReply(None, "hi")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {
            "message_id": "reply", "reply_to_message_id": "elsewhere"}})
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "hi"}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "reply", "display_text": "Hello!"}})
        assert await asyncio.wait_for(tracker.done, 2) == "Hello!"
        tracker.close()
    asyncio.run(scenario())


def test_links_from_resources_and_dropped_streamed_urls_reach_the_desktop():
    async def scenario():
        progress = []
        tracker = _ChatReply(progress.append, "send me the link")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "send me the link"}})
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {"message_id": "r"}})
        tracker.event({"type": "event", "event": "delta.text_append", "payload": {
            "message_id": "r", "text": "Here: https://example.com/a?b=1."}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "r", "display_text": "Here is the page.",
            "resources": [{"type": "link", "title": "Flights", "url": "https://flights.example/x"},
                          {"type": "link", "url": "javascript:alert(1)"}]}})
        await asyncio.wait_for(tracker.done, 2)
        final = [event for event in progress if event.get("event") == "reply"][-1]
        assert final["links"] == [{"url": "https://flights.example/x", "title": "Flights"},
                                  {"url": "https://example.com/a?b=1", "title": ""}]
        assert tracker.links() == final["links"]
        assert "resources[].url" in tracker.summary() and "links=2" in tracker.summary()
        tracker.close()
    asyncio.run(scenario())


def test_a_card_only_answer_is_a_reply_and_ends_the_turn():
    """Shape seen live: Muse answered with only a delta.presentation card."""
    async def scenario():
        progress = []
        tracker = _ChatReply(progress.append, "best url to order?")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "best url to order?"}})
        tracker.event({"type": "event", "event": "delta.presentation", "payload": {
            "message_id": "card-1", "kind": "link", "display_text": "Open the listing",
            "fallback_text": "Open the listing",
            "data": {"url": "https://www.shop.example/item/123", "fallback_text": "Shop",
                     "image_url": "https://cdn.shop.example/x.png"}}})
        await asyncio.wait_for(tracker.done, 2)
        reply = [event for event in progress if event.get("event") == "reply"][-1]
        assert reply["links"] == [{"url": "https://www.shop.example/item/123",
                                   "title": "Open the listing"}]
        assert tracker.links() == reply["links"] and "data.url" in tracker.summary()
        tracker.close()
    asyncio.run(scenario())


def test_card_after_text_is_its_own_item():
    async def scenario():
        progress = []
        tracker = _ChatReply(progress.append, "hi")
        tracker.ack("user-msg")
        tracker.event({"type": "event", "event": "message.user", "payload": {
            "message_id": "user-msg", "display_text": "hi"}})
        tracker.event({"type": "event", "event": "delta.message_start", "payload": {"message_id": "r1"}})
        tracker.event({"type": "event", "event": "delta.message_done", "payload": {
            "message_id": "r1", "display_text": "The open-box deal is best."}})
        tracker.event({"type": "event", "event": "delta.presentation", "payload": {
            "message_id": "card-1", "display_text": "Open the listing",
            "data": {"url": "https://www.shop.example/item/123"}}})
        assert await asyncio.wait_for(tracker.done, 2) == "The open-box deal is best."
        ids = {event["id"] for event in progress if event.get("event") == "reply"}
        assert ids == {"r1", "card-1"}
        tracker.close()
    asyncio.run(scenario())



def test_reply_in_another_chat_is_never_ours():
    async def scenario():
        tracker = _ChatReply(None, "hello")
        tracker.ack("ours")
        tracker.event({"event": "message.user", "payload": {
            "message_id": "ours", "display_text": "hello", "chat_context": {"chat_id": "main"}}})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "side", "display_text": "Background task finished",
            "chat_context": {"chat_id": "side-chat"}}})
        await asyncio.sleep(0.2)
        assert not tracker.done.done() and tracker.text() == ""
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "mine", "display_text": "Hi!", "chat_context": {"chat_id": "main"}}})
        assert await asyncio.wait_for(tracker.done, 2) == "Hi!"
        tracker.close()
    asyncio.run(scenario())


def test_short_message_does_not_claim_a_longer_foreign_message():
    async def scenario():
        tracker = _ChatReply(None, "ok")
        tracker.ack("ours")
        tracker.event({"event": "message.user", "payload": {
            "message_id": "phone", "display_text": "ok, now read my latest email"}})
        tracker.event({"event": "message.assistant", "payload": {
            "message_id": "r", "reply_to_message_id": "phone", "display_text": "Your email says..."}})
        await asyncio.sleep(0.2)
        assert tracker.text() == "" and tracker.foreign_user
        tracker.close()
    asyncio.run(scenario())


def test_nothing_is_published_before_our_echo_or_ids():
    async def scenario():
        progress = []
        tracker = _ChatReply(progress.append, "hello")
        tracker.ack(None, None)
        tracker.event({"event": "message.assistant", "payload": {"message_id": "x", "display_text": "?"}})
        assert not [event for event in progress if event.get("event") == "reply"]
        tracker.close()
    asyncio.run(scenario())


def test_hostile_stream_lines_cannot_end_the_session_or_bloat_the_log():
    tracker = _ChatReply.__new__(_ChatReply)
    tracker.counts = {}
    subscription = _Subscription(lambda message: None)
    subscription.on_frame(ServiceFrame.response(1, ApplicationResponse(
        status=200, body=b"[" * 200000 + b"\n", end_body=False)))
    for index in range(500):
        tracker.count(f"other:weird.event{index}")
    assert len(tracker.counts) <= 33


def test_image_entries_are_not_links():
    from musegadget.link_client import _payload_links
    links = _payload_links({"items": [
        {"type": "image", "url": "https://cdn.example/a.png"},
        {"preview_image": {"preview_card_url": "https://cdn.example/b.webp"}},
        {"url": "https://ok.example/page", "title": "Page"},
        {"url": "https://ctl.example/\x01x"},
    ]})
    assert links == [{"url": "https://ok.example/page", "title": "Page"}]
