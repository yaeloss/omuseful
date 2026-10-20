# SPDX-License-Identifier: Apache-2.0
"""Exercise WebSocket resource limits before Noise or application validation."""
from __future__ import annotations

import asyncio
import struct
import zlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError
from websockets.extensions.permessage_deflate import PerMessageDeflate
from websockets.frames import CloseCode, Frame, Opcode

from musegadget.link_client import DeviceDescription, LinkSession, Outcome
from musegadget.noise import (
    ApplicationResponse, NoiseXXInitiator, NoiseXXResponder, ServiceFrame, encode_noise_frames,
)
from musegadget.noise.transport import encode_response_envelope

WS_LIMIT = 64 * 1024


@pytest.fixture(autouse=True)
def no_loopback_proxy(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def _session():
    return LinkSession(
        noise_host="hatch.metaaivm.com", vm_id="test-vm", vm_auth_token="test-token",
        device=DeviceDescription("test-device", "test", "0.1.0", {}),
        run_command=lambda *args: {"ok": False},
    )


@asynccontextmanager
async def _connection(handler):
    # Use _open's production options, not the injected in-memory test connector.
    async with serve(handler, "127.0.0.1", 0, compression="deflate", close_timeout=1) as server:
        session = _session()
        port = server.sockets[0].getsockname()[1]
        session._url = f"ws://127.0.0.1:{port}/v1/noise?vm_id=test-vm"
        ws = await session._open()
        try:
            yield session, ws
        finally:
            await ws.close()


async def _assert_rejected(session, ws, phase, code, monkeypatch):
    process_noise = Mock(side_effect=AssertionError("hostile message reached Noise"))
    monkeypatch.setattr(NoiseXXInitiator, "read_message2", process_noise)
    if phase == "handshake":
        with pytest.raises(ConnectionClosedError):
            await asyncio.wait_for(session._handshake(ws), 2)
    else:
        session._ws = ws
        session._transport = SimpleNamespace(decrypt_frame=process_noise)
        assert await asyncio.wait_for(session._read_loop(), 2) is Outcome.CLOSED
    assert ws.protocol.close_sent is not None
    assert ws.protocol.close_sent.code == code
    process_noise.assert_not_called()


def test_open_sets_websocket_resource_limits(monkeypatch):
    connect = AsyncMock(return_value=object())
    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    asyncio.run(_session()._open())
    options = connect.call_args.kwargs
    assert options["max_size"] == WS_LIMIT
    assert options["max_queue"] == 4
    assert options["compression"] is None


@pytest.mark.parametrize("phase", ["handshake", "transport"])
@pytest.mark.parametrize("opcode", [Opcode.BINARY, Opcode.TEXT], ids=["binary", "text"])
@pytest.mark.parametrize("attack", ["frame", "fragmented", "length-header"])
def test_oversized_messages_are_rejected_before_noise(phase, opcode, attack, monkeypatch):
    async def scenario():
        async def endpoint(ws):
            if phase == "handshake":
                await ws.recv()  # Noise message 1
            if attack == "length-header":
                # A huge advertised length must fail without waiting for its body.
                wire = struct.pack("!BBQ", 0x80 | opcode, 127, 1 << 40)
            elif attack == "fragmented":
                # Every fragment fits, but their sum doesn't. Never send FIN:
                # rejection must happen during assembly, not after recv().
                chunk = b"x" * (WS_LIMIT // 2)
                wire = b"".join(frame.serialize(mask=False) for frame in (
                    Frame(opcode, chunk, fin=False),
                    Frame(Opcode.CONT, chunk, fin=False),
                    Frame(Opcode.CONT, b"x", fin=False),
                ))
            else:
                wire = Frame(opcode, b"x" * (WS_LIMIT + 1)).serialize(mask=False)
            # Deliberately bypass the server's send API to model a hostile peer.
            ws.transport.write(wire)
            await ws.wait_closed()

        async with _connection(endpoint) as (session, ws):
            await _assert_rejected(session, ws, phase, CloseCode.MESSAGE_TOO_BIG, monkeypatch)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["handshake", "transport"])
def test_compression_is_not_negotiated_or_inflated(phase, monkeypatch):
    decoder = Mock(side_effect=AssertionError("compressed input was inflated"))
    monkeypatch.setattr(PerMessageDeflate, "decode", decoder)
    offers = []

    async def scenario():
        async def endpoint(ws):
            offers.extend(ws.request.headers.get_all("Sec-WebSocket-Extensions"))
            if phase == "handshake":
                await ws.recv()
            compressor = zlib.compressobj(wbits=-15)
            compressed = compressor.compress(b"x" * (WS_LIMIT * 16))
            compressed += compressor.flush(zlib.Z_SYNC_FLUSH)
            wire = bytearray(Frame(Opcode.BINARY, compressed[:-4]).serialize(mask=False))
            wire[0] |= 0x40  # RSV1: permessage-deflate, despite no negotiation
            assert len(wire) < WS_LIMIT
            ws.transport.write(wire)
            await ws.wait_closed()

        async with _connection(endpoint) as (session, ws):
            await _assert_rejected(session, ws, phase, CloseCode.PROTOCOL_ERROR, monkeypatch)
            assert ws.protocol.extensions == []

    asyncio.run(scenario())
    assert offers == []
    decoder.assert_not_called()


@pytest.mark.parametrize("fragmented", [False, True])
def test_valid_noise_handshake_and_large_chunked_response_still_work(fragmented):
    body = b"x" * (WS_LIMIT * 2)

    async def scenario():
        async def endpoint(ws):
            responder = NoiseXXResponder()
            responder.initialize()
            await ws.send(responder.read_message1_and_write_message2(await ws.recv()))
            responder.read_message3(await ws.recv())
            send, _ = responder.split()
            response = ServiceFrame.response(1, ApplicationResponse(
                status=200, body=body, end_body=True,
            ))
            for plain in encode_noise_frames(encode_response_envelope(response), chunk_id=-1):
                encrypted = send.encrypt_with_ad(b"", plain)
                if fragmented:
                    await ws.send([encrypted[:32], encrypted[32:]])
                else:
                    await ws.send(encrypted)
            await ws.wait_closed()

        async with _connection(endpoint) as (session, ws):
            transport = await asyncio.wait_for(session._handshake(ws), 2)
            response = None
            chunks = 0
            while response is None:
                raw = await asyncio.wait_for(ws.recv(), 2)
                assert len(raw) <= WS_LIMIT
                response = transport.decrypt_frame(raw)
                chunks += 1
            assert chunks > 1
            assert response.kind == "response"
            assert response.value.status == 200
            assert response.value.body == body

    asyncio.run(scenario())
