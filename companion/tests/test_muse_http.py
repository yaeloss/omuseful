# SPDX-License-Identifier: Apache-2.0
"""Exercise urllib redirects and the real HTTP body parser with hostile input.

Only the HTTPS transport is replaced. The opener, error/redirect handlers and
http.client.HTTPResponse framing run unchanged, without external credentials or
network. The wire stream fails immediately on any unbounded body read.
"""
from __future__ import annotations

import asyncio
import gzip
import http.client
import io
import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from musegadget import config, muse_api
from musegadget.identity import Identity
from musegadget.service import Service

LIMIT = 1024 * 1024
TOKENS = {"access_token": "new-access", "refresh_token": "new-refresh"}
VM = {
    "vm_ws_url": "wss://hatch.metaaivm.com/test",
    "vm_auth_token": "vm-secret", "vm_name": "test", "vm_id": "test-id", "default": True,
}


@pytest.fixture(params=["fetch", "refresh"])
def api(request):
    if request.param == "fetch":
        return SimpleNamespace(
            call=lambda: muse_api.fetch_vms_with_status("access"),
            payload={"vm_list": [VM]},
            result=([{
                "vm_url": VM["vm_ws_url"], "vm_auth_token": VM["vm_auth_token"],
                "vm_name": "test", "vm_id": "test-id", "is_default": True,
            }], 200),
            empty=[],
        )
    return SimpleNamespace(
        call=lambda: muse_api.refresh_device_token("refresh", "test-device"),
        payload={"payload": TOKENS}, result=(TOKENS, 200), empty=None,
    )


class _WireStream(io.BytesIO):
    bytes_read = 0

    def read(self, size=-1):
        assert 0 < size <= 64 * 1024, "unbounded or oversized HTTP body read"
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def _wire(body=b"", *, headers=b"", status=200):
    return f"HTTP/1.1 {status} Test\r\n".encode() + headers + b"\r\n" + body


def _chunked(body, chunk_size=8191, *, end=True):
    chunks = []
    for start in range(0, len(body), chunk_size):
        chunk = body[start:start + chunk_size]
        chunks.append(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
    return b"".join(chunks) + (b"0\r\n\r\n" if end else b"")


def _transport(monkeypatch, *responses):
    wires = iter(responses)
    streams, requests = [], []

    def https_open(handler, req):
        requests.append(req)
        stream = _WireStream(next(wires))
        streams.append(stream)
        sock = SimpleNamespace(makefile=lambda *args: stream)
        response = http.client.HTTPResponse(sock, method=req.get_method())
        response.begin()
        # These attributes are normally set by AbstractHTTPHandler.do_open.
        response.url = req.full_url
        response.msg = response.reason
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    return streams, requests


@pytest.mark.parametrize("framing", ["length", "eof", "chunked"])
@pytest.mark.parametrize("at_limit", [False, True])
def test_valid_api_json_including_exact_limit(api, framing, at_limit, monkeypatch):
    body = json.dumps(api.payload).encode()
    if at_limit:
        body += b" " * (LIMIT - len(body))
    headers = b"Content-Encoding: identity\r\n"
    if framing == "length":
        headers += f"Content-Length: {len(body)}\r\n".encode()
    elif framing == "chunked":
        headers += b"Transfer-Encoding: chunked\r\n"
        body = _chunked(body)
    streams, requests = _transport(monkeypatch, _wire(body, headers=headers))
    assert api.call() == api.result
    assert streams[0].closed
    assert requests[0].get_header("Accept-encoding") == "identity"
    assert requests[0].timeout == 15


@pytest.mark.parametrize("framing", ["eof", "chunked", "huge-chunk", "false-length"])
def test_overflow_stops_before_json_or_end_of_body(api, framing, monkeypatch, caplog):
    # More than the budget remains unread, including an unfinished chunked
    # response. A tiny Content-Length cannot bypass chunked-body accounting.
    body = b"x" * (LIMIT + 8192)
    headers = b""
    if framing != "eof":
        headers = b"Transfer-Encoding: chunked\r\n"
        if framing == "false-length":
            headers += b"Content-Length: 1\r\n"
        body = (b"10000000000\r\n" + body if framing == "huge-chunk"
                else _chunked(body, end=False))
    streams, _ = _transport(monkeypatch, _wire(body, headers=headers))
    parse = Mock(side_effect=AssertionError("oversized body reached JSON decoding"))
    monkeypatch.setattr(muse_api.json, "loads", parse)
    assert api.call() == (api.empty, None)
    # http.client consumes each completed chunk's CRLF separately from its
    # body bytes. Neither these delimiters nor chunk headers enter the JSON.
    delimiters = 2 * ((LIMIT + 1) // 8191) if framing in ("chunked", "false-length") else 0
    assert streams[0].bytes_read == LIMIT + 1 + delimiters
    assert streams[0].closed
    assert "exceeds 1 MiB" in caplog.text
    parse.assert_not_called()


@pytest.mark.parametrize("length", [str(LIMIT + 1), str(1 << 60), "9" * 5000, "-1", "invalid"])
def test_bad_advertised_length_is_rejected_without_reading(api, length, monkeypatch):
    streams, _ = _transport(monkeypatch, _wire(headers=f"Content-Length: {length}\r\n".encode()))
    parse = Mock(side_effect=AssertionError("invalid response reached JSON decoding"))
    monkeypatch.setattr(muse_api.json, "loads", parse)
    assert api.call() == (api.empty, None)
    assert streams[0].bytes_read == 0 and streams[0].closed
    parse.assert_not_called()


@pytest.mark.parametrize("encoding", [b"gzip", b"deflate", b"br", b"identity\r\nContent-Encoding: gzip"])
def test_encoded_bodies_are_rejected_before_reading_or_decoding(api, encoding, monkeypatch):
    body = gzip.compress(b"x" * (LIMIT * 4))
    assert len(body) < LIMIT
    streams, _ = _transport(monkeypatch, _wire(body, headers=b"Content-Encoding: " + encoding + b"\r\n"))
    parse = Mock(side_effect=AssertionError("encoded body reached JSON decoding"))
    monkeypatch.setattr(muse_api.json, "loads", parse)
    assert api.call() == (api.empty, None)
    assert streams[0].bytes_read == 0 and streams[0].closed
    parse.assert_not_called()


@pytest.mark.parametrize("body", [b"\xffSYNTHETIC-SECRET", b'{"secret":'], ids=["encoding", "json"])
def test_malformed_json_fails_without_logging_body(api, body, monkeypatch, caplog):
    streams, _ = _transport(monkeypatch, _wire(body))
    assert api.call() == (api.empty, None)
    assert streams[0].closed
    assert "invalid API response JSON" in caplog.text
    assert "SYNTHETIC-SECRET" not in caplog.text


def test_deeply_nested_json_does_not_crash_the_caller(api, monkeypatch):
    streams, _ = _transport(monkeypatch, _wire(b"[" * 10000 + b"]" * 10000))
    result, status = api.call()
    # Python versions differ in the JSON decoder's nesting limit. Either an
    # unusable response or a parsed value of the wrong type is a clean failure.
    assert result == api.empty and status in (None, 200)
    assert streams[0].closed


def test_truncated_chunked_body_fails_cleanly(api, monkeypatch):
    streams, _ = _transport(monkeypatch, _wire(
        b"100\r\n{", headers=b"Transfer-Encoding: chunked\r\n",
    ))
    assert api.call() == (api.empty, None)
    assert streams[0].closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_allowed_redirect_bodies_are_not_drained(status, monkeypatch):
    # No body is sent despite an enormous advertised length. Following the
    # redirect must work without reading or waiting for those bytes.
    streams, requests = _transport(
        monkeypatch,
        _wire(status=status, headers=(
            b"Location: https://api.muse.ai/next\r\nContent-Length: 1099511627776\r\n"
        )),
        _wire(b'{"vm_list": []}'),
    )
    assert muse_api.fetch_vms_with_status("access") == ([], 200)
    assert len(requests) == 2
    assert requests[1].get_header("Authorization") == "Bearer access"
    assert streams[0].bytes_read == 0
    assert all(stream.closed for stream in streams)


def test_refresh_redirect_body_is_not_drained(monkeypatch):
    streams, _ = _transport(
        monkeypatch,
        _wire(status=303, headers=b"Location: /next\r\nTransfer-Encoding: chunked\r\n"),
        _wire(json.dumps(TOKENS).encode()),
    )
    assert muse_api.refresh_device_token("refresh", "test-device") == (TOKENS, 200)
    assert streams[0].bytes_read == 0
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("target", [b"https://evil.example/x", b"http://api.muse.ai/x",
                                    b"https://api.muse.ai:8443/x"])
def test_rejected_redirects_close_body_without_forwarding_credentials(api, target, monkeypatch):
    streams, requests = _transport(monkeypatch, _wire(
        status=302, headers=b"Location: " + target + b"\r\nContent-Length: 1099511627776\r\n",
    ))
    assert api.call() == (api.empty, 302)
    assert len(requests) == 1
    assert streams[0].bytes_read == 0 and streams[0].closed


def test_redirect_loops_remain_bounded(api, monkeypatch):
    redirect = _wire(status=302, headers=b"Location: /loop\r\nTransfer-Encoding: chunked\r\n")
    streams, _ = _transport(monkeypatch, *([redirect] * 6))
    assert api.call() == (api.empty, 302)
    assert len(streams) == 5
    assert all(stream.bytes_read == 0 and stream.closed for stream in streams)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_error_status_survives_without_reading_body(api, status, monkeypatch):
    streams, _ = _transport(monkeypatch, _wire(
        status=status, headers=b"Content-Length: 1099511627776\r\n",
    ))
    assert api.call() == (api.empty, status)
    assert streams[0].bytes_read == 0 and streams[0].closed


@pytest.mark.parametrize("force", [False, True])
def test_oversized_refresh_preserves_saved_credentials(force, tmp_path, monkeypatch):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path))
    pairing = {"access_token": "old-access", "refresh_token": "old-refresh", "access_token_saved_at": 0}
    config.save_json(config.PAIRING_FILE, pairing)
    saved = (tmp_path / config.PAIRING_FILE).read_bytes()
    _transport(monkeypatch, _wire(json.dumps(TOKENS).encode() + b" " * LIMIT))

    async def scenario():
        service = Service(identity=Identity("02:00:00:00:00:01"))
        return await service._maybe_refresh(pairing, force=force)

    assert asyncio.run(scenario()) == (None if force else pairing)
    assert (tmp_path / config.PAIRING_FILE).read_bytes() == saved
