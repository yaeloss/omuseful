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
# Modified by yaeloss for Omuseful: chat-only device, status, and opt-in local chat events.

"""Keep a paired device connected to its Muse.

Each round fetches the leased VMs with the device token (which also yields a
fresh per-VM bearer), connects to the default VM and serves commands until
the connection ends. Failures back off exponentially. A session that stayed
up for a while resets the backoff. The device token is rotated before it
expires, and immediately if the API rejects it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field

from musegadget import __version__, config, muse_api
from musegadget.omuseful import ChatStatus
from musegadget.identity import Identity
from musegadget.link_client import DeviceDescription, LinkSession, Outcome

log = logging.getLogger(__name__)

# Chat-only: nothing is advertised, and anything the Muse sends anyway is refused.
OMUSEFUL_COMMAND_SPECS: dict = {}


def refuse_command(command, params, timeout_ms=None) -> dict:
    return {"ok": False, "error": "This Omuseful device is chat-only; it runs no commands."}


DEFAULT_NOISE_HOST = muse_api.NOISE_HOST
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
AUTH_BACKOFF_MIN_S = 15.0
HEALTHY_SESSION_S = 30.0
UNPAIRED_POLL_S = 3.0  # connect within seconds of pairing
MAX_LOCAL_REQUEST = 64 * 1024
MAX_LOCAL_CLIENTS = 8
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
# Device access tokens live about 4 hours; rotate at 3.
TOKEN_REFRESH_AGE_S = 3 * 3600
TOKEN_RETRY_S = 300


@dataclass
class Backoff:
    failures: int = 0
    floor: float = 0.0

    def next_delay(self) -> float:
        # The delay saturates at BACKOFF_MAX_S after a handful of failures, so
        # cap the exponent: 2 ** failures stops converting to a float at 1024,
        # about 17 hours into an outage at the 60 s ceiling.
        delay = min(BACKOFF_BASE_S * 2.0 ** min(self.failures, 16), BACKOFF_MAX_S)
        self.failures += 1
        return max(delay, self.floor)

    def reset(self) -> None:
        self.failures = 0
        self.floor = 0.0


@dataclass
class Service:
    identity: Identity
    sdk_token: str | None = None
    chat: "ChatStatus | None" = None
    display_name: str = field(default_factory=socket.gethostname)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _last_refresh_attempt: float = float("-inf")
    # The SDK token reaches Muse only in refresh bodies until apps forward it at
    # mint, so each start with a token attempts one refresh to report it.
    _sdk_token_report_attempted: bool = False
    _current: LinkSession | None = None
    _session_stop: asyncio.Event | None = None
    _command_cancelled: threading.Event | None = None
    _local_clients: int = 0

    def stop(self) -> None:
        self._stop.set()
        if self._session_stop is not None:
            self._session_stop.set()

    async def run(self) -> None:
        backoff = Backoff()
        waiting_logged = False
        while not self._stop.is_set():
            pairing = config.load_json(config.PAIRING_FILE)
            if not pairing:
                if not waiting_logged:  # polled every few seconds; say it once
                    log.info("not paired; run `musegadget pair` to set up")
                    waiting_logged = True
                await self._sleep(UNPAIRED_POLL_S)
                continue
            waiting_logged = False
            pairing = await self._maybe_refresh(pairing)
            if pairing is None:
                await self._sleep(TOKEN_RETRY_S)
                continue

            api = muse_api.api_root(pairing.get("api_url_v2", ""))
            vms, status = await asyncio.to_thread(
                muse_api.fetch_vms_with_status, pairing["access_token"], api,
            )
            if status == 401:
                log.warning("device token rejected by the API; refreshing")
                if await self._maybe_refresh(pairing, force=True) is None:
                    await self._sleep(TOKEN_RETRY_S)
                continue
            vm = next((v for v in vms if v["is_default"]), vms[0] if vms else None)
            if vm is None:
                await self._sleep(backoff.next_delay())
                continue

            outcome, lasted = await self._session(vm, pairing)
            if outcome is Outcome.STOPPED:
                return
            if outcome is Outcome.UNPAIRED:
                config.delete_json(config.PAIRING_FILE)
                log.warning("pairing removed; run `musegadget pair` to set up again")
                continue
            if lasted >= HEALTHY_SESSION_S:
                backoff.reset()
            if outcome in (Outcome.AUTH_REJECTED, Outcome.FORBIDDEN):
                backoff.floor = AUTH_BACKOFF_MIN_S
            delay = backoff.next_delay()
            log.info("reconnecting in %.0fs", delay)
            await self._sleep(delay)

    async def _session(self, vm: dict, pairing: dict) -> tuple[Outcome, float]:
        device = DeviceDescription(
            node_id=self.identity.node_id,
            display_name=self.display_name,
            version=__version__,
            # Omuseful is a chat interface to the Muse app. It registers no
            # commands, so the Muse cannot run anything on this computer.
            commands=OMUSEFUL_COMMAND_SPECS,
        )
        cancelled = threading.Event()
        session_stop = asyncio.Event()
        self._command_cancelled = cancelled
        self._session_stop = session_stop
        if self._stop.is_set():
            session_stop.set()
        run_command = refuse_command
        session = LinkSession(
            noise_host=muse_api.trusted_noise_host(pairing.get("noise_host")),
            vm_id=vm["vm_id"] or vm["vm_name"],
            vm_auth_token=vm["vm_auth_token"],
            device=device,
            run_command=run_command,
        )
        log.info("connecting to %s", vm["vm_name"] or vm["vm_id"])
        started = time.monotonic()
        self._current = session
        try:
            self._write_status(connected=False)
            outcome = await session.run(session_stop)
            if outcome is Outcome.STOPPED and not self._stop.is_set():
                outcome = Outcome.UNPAIRED
        except Exception as exc:
            log.warning("session failed: %s: %s", type(exc).__name__, exc)
            outcome = Outcome.CLOSED
        finally:
            cancelled.set()
            self._current = None
            self._session_stop = None
            self._command_cancelled = None
            self._write_status(connected=False)
        lasted = time.monotonic() - (session.registered_at or time.monotonic())
        log.info("session ended: %s after %.0fs", outcome.value, time.monotonic() - started)
        return outcome, lasted

    def _write_status(self, *, connected: bool, pairing: bool = False) -> None:
        if self.chat is None:
            return
        try:
            self.chat.write_status(
                paired=config.load_json(config.PAIRING_FILE) is not None,
                connected=connected,
                pairing=pairing,
                ble_name=self.identity.ble_name,
                sdk_token_configured=bool(self.sdk_token),
            )
        except OSError:
            log.warning("could not update Omuseful status", exc_info=True)

    def _paired(self) -> bool:
        """Only a missing pairing file means unpaired. A transient read error
        (e.g. too many open files) must never end the pairing."""
        try:
            (config.state_dir() / config.PAIRING_FILE).stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    async def watch_status(self) -> None:
        chat = self.chat
        while not self._stop.is_set():
            try:
                await self._watch_once(chat)
            except Exception:
                log.warning("status update failed; retrying", exc_info=True)
            await self._sleep(1)

    async def _watch_once(self, chat) -> None:
        paired = self._paired()
        connected = paired and self._current is not None and self._current.registered_at is not None
        if not paired and self._command_cancelled is not None:
            self._command_cancelled.set()
            if self._session_stop is not None:
                self._session_stop.set()
        pairing = False
        try:
            from musegadget.omuseful import read_json
            marker = read_json(chat.paths.runtime / "pairing.json", gid=chat.gid, owner=0)
            pairing = marker.get("expires_at", 0) > time.time()
        except (OSError, ValueError, TypeError):
            pass
        self._write_status(connected=connected, pairing=pairing)

    async def _maybe_refresh(self, pairing: dict, force: bool = False) -> dict | None:
        """Return current pairing, rotating tokens first if they are due.

        Returns None only when a due refresh failed and the old token should
        not be used yet; the pairing file is removed when the pairing itself
        has been revoked.
        """
        age = time.time() - pairing.get("access_token_saved_at", 0)
        report_due = bool(self.sdk_token) and not self._sdk_token_report_attempted
        due = force or age >= TOKEN_REFRESH_AGE_S
        if not due and not report_due:
            return pairing
        if not force and time.monotonic() - self._last_refresh_attempt < TOKEN_RETRY_S:
            return pairing
        self._last_refresh_attempt = time.monotonic()
        if report_due:
            self._sdk_token_report_attempted = True
            log.info("refreshing device token to report the SDK token")
        tokens, status = await asyncio.to_thread(
            muse_api.refresh_device_token,
            pairing["refresh_token"], self.identity.node_id,
            muse_api.api_root(pairing.get("api_url_v2", "")),
            self.sdk_token,
        )
        if tokens:
            pairing = {
                **pairing,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "access_token_saved_at": int(time.time()),
            }
            config.save_json(config.PAIRING_FILE, pairing)
            log.info("device token rotated")
            return pairing
        if not due:
            # Only reporting the SDK token: nothing has rejected the current
            # token, so a refusal here must never unpair the device.
            log.warning("SDK token report refresh failed (HTTP %s); keeping the pairing", status)
            return pairing
        if status == 401:
            config.delete_json(config.PAIRING_FILE)
            log.error("pairing revoked; run `musegadget pair` to set up again")
            return None
        # Transient failure: keep using the current token while it still works.
        return None if force else pairing

    # -- Local socket -----------------------------------------------------------

    async def serve_local(self, path) -> asyncio.AbstractServer:
        """Accept messages for the Muse from programs on this device.

        Each connection sends one JSON line, ``{"message": "..."}`` plus an
        optional ``"session_id"`` naming a side chat, and gets one JSON line
        back. ``"stream": true`` opts into bounded JSON-line progress events
        followed by the same final reply. Only the omuseful chat group may
        connect; no network listener is opened.
        """
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = await asyncio.start_unix_server(self._handle_local, str(path),
                                                 limit=MAX_LOCAL_REQUEST)
        if os.geteuid() == 0 and self.chat is not None:
            os.chown(path, 0, self.chat.gid)
        os.chmod(path, 0o660)
        if self.chat is not None:
            self.chat.refresh_access(path)
        log.info("accepting messages on %s", path)
        return server

    async def _handle_local(self, reader, writer) -> None:
        if self._local_clients >= MAX_LOCAL_CLIENTS:
            writer.close()  # bounded: a flood of idle connections cannot exhaust files
            return
        self._local_clients += 1
        try:
            await self._serve_local_client(reader, writer)
        finally:
            self._local_clients -= 1

    async def _serve_local_client(self, reader, writer) -> None:
        def progress(event):
            # A slow/disconnected desktop must not stall the encrypted link or
            # grow its write buffer without bound. No chat data enters logs.
            if writer.is_closing() or writer.transport.get_write_buffer_size() > MAX_LOCAL_REQUEST * 2:
                return
            writer.write(json.dumps(event).encode() + b"\n")

        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            request = asyncio.create_task(self._local_request(line, on_progress=progress))
            # The desktop never half-closes while it waits, so EOF means it gave
            # up: cancel the turn instead of holding the chat lock for minutes.
            async def disconnected():
                try:
                    while await reader.read(1024):
                        pass  # ignore stray bytes; only EOF or a reset counts
                except (ConnectionError, OSError):
                    pass
            hangup = asyncio.create_task(disconnected())
            done, _ = await asyncio.wait({request, hangup}, return_when=asyncio.FIRST_COMPLETED)
            if request not in done:
                request.cancel()
                log.info("desktop disconnected; cancelled its pending chat turn")
                try:
                    await request
                except BaseException:
                    pass
                writer.close()
                return
            hangup.cancel()
            reply = request.result()
        except Exception:
            reply = {"ok": False, "error": "local chat operation failed"}
        writer.write(json.dumps(reply).encode() + b"\n")
        try:
            await asyncio.wait_for(writer.drain(), 3)
        finally:
            writer.close()

    async def _local_request(self, line: bytes, on_progress=None) -> dict:
        request = json.loads(line)
        message = request.get("message") if isinstance(request, dict) else None
        if not isinstance(message, str) or not message.strip():
            return {"ok": False, "error": "expected {\"message\": \"...\"}"}
        session_id = request.get("session_id")
        if session_id is not None and not (
            isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id)
        ):
            return {"ok": False, "error": "session_id must be letters, digits and dashes"}
        session = self._current
        if session is None or session.registered_at is None:
            return {"ok": False, "error": "not connected to the Muse"}
        log.info("forwarding a %d-character message to the Muse", len(message))
        if request.get("stream") is True and on_progress is not None:
            return await session.send_chat(message, session_id, wait_for_reply=True,
                                           on_progress=on_progress)
        # Upstream behaviour for send-user-msg: one final line once Muse accepts it.
        return await session.send_chat(message, session_id, wait_for_reply=False)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass


def run_service(identity: Identity, chat: "ChatStatus", sdk_token: str | None = None) -> None:
    async def main() -> None:
        service = Service(identity=identity, chat=chat, sdk_token=sdk_token)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, service.stop)
        server = await service.serve_local(config.socket_path())
        watcher = asyncio.create_task(service.watch_status())
        try:
            await service.run()
        finally:
            if service._command_cancelled is not None:
                service._command_cancelled.set()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            service._write_status(connected=False)
            server.close()
            await server.wait_closed()

    asyncio.run(main())
