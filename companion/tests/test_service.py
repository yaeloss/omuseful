# Copyright (c) Meta Platforms, Inc. and affiliates.
# Modified by yaeloss to test Omuseful's chat-only service.
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
import os
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from musegadget import muse_api
from musegadget.identity import Identity
from musegadget.service import Backoff, Service


class FakeSession:
    registered_at = 1.0

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_chat(self, message: str, session_id: str | None = None,
                        wait_for_reply: bool = False) -> dict:
        self.sent.append((message, session_id, wait_for_reply))
        return {"ok": True, "status": 200, "response": None, "reply": "Synthetic reply"}


async def ask(path: Path, payload: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(payload)
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


def run_with_socket(check) -> None:
    async def scenario():
        # AF_UNIX paths are short on macOS, so avoid pytest's long tmp_path.
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "mg.sock"
            service = Service(identity=Identity("02:00:00:00:00:01"))
            server = await service.serve_local(path)
            try:
                await check(service, path)
            finally:
                server.close()

    asyncio.run(scenario())


def test_local_message_is_forwarded_to_the_live_session():
    async def check(service, path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
        session = FakeSession()
        service._current = session
        reply = await ask(path, json.dumps({"message": "hello from the ring"}).encode() + b"\n")
        assert reply["ok"] and reply["reply"] == "Synthetic reply"
        assert session.sent == [("hello from the ring", None, False)]

    run_with_socket(check)


def test_local_message_can_target_a_side_chat():
    async def check(service, path):
        session = FakeSession()
        service._current = session
        sid = "4f6c7ff7-3406-4a35-8ec9-907f61fc43f3"
        assert (await ask(path, json.dumps({"message": "ring", "session_id": sid}).encode() + b"\n"))["ok"]
        assert session.sent == [("ring", sid, False)]
        bad = await ask(path, b'{"message": "ring", "session_id": "../x"}\n')
        assert not bad["ok"] and len(session.sent) == 1

    run_with_socket(check)


def test_local_stream_sends_progress_before_its_final_reply():
    class StreamingSession(FakeSession):
        async def send_chat(self, message, session_id=None, wait_for_reply=False, on_progress=None):
            assert wait_for_reply and on_progress is not None
            on_progress({"event": "accepted"})
            on_progress({"event": "reply", "id": "one", "text": "Partial", "done": False})
            await asyncio.sleep(0)
            return {"ok": True, "reply": "Final answer"}

    async def check(service, path):
        service._current = StreamingSession()
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"message":"hello","stream":true}\n')
        await writer.drain()
        events = []
        while True:
            row = json.loads(await reader.readline())
            events.append(row)
            if "ok" in row:
                break
        writer.close()
        assert events[0] == {"event": "accepted"}
        assert events[1]["done"] is False
        assert events[2] == {"ok": True, "reply": "Final answer"}

    run_with_socket(check)


def test_local_message_fails_cleanly_when_not_connected():
    async def check(service, path):
        reply = await ask(path, b'{"message": "hi"}\n')
        assert reply == {"ok": False, "delivery": "failed", "error": "not connected to the Muse"}

    run_with_socket(check)


def test_local_message_must_be_non_empty_text():
    async def check(service, path):
        service._current = FakeSession()
        for payload in (b'{"message": ""}\n', b'{"text": "hi"}\n', b'[1]\n'):
            assert not (await ask(path, payload))["ok"]
        reply = await ask(path, b"not json\n")
        assert not reply["ok"] and reply["error"] == "local chat operation failed"

    run_with_socket(check)


def test_backoff_doubles_to_a_ceiling_and_honours_the_floor():
    backoff = Backoff()
    assert [backoff.next_delay() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    backoff.reset()
    backoff.floor = 15
    assert backoff.next_delay() == 15


def test_backoff_stays_at_the_ceiling_through_a_long_outage():
    backoff = Backoff()
    for _ in range(1100):  # 2 ** 1024 no longer fits a float
        delay = backoff.next_delay()
    assert delay == 60


def refresh_with(service_kwargs, pairing, calls=1):
    """Run _maybe_refresh `calls` times on a Service built inside the loop (Python 3.9)."""
    async def scenario():
        service = Service(identity=Identity("02:00:00:00:00:01"),
                          **service_kwargs)
        result = pairing
        for _ in range(calls):
            result = await service._maybe_refresh(result)
        return result
    return asyncio.run(scenario())


def fresh_pairing() -> dict:
    return {"access_token": "a", "refresh_token": "r", "access_token_saved_at": int(time.time())}


def test_a_start_with_an_sdk_token_does_not_refresh_to_report_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(tmp_path))
    calls = []

    monkeypatch.setattr("musegadget.service.muse_api.refresh_device_token",
                        lambda *args: calls.append(args))
    fresh = fresh_pairing()
    assert refresh_with({"sdk_token": "mgst_token"}, fresh, calls=2) is fresh
    assert calls == []


def test_a_start_without_an_sdk_token_keeps_a_fresh_token(monkeypatch):
    monkeypatch.setattr("musegadget.service.muse_api.refresh_device_token",
                        lambda *args: pytest.fail("unexpected refresh"))
    fresh = fresh_pairing()
    assert refresh_with({}, fresh) is fresh


def test_refreshing_a_due_token_does_not_send_the_sdk_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(tmp_path))
    calls = []

    def refresh(*args):
        calls.append(args)
        return {"access_token": "new-a", "refresh_token": "new-r"}, 200

    monkeypatch.setattr("musegadget.service.muse_api.refresh_device_token", refresh)
    old = {**fresh_pairing(), "access_token_saved_at": 0}
    result = refresh_with({"sdk_token": "PRIVATE-SYNTHETIC"}, old)
    assert result["access_token"] == "new-a"
    assert len(calls) == 1 and len(calls[0]) == 3
    assert "PRIVATE-SYNTHETIC" not in str(calls)


def test_changing_a_paired_sdk_token_warns_once_without_logging_tokens(monkeypatch, caplog):
    monkeypatch.setattr("musegadget.service.muse_api.refresh_device_token",
                        lambda *args: pytest.fail("unexpected refresh"))
    token = "mgst_" + "B" * 43
    paired = {**fresh_pairing(), "sdk_token_hint": "mgst_AAAAAAA"}
    refresh_with({"sdk_token": token}, paired, calls=2)
    assert caplog.text.count("SDK token changed since pairing") == 1
    assert "pair --force" in caplog.text and token not in caplog.text


@pytest.mark.parametrize("hint", [None, "mgst_AAAAAAA"])
def test_an_older_or_matching_pairing_does_not_warn_about_a_changed_token(hint, caplog):
    token = "mgst_AAAAAAA" + "A" * 32
    paired = fresh_pairing()
    if hint:
        paired["sdk_token_hint"] = hint
    assert refresh_with({"sdk_token": token}, paired) is paired
    assert "SDK token changed" not in caplog.text


def test_api_root_uses_a_trusted_api_url_v2_or_the_muse_api():
    assert muse_api.api_root("https://edge.muse.ai/") == "https://edge.muse.ai"
    # A pairing may not redirect the device token to an arbitrary host.
    assert muse_api.api_root("https://api.example/") == "https://api.muse.ai"
    assert muse_api.api_root() == "https://api.muse.ai"


def test_local_socket_cannot_store_an_sdk_token(tmp_path, monkeypatch):
    from musegadget import config
    token = "mgst_" + "A" * 42 + "A"
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path))
    service = Service(identity=Identity("02:00:00:00:00:01"),
                      chat=SimpleNamespace())
    reply = asyncio.run(service._local_request(
        json.dumps({"set_sdk_token": token}).encode(),
    ))
    assert reply == {"ok": False, "delivery": "failed", "error": "expected {\"message\": \"...\"}"}
    assert not (tmp_path / config.SDK_TOKEN_FILE).exists()


def test_omuseful_device_is_chat_only():
    from musegadget import service
    assert service.OMUSEFUL_COMMAND_SPECS == {}
    reply = service.refuse_command("system.run", {"command": "id"}, 1000)
    assert reply["ok"] is False and "chat-only" in reply["error"]
    source = Path(service.__file__).read_text()
    assert "commands=OMUSEFUL_COMMAND_SPECS" in source
    assert "run_command = refuse_command" in source


def test_desktop_hangup_cancels_the_pending_turn(tmp_path):
    from musegadget import service as service_module

    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Session:
            registered_at = 1.0

            async def send_chat(self, message, session_id, wait_for_reply=True, on_progress=None):
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        svc = service_module.Service.__new__(service_module.Service)
        svc._current = Session()
        path = Path(tempfile.mkdtemp(prefix="mg-", dir="/tmp")) / "s"
        server = await asyncio.start_unix_server(svc._handle_local, str(path))
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"message": "hi", "stream": true}\n')
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
        writer.close()
        await asyncio.wait_for(cancelled.wait(), 2)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())



def test_service_notices_a_new_pairing_within_seconds():
    from musegadget import service
    assert service.UNPAIRED_POLL_S <= 5
    source = Path(service.__file__).read_text()
    assert "if not waiting_logged:" in source  # the frequent check logs once, not every poll
