# Copyright (c) Meta Platforms, Inc. and affiliates.
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
# Modified by yaeloss for Omuseful: bounded, correlated local chat streaming.

"""One control session with a Muse VM.

Opens a WebSocket to ``/v1/noise`` with the per-VM bearer in an Authorization
header, runs the Noise XX handshake, then opens a long-lived ``POST
/link-control`` stream. Both directions of that stream carry JSON messages,
each prefixed with its length as a little-endian u32:

* device → VM: ``link.register`` (capabilities), then ``link.result`` for
  each invoke;
* VM → device: the register reply, ``link.invoke`` requests, and events such
  as ``link.unpaired``.

Messages the device sends to the Muse (:meth:`LinkSession.send_chat`) go as
separate ``POST /chat/stream`` requests on the same session.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from musegadget.muse_api import user_agent
from musegadget.noise import Header, NoiseTransport, NoiseXXInitiator

log = logging.getLogger(__name__)

NOISE_PATH = "/v1/noise"
CONTROL_PATH = "/link-control"
CHAT_PATH = "/chat/stream"
CHAT_SUBSCRIBE_PATH = "/chat/subscribe"
APP_ID = "hatch-web"
REQUEST_TIMEOUT_S = 60
REPLY_TIMEOUT_S = 300
SETTLE_S = 3.0       # quiet period after the last finished reply (ESP32: SETTLE_US)
BUSY_HOLD_S = 20.0   # a busy agent keeps the turn open this long after its last text
ECHO_WINDOW_S = 15.0  # our own message.user echo arrives within this of sending
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REPLY_CHARS = 16 * 1024
HANDSHAKE_TIMEOUT_S = 20
PING_INTERVAL_S = 20
MAX_CONCURRENT_INVOKES = 4
MAX_INBOUND_MESSAGE = 4 * 1024 * 1024
# Matches JavaScript's encodeURIComponent, as the firmware does.
_URI_COMPONENT_SAFE = "-_.!~*'()"


class Outcome(enum.Enum):
    CLOSED = "closed"               # connection ended; reconnect normally
    AUTH_REJECTED = "auth_rejected"  # edge refused the VM bearer; re-fetch VMs
    FORBIDDEN = "forbidden"         # authenticated but not allowed right now
    UNPAIRED = "unpaired"           # the Muse removed this device
    STOPPED = "stopped"


@dataclass(frozen=True)
class DeviceDescription:
    node_id: str
    display_name: str
    version: str
    commands: dict

    def register_params(self) -> dict:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "platform": "linux",
            "version": self.version,
            "device_family": "homehub",
            "model_id": "linux",
            "is_wakeup_supported": False,
            "commands_v2": self.commands,
        }


def encode_message(obj: dict) -> bytes:
    data = json.dumps(obj, separators=(",", ":")).encode()
    return struct.pack("<I", len(data)) + data


class MessageDecoder:
    """Splits the control stream into length-prefixed JSON messages.

    A message may span body chunks, so bytes are buffered until complete.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        messages = []
        while len(self._buf) >= 4:
            (length,) = struct.unpack_from("<I", self._buf)
            if length > MAX_INBOUND_MESSAGE:
                raise ValueError(f"inbound message too large: {length}")
            if len(self._buf) < 4 + length:
                break
            raw = bytes(self._buf[4:4 + length])
            del self._buf[:4 + length]
            if not raw:
                continue  # keepalive
            try:
                message = json.loads(raw)
            except ValueError:  # bad JSON, or bytes that aren't UTF-8
                log.warning("dropping malformed control message (%d bytes)", length)
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages


def noise_url(noise_host: str, vm_id: str) -> str:
    return f"wss://{noise_host}{NOISE_PATH}?vm_id={quote(vm_id, safe=_URI_COMPONENT_SAFE)}"


class LinkSession:
    def __init__(
        self,
        *,
        noise_host: str,
        vm_id: str,
        vm_auth_token: str,
        device: DeviceDescription,
        run_command: Callable[[str, dict, int | None], dict],
        connect=None,
    ) -> None:
        self._url = noise_url(noise_host, vm_id)
        self._token = vm_auth_token
        self._device = device
        self._run_command = run_command
        self._connect = connect
        self._send_lock = asyncio.Lock()
        self._invokes = asyncio.Semaphore(MAX_CONCURRENT_INVOKES)
        self._tasks: set[asyncio.Task] = set()
        self._stream_id = 0
        self._register_id = ""
        self._requests: dict[int, _Request] = {}
        self._reply_tracker: _ChatReply | None = None
        self._chat_lock = asyncio.Lock()
        self.registered_at: float | None = None

    async def run(self, stop: asyncio.Event) -> Outcome:
        try:
            ws = await self._open()
        except _UpgradeRejected as rejected:
            log.warning("VM refused connection: HTTP %d", rejected.status)
            return Outcome.AUTH_REJECTED if rejected.status == 401 else Outcome.FORBIDDEN
        try:
            self._ws = ws
            self._transport = await asyncio.wait_for(self._handshake(ws), HANDSHAKE_TIMEOUT_S)
            await self._open_control_stream()
            reader = asyncio.ensure_future(self._read_loop())
            stopper = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done:
                reader.cancel()
                return Outcome.STOPPED
            stopper.cancel()
            return reader.result()
        finally:
            for task in self._tasks:
                task.cancel()
            for request in self._requests.values():
                # Reply subscriptions have no future; their tracker is told below.
                done = getattr(request, "done", None)
                if done is not None and not done.done():
                    done.set_exception(ConnectionError("session ended"))
            self._requests.clear()
            if self._reply_tracker is not None:
                self._reply_tracker.disconnect()
            self._reply_tracker = None
            await ws.close()

    # -- Connection setup -----------------------------------------------------

    async def _open(self):
        connect = self._connect
        headers = {"Authorization": f"Bearer {self._token}"}
        if connect is not None:
            return await connect(self._url, headers)
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus

        try:
            return await connect(
                self._url,
                additional_headers=headers,
                user_agent_header=user_agent(),
                open_timeout=HANDSHAKE_TIMEOUT_S,
                ping_interval=PING_INTERVAL_S,
                ping_timeout=PING_INTERVAL_S,
                max_size=None,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise _UpgradeRejected(status) from None
            raise

    async def _handshake(self, ws) -> NoiseTransport:
        initiator = NoiseXXInitiator()
        initiator.initialize()
        await ws.send(initiator.write_message1())
        msg2 = await ws.recv()
        if isinstance(msg2, str):
            raise ConnectionError("Noise handshake got a text frame")
        initiator.read_message2(bytes(msg2))
        # The bearer already authenticated us at the upgrade; message 3
        # carries an empty payload.
        await ws.send(initiator.write_message3())
        send, recv = initiator.split()
        log.info("Noise session established")
        return NoiseTransport(send, recv)

    async def _open_control_stream(self) -> None:
        encrypted = self._transport.start_stream_request("POST", CONTROL_PATH)
        self._stream_id = encrypted.stream_id
        await self._send_frames(encrypted.frames)
        self._register_id = str(uuid.uuid4())
        await self.send({
            "type": "req",
            "id": self._register_id,
            "method": "link.register",
            "params": self._device.register_params(),
        })
        log.info("sent link.register as %s", self._device.node_id)

    # -- Device-originated requests -------------------------------------------

    async def send_chat(self, message: str, session_id: str | None = None,
                        wait_for_reply: bool = False, on_progress=None) -> dict:
        """Post a user message to the Muse as coming from this device.

        Sent on this session, so the VM attributes the turn to the device
        registered on it (``device_id``) and routes any follow-up device
        commands back here. ``session_id`` targets a side chat; an id the Muse
        has not seen before starts a new one. Without it the message goes to
        the main chat.
        """
        if wait_for_reply:
            if self._chat_lock.locked():
                return {"ok": False, "error": "another desktop message is awaiting a reply"}
            async with self._chat_lock:
                return await self._send_chat(message, session_id, on_progress)
        return await self._send_chat(message, session_id, None, wait_for_reply=False)

    async def _send_chat(self, message, session_id, on_progress, wait_for_reply=True):
        subscription_id = 0
        subscription_frames = []
        tracker = _ChatReply(on_progress, message) if wait_for_reply else None
        if wait_for_reply:
            subscription_headers = [
                Header("Content-Type", "application/json"),
                Header("Accept", "application/x-ndjson"),
                Header("x-request-id", str(uuid.uuid4())),
                Header("x-app-id", APP_ID),
            ]
            encrypted = self._transport.encrypt_http_request(
                "POST", CHAT_SUBSCRIBE_PATH, b"{}", headers=subscription_headers,
            )
            subscription_id = encrypted.stream_id
            self._reply_tracker = tracker
            self._requests[subscription_id] = _Subscription(tracker.event, tracker.disconnect)
            subscription_frames = encrypted.frames

        request_body = {"message": message, "output_modality": "text"}
        if not wait_for_reply:
            # Upstream attributes fire-and-forget messages to this device. A
            # turn that waits for its reply posts like the published ESP32
            # client instead, so the reply is parented to the acknowledged
            # message rather than to a device-wrapped copy of it.
            request_body["device_id"] = self._device.node_id
        if session_id:
            request_body["session_id"] = session_id
        body = json.dumps(request_body).encode()
        headers = [
            Header("Content-Type", "application/json"),
            Header("x-request-id", str(uuid.uuid4())),
            Header("x-app-id", APP_ID),
        ]
        encrypted = self._transport.encrypt_http_request("POST", CHAT_PATH, body, headers=headers)
        request = _Request(asyncio.get_running_loop().create_future())
        self._requests[encrypted.stream_id] = request
        try:
            try:
                # Frames are encrypted with implicit nonces: once encrypted they
                # must be sent, even if the desktop hangs up meanwhile.
                if subscription_frames:
                    await asyncio.shield(self._send_frames(subscription_frames))
                await asyncio.shield(self._send_frames(encrypted.frames))
                status, response = await asyncio.wait_for(request.done, REQUEST_TIMEOUT_S)
            finally:
                self._requests.pop(encrypted.stream_id, None)
            try:
                decoded = json.loads(response) if response else None
            except json.JSONDecodeError:
                decoded = response.decode("utf-8", errors="replace")[:2000]
            result = {"ok": 200 <= status < 300, "status": status, "response": decoded}
            if not result["ok"] or not wait_for_reply:
                return result
            ack = decoded.get("result", decoded) if isinstance(decoded, dict) else {}
            message_id = ack.get("message_id") if isinstance(ack, dict) else None
            parent_id = ack.get("reply_to_message_id") if isinstance(ack, dict) else None
            # Follow the published ESP32 client's acknowledgement/correlation
            # rules. Some accepted responses omit IDs; keep the bounded live
            # subscription rather than silently dropping a valid reply.
            tracker.ack(message_id, parent_id)
            log.info("chat message accepted; waiting for its Muse reply")
            try:
                result["reply"] = await asyncio.wait_for(tracker.done, REPLY_TIMEOUT_S)
                if tracker.links():
                    result["links"] = tracker.links()
                log.info("correlated Muse reply received from chat subscription")
            except asyncio.TimeoutError:
                result["reply"] = tracker.text()
                result["warning"] = "Message sent. Muse is taking longer than five minutes; check the official Muse chat for its final reply."
            except ConnectionError:
                result["reply"] = tracker.text()
                result["warning"] = "Message sent, but the reply connection closed. Any partial text is kept; check the official Muse chat."
            return result
        finally:
            if tracker is not None:
                subscription = self._requests.get(subscription_id) if subscription_id else None
                log.info("chat turn summary: subscription_http=%s frames=%s lines=%s %s",
                         getattr(subscription, "status", "?"), getattr(subscription, "frames", "?"),
                         getattr(subscription, "lines", "?"), tracker.summary())
                tracker.close()
                if self._reply_tracker is tracker:
                    self._reply_tracker = None
            if subscription_id:
                self._requests.pop(subscription_id, None)
                try:
                    await self._send_frames(self._transport.encrypt_reset(
                        subscription_id, reason="reply received or timed out",
                    ))
                except Exception:
                    pass

    def _assistant_event(self, message: dict) -> None:
        if self._reply_tracker is not None:
            self._reply_tracker.event(message)

    # -- Sending --------------------------------------------------------------

    async def send(self, message: dict) -> None:
        frames = self._transport.encrypt_body_chunk(self._stream_id, encode_message(message))
        await self._send_frames(frames)

    async def _send_frames(self, frames) -> None:
        async with self._send_lock:
            for frame in frames:
                await self._ws.send(frame)

    # -- Receiving ------------------------------------------------------------

    async def _read_loop(self) -> Outcome:
        decoder = MessageDecoder()
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:
                log.info("control connection closed: %s", exc)
                return Outcome.CLOSED
            if isinstance(raw, str):
                log.warning("ignoring text frame on Noise connection")
                continue
            frame = self._transport.decrypt_frame(bytes(raw))
            if frame is None:
                continue
            if frame.stream_id != self._stream_id:
                self._requests.get(frame.stream_id, _NO_REQUEST).on_frame(frame)
                continue
            if frame.kind == "reset":
                log.warning("control stream reset: %s", frame.value.reason)
                return Outcome.CLOSED
            if frame.kind == "response":
                if frame.value.status >= 400:
                    log.warning("/link-control refused: HTTP %d", frame.value.status)
                    return Outcome.FORBIDDEN if frame.value.status == 403 else Outcome.CLOSED
                data, ended = frame.value.body, frame.value.end_body
            else:
                data, ended = frame.value.data, frame.value.end_body
            for message in decoder.feed(data):
                outcome = self._handle(message)
                if outcome is not None:
                    return outcome
            if ended:
                log.info("control stream ended by VM")
                return Outcome.CLOSED

    def _handle(self, message: dict) -> Outcome | None:
        if message.get("id") == self._register_id and message.get("method") is None:
            if message.get("error"):
                log.error("link.register rejected: %s", message["error"])
            else:
                self.registered_at = time.monotonic()
                log.info("registered with the Muse")
            return None
        event = message.get("event")
        self._assistant_event(message)
        if event in ("link.unpaired", "node.unpaired"):
            log.warning("the Muse removed this device")
            return Outcome.UNPAIRED
        if message.get("method") == "link.invoke":
            task = asyncio.ensure_future(self._invoke(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return None

    async def _invoke(self, message: dict) -> None:
        invoke_id = message.get("id")
        command = message.get("command") or ""
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        timeout_ms = message.get("timeout_ms") or None
        if not invoke_id:
            return
        shown = printable(command)
        log.info("invoke %s", shown)
        async with self._invokes:
            started = time.monotonic()
            result = await asyncio.get_running_loop().run_in_executor(
                None, self._run_command, command, params, timeout_ms,
            )
            log.info("%s %s in %d ms", shown, describe_result(result),
                     (time.monotonic() - started) * 1000)
        await self.send({"method": "link.result", "id": invoke_id, **result})


def printable(text: str) -> str:
    """text with control characters replaced, so it can't forge log lines."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def describe_result(result: dict) -> str:
    """How an invoke ended, for the log (always "failed": nothing runs here).
    Never its parameters or error, which can echo them."""
    return "ok" if result.get("ok") else "failed"


class _Request:
    """Collects the response to one request stream."""

    def __init__(self, done: asyncio.Future) -> None:
        self.done = done
        self.status = 0
        self.body = bytearray()

    def on_frame(self, frame) -> None:
        if self.done.done():
            return
        if frame.kind == "reset":
            self.done.set_exception(ConnectionError(f"stream reset: {frame.value.reason}"))
            return
        if frame.kind == "response":
            self.status = frame.value.status
            data, ended = frame.value.body, frame.value.end_body
        else:
            data, ended = frame.value.data, frame.value.end_body
        self.body += data
        if len(self.body) > MAX_RESPONSE_BYTES:
            self.done.set_exception(ValueError("response too large"))
        elif ended:
            self.done.set_result((self.status, bytes(self.body)))


class _Subscription:
    """Parse bounded NDJSON assistant events from /chat/subscribe."""

    def __init__(self, on_event, on_closed=lambda: None) -> None:
        self.on_event = on_event
        self.on_closed = on_closed
        self.body = bytearray()
        self.accepted = False
        self.overflow = False
        self.frames = 0
        self.lines = 0
        self.status = 0

    def on_frame(self, frame) -> None:
        self.frames += 1
        if frame.kind == "reset":
            self.on_closed()
            return
        if frame.kind == "response":
            self.status = frame.value.status
            self.accepted = frame.value.status == 200
            log.info("chat reply subscription HTTP %d", frame.value.status)
            if not self.accepted:
                self.on_closed()
            data = frame.value.body
        else:
            data = frame.value.data
        ended = frame.value.end_body
        if not self.accepted:
            return
        # Bound each line even when an oversized event spans many chunks. Do
        # not parse the tail of an oversized line as a different valid event.
        parts = data.split(b"\n")
        for index, part in enumerate(parts):
            if not self.overflow:
                if len(self.body) + len(part) > MAX_RESPONSE_BYTES:
                    self.overflow = True
                    self.body.clear()
                else:
                    self.body.extend(part)
            if index < len(parts) - 1:
                if not self.overflow:
                    self._parse(bytes(self.body))
                self.body.clear()
                self.overflow = False
        if ended:
            if self.body and not self.overflow:
                self._parse(bytes(self.body))
            self.body.clear()
            self.on_closed()

    def _parse(self, raw: bytes) -> None:
        self.lines += 1
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return  # malformed or absurdly nested lines never end the session
        if isinstance(message, dict):
            self.on_event(message)


_TEXT_KEYS = ("content", "text", "display_text", "message", "body", "parts")
_URL = re.compile(r"https?://[^\s<>\"'`)\]\x00-\x1f\x7f]{1,2048}")
_URL_KEYS = ("url", "href", "link", "uri", "web_url", "source_url")
_TITLE_KEYS = ("title", "label", "name", "display_name", "text", "subtitle")
MAX_LINKS = 10
_IMAGE_WORDS = ("image", "icon", "thumb", "avatar", "favicon", "logo", "photo", "img")


def _chat_ids(payload) -> set:
    """The chat/session a stream event belongs to, when the event says."""
    context = payload.get("chat_context") if isinstance(payload.get("chat_context"), dict) else {}
    return {value for value in (context.get("chat_id"), payload.get("session_id"))
            if isinstance(value, str) and 0 < len(value) <= 256}


def _key_names(payload) -> str:
    """Field names only (never values), for diagnosing protocol shapes."""
    names = set()
    for key, value in list(payload.items())[:60]:
        if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,30}", key):
            names.add(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                names.update(f"{key}[].{inner}" for inner in list(value[0])[:20]
                             if isinstance(inner, str) and re.fullmatch(r"[a-z_]{1,30}", inner))
    return ",".join(sorted(names))[:400]


def _key_paths(value, prefix="", depth=0) -> str:
    """Nested field names only (never values), for diagnosing card shapes."""
    names = []
    if depth < 3 and isinstance(value, dict):
        for key, item in list(value.items())[:30]:
            if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,30}", key):
                names.append(prefix + key)
                names.extend(filter(None, _key_paths(item, prefix + key + ".", depth + 1).split(",")))
    elif depth < 3 and isinstance(value, list) and value:
        return _key_paths(value[0], prefix.rstrip(".") + "[].", depth + 1)
    return ",".join(names)[:500]


def _payload_links(value, depth=0) -> list[dict]:
    """http(s) links in structured fields (e.g. resources), with a title if any."""
    found = []
    if depth > 4:
        return found
    if isinstance(value, dict):
        kind = value.get("type") or value.get("kind")
        if isinstance(kind, str) and any(word in kind.lower() for word in _IMAGE_WORDS):
            return found  # an image, not a link
        keys = [key for key in _URL_KEYS if key in value] + [
            key for key in value if isinstance(key, str) and key not in _URL_KEYS]
        for key in keys:
            url = value.get(key)
            if (isinstance(url, str) and _URL.fullmatch(url.strip())
                    and not any(word in key.lower() for word in _IMAGE_WORDS)):
                title = next((value[name] for name in _TITLE_KEYS
                              if isinstance(value.get(name), str) and value[name].strip()
                              and not _URL.fullmatch(value[name].strip())), "")
                found.append({"url": url.strip(), "title": title.strip()[:200]})
                break
        for key, item in list(value.items())[:50]:
            if isinstance(item, (dict, list)) and not (
                    isinstance(key, str) and any(word in key.lower() for word in _IMAGE_WORDS)):
                found.extend(_payload_links(item, depth + 1))
    elif isinstance(value, list):
        for item in value[:50]:
            found.extend(_payload_links(item, depth + 1))
    return found[:MAX_LINKS]


def _add_links(row: dict, links: list[dict]) -> None:
    current = row.setdefault("links", [])
    known = {link["url"] for link in current}
    for link in links:
        url = link["url"].rstrip(".,;:!?")
        if url not in known and len(current) < MAX_LINKS:
            current.append({"url": url, "title": link.get("title", "")})
            known.add(url)


def _strings(value, depth=0):
    """Whitespace-normalised strings in a bounded JSON value."""
    if isinstance(value, str):
        yield " ".join(value.split())
    elif depth < 3 and isinstance(value, dict):
        for item in list(value.values())[:50]:
            yield from _strings(item, depth + 1)
    elif depth < 3 and isinstance(value, list):
        for item in value[:50]:
            yield from _strings(item, depth + 1)


class _ChatReply:
    """One bounded turn, using the public ESP32 SDK's event and parent fields.

    Text stays in memory and on the requesting desktop socket, never in logs.
    Keep delta rows through their final event; later chunks often omit parents.
    """

    def __init__(self, on_progress=None, sent_text: str = ""):
        self.done = asyncio.get_running_loop().create_future()
        self.on_progress = on_progress or (lambda event: None)
        self.ids = set()
        # Our own message as the Muse echoes it; replies may be parented to
        # that stored copy rather than to the acknowledged message ID.
        self.sent_text = " ".join(sent_text.split())
        self.sent_at = time.monotonic()
        self.echo_ids = 0
        self.echo_fallback = False
        self.foreign_user = False
        self.chat_ids = set()
        self.user_keys = ""
        self.assistant_keys = ""
        self.presentation_keys = ""
        self.rows = {}
        self.rejected = set()
        self.acked = False
        self.closed = False
        self.busy = False
        self.seq = 0
        self.settle = None
        self.last_emit = 0.0
        self.last_content = time.monotonic()
        self.counts = {}

    def count(self, key):
        if key not in self.counts and len(self.counts) >= 32:
            key = "other:*"  # bounded summary, whatever names the stream sends
        self.counts[key] = self.counts.get(key, 0) + 1

    def summary(self) -> str:
        """Metadata for diagnosing correlation: event names and decisions only."""
        rows = sum(1 for message_id, row in self.rows.items() if self.related(message_id, row))
        relations = ",".join(
            "none" if not row["parent"] else "ours" if row["parent"] in self.ids
            else "row" if row["parent"] in self.rows else "unknown"
            for row in self.rows.values())
        return (f"events={dict(sorted(self.counts.items()))} rows={len(self.rows)} "
                f"related_rows={rows} rejected={len(self.rejected)} "
                f"ids={len(self.ids)} echo_ids={self.echo_ids} echo_fallback={self.echo_fallback} "
                f"user_keys=[{self.user_keys}] assistant_keys=[{self.assistant_keys}] "
                f"presentation_keys=[{self.presentation_keys}] "
                f"links={sum(len(row.get('links', [])) for row in self.rows.values())} "
                f"parents=[{relations}] foreign_user={self.foreign_user} "
                f"busy={self.busy} closed={self.closed}")

    def emit(self, event):
        self.on_progress(event)

    def ack(self, *ids):
        self.ids |= {value for value in ids if isinstance(value, str) and value}
        self.acked = True
        self.emit({"event": "accepted"})
        for message_id, row in list(self.rows.items()):
            if self.related(message_id, row):
                self.publish(message_id, row, force=True)
        if self.closed:
            self.disconnect()

    def presentation(self, message, payload):
        """A link card (e.g. "Open the listing"), shown in the Muse app
        as its own item. It can be the whole answer, so it is a reply row."""
        if not self.presentation_keys:
            self.presentation_keys = _key_paths(payload)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        label = next((value.strip()[:200] for value in (
            payload.get("display_text"), payload.get("fallback_text"), data.get("fallback_text"),
            data.get("title")) if isinstance(value, str) and value.strip()), "")
        links = _payload_links(payload)
        for link in links:
            link["title"] = link["title"] or label
        if not links:
            return
        target = payload.get("message_id") or message.get("message_id") or payload.get("id")
        if not isinstance(target, str) or not target or len(target) > 256:
            target = "presentation-%d" % len(self.rows)
        if target not in self.rows and len(self.rows) >= 16:
            return
        row = self.rows.setdefault(target, {
            "parent": "", "text": "", "done": True, "early": not self.acked,
            "after_echo": bool(self.echo_ids) and not self.foreign_user})
        parent = payload.get("reply_to_message_id")
        if isinstance(parent, str) and parent and len(parent) <= 256 and not row["parent"]:
            row["parent"] = parent
        if _chat_ids(payload):
            row["chat"] = _chat_ids(payload)
        self.last_content = time.monotonic()
        _add_links(row, links)
        if self.acked and self.related(target, row):
            self.publish(target, row, force=True)

    def learn_echo(self, message, payload):
        """Bind the Muse's stored copy of our message (a ``message.user`` event).

        Match its text anywhere in the payload, ignoring whitespace. If the text
        is not found, take the first user message within ECHO_WINDOW_S of
        sending, which is ours unless another device posts at the same moment.
        """
        event = message.get("event") or payload.get("event") or payload.get("event_name")
        if event != "message.user" or len(self.ids) >= 8:
            return
        if not self.user_keys:
            self.user_keys = _key_names(payload)
        echo_id = payload.get("message_id") or message.get("message_id") or payload.get("id")
        if not isinstance(echo_id, str) or not echo_id or len(echo_id) > 256:
            return
        if echo_id in self.ids:
            # The acknowledged message itself. As in the ESP32 client, replies
            # may be parented to the message it answers, which the Linux
            # acknowledgement omits but this event carries.
            self.bind_echo(echo_id, payload)
            return
        texts = [text for key in _TEXT_KEYS if key in payload for text in _strings(payload[key]) if text]
        # Exact match; a containing text only counts for longer messages, so a
        # short "ok" cannot claim another device's "ok, read my email".
        matched = bool(self.sent_text) and any(
            text == self.sent_text or (len(self.sent_text) >= 20 and self.sent_text in text
                                       and len(text) <= len(self.sent_text) + 500)
            for text in texts)
        if not matched:
            # Readable text that isn't ours means another device's message.
            if texts or self.echo_ids or time.monotonic() - self.sent_at > ECHO_WINDOW_S:
                self.foreign_user = True
                return
            self.echo_fallback = True
        self.bind_echo(echo_id, payload)

    def bind_echo(self, echo_id, payload):
        self.ids.add(echo_id)
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        if isinstance(parent, str) and parent and len(parent) <= 256:
            self.ids.add(parent)
        self.echo_ids += 1
        self.chat_ids |= _chat_ids(payload)
        if not self.foreign_user:
            # Messages that began after our acknowledgement but before this
            # echo arrived are answers to ours too.
            for row in self.rows.values():
                if not row.get("early"):
                    row["after_echo"] = True
        # Replies rejected before the echo arrived may belong to it after all.
        self.rejected.clear()
        if self.acked:
            for message_id, row in list(self.rows.items()):
                if self.related(message_id, row):
                    self.publish(message_id, row, force=True)

    def related(self, message_id, row):
        if message_id in self.rejected:
            return False
        # A message in another chat or session is never ours.
        if self.chat_ids and row.get("chat") and not (row["chat"] & self.chat_ids):
            if len(self.rejected) < 32:
                self.rejected.add(message_id)
            return False
        if row["parent"] and row["parent"] in self.ids:
            return True
        # Timing-based binding needs our echo, and no other user's message.
        if (row.get("early") or row.get("after_echo") or not row["parent"]):
            return bool(self.echo_ids) and not self.foreign_user
        seen = {message_id}
        while row["parent"] and self.ids and row["parent"] not in self.ids:
            parent = row["parent"]
            ancestor = self.rows.get(parent)
            if parent in seen or parent in self.rejected or ancestor is None:
                if len(self.rejected) < 32:
                    self.rejected.add(message_id)
                return False
            seen.add(parent)
            row = ancestor
        return True

    def event(self, message):
        if self.done.done():
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            payload = message
        event = message.get("event") or payload.get("event") or payload.get("event_name")
        if event == "delta.presentation":
            self.count(event)
            self.presentation(message, payload)
            return
        if event not in ("agent.status", "task.status", "delta.message_start",
                         "delta.text_append", "delta.message_done", "message.assistant"):
            name = event if isinstance(event, str) and re.fullmatch(r"[a-z][a-z0-9_.]{0,39}", event) else "?"
            self.count("other:" + name)
            self.learn_echo(message, payload)
            return
        self.count(event)
        seq = message.get("seq")
        if type(seq) is int and seq > 0:
            if seq <= self.seq:
                return
            self.seq = seq
        if event in ("agent.status", "task.status"):
            code = payload.get("activity_code") or payload.get("status")
            if not isinstance(code, str):
                return
            self.busy = code not in ("", "online", "idle", "completed", "failed")
            # Only a coarse phase leaves the bridge, not internal reasoning or
            # arbitrary status payloads from other connected applications.
            self.emit({"event": "progress", "phase": "working" if self.busy else "waiting"})
            self.finish_soon()
            return
        if event not in ("delta.message_start", "delta.text_append", "delta.message_done", "message.assistant"):
            return
        message_id = payload.get("message_id") or message.get("message_id") or payload.get("id")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            return
        if message_id not in self.rows and len(self.rows) >= 16:
            return
        row = self.rows.setdefault(message_id, {
            "parent": "", "text": "", "done": False, "early": not self.acked,
            # An assistant message that starts after our echo, with no other
            # user's message in between, answers ours whatever its parent ID.
            "after_echo": bool(self.echo_ids) and not self.foreign_user})
        self.last_content = time.monotonic()
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        if isinstance(parent, str) and parent and len(parent) <= 256:
            row["parent"] = parent
        if _chat_ids(payload):
            row["chat"] = _chat_ids(payload)
        # Keep accumulating an unrelated message: a later echo of our own
        # message can still bind it. Only related messages are published.
        if self.acked and not self.related(message_id, row) and event == "delta.message_start":
            self.count("unrelated")
        if event == "delta.text_append":
            text = payload.get("text")
            if isinstance(text, str):
                row["text"] = (row["text"] + text)[:MAX_REPLY_CHARS]
            row["done"] = False
        elif event in ("delta.message_done", "message.assistant"):
            if event == "message.assistant" and payload.get("display_text_ready") is False:
                return
            if not self.assistant_keys:
                self.assistant_keys = _key_names(payload)
            # Links may sit in structured fields, or only in the streamed text
            # if the final display text drops them; keep both.
            _add_links(row, _payload_links(payload))
            _add_links(row, [{"url": url, "title": ""} for url in _URL.findall(row["text"])])
            for key in ("display_text", "content", "text"):
                if isinstance(payload.get(key), str) and payload[key]:
                    row["text"] = payload[key][:MAX_REPLY_CHARS]
                    break
            row["done"] = True
        if self.acked and self.related(message_id, row):
            self.publish(message_id, row, force=row["done"])

    def publish(self, message_id, row, force=False):
        if not row["text"] and not row.get("links"):
            return
        if self.settle is not None:
            self.settle.cancel()
            self.settle = None
        now = time.monotonic()
        if force or now - self.last_emit >= 0.08:
            event = {"event": "reply", "id": message_id, "text": row["text"], "done": row["done"]}
            if row.get("links"):
                event["links"] = row["links"]
            self.emit(event)
            self.last_emit = now
        if row["done"]:
            self.finish_soon()

    def links(self):
        found = {}
        for message_id, row in self.rows.items():
            if self.related(message_id, row):
                for link in row.get("links", []):
                    found.setdefault(link["url"], link)
        return list(found.values())[:MAX_LINKS]

    def text(self):
        return "\n\n".join(row["text"] for message_id, row in self.rows.items()
                           if row["text"] and self.related(message_id, row))[:MAX_REPLY_CHARS]

    def finish_soon(self):
        """Settle like the ESP32 client: every reply done, then a quiet period.

        A busy agent holds the turn open briefly after its last text, but not
        indefinitely; the text has already streamed to the desktop.
        """
        if self.done.done() or not self.acked:
            return
        rows = [row for message_id, row in self.rows.items() if self.related(message_id, row)]
        if not rows or not all(row["done"] for row in rows) or not any(
                row["text"] or row.get("links") for row in rows):
            return
        delay = SETTLE_S
        if self.busy:
            delay = max(delay, BUSY_HOLD_S - (time.monotonic() - self.last_content))
        if self.settle is not None:
            self.settle.cancel()
        self.settle = asyncio.get_running_loop().call_later(delay, self.finish)

    def finish(self):
        if not self.done.done():
            self.done.set_result(self.text())

    def disconnect(self):
        self.closed = True
        if self.done.done() or not self.acked:
            return
        if any(row["done"] and (row["text"] or row.get("links")) and self.related(message_id, row)
               for message_id, row in self.rows.items()):
            self.finish()
        else:
            self.done.set_exception(ConnectionError("reply connection closed"))

    def close(self):
        if self.settle is not None:
            self.settle.cancel()
        if not self.done.done():
            self.done.cancel()
        elif not self.done.cancelled():
            self.done.exception()  # consume a closure before the send acknowledgement


class _NoRequest:
    def on_frame(self, frame) -> None:
        pass


_NO_REQUEST = _NoRequest()


class _UpgradeRejected(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status
