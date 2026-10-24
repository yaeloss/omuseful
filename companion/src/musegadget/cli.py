# Copyright (c) Meta Platforms, Inc. and affiliates.
# Modified by yaeloss for Omuseful: chat-only service and its setup.
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

"""``musegadget`` command line."""

from __future__ import annotations

import argparse
import logging
import os
import select
import sys
import termios
import threading
import time
from typing import Callable

from musegadget import __version__, config, identity, muse_api, network
from musegadget.ble_setup import Credentials, ProvisionFailed, SetupController
from musegadget.pairing import BUTTON_CONFIRM_TIMEOUT_S, PairingSession

log = logging.getLogger("musegadget")

DEFAULT_SETUP_WINDOW_S = 600
BLE_SHUTDOWN_DELAY_S = 1.5
CONFIRM_POLL_S = 0.2


class _SystemNetwork:
    is_online = staticmethod(network.is_online)
    current_connection_entry = staticmethod(network.current_connection_entry)


class _TerminalConfirmation:
    """Turns Enter in the pairing terminal into confirmation of one session.

    One reader serves every prompt, so no waiter outlives the prompt it was
    started for. A press counts only for the prompt that was showing before
    it was typed and is still showing once it is read, and the controller
    then accepts it only for that prompt's own session generation.
    """

    def __init__(self, fd: int, confirm: Callable[[int], bool]) -> None:
        self._fd = fd
        self._confirm = confirm
        self._lock = threading.Lock()
        self._generation = 0
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read, name="pair-confirm", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def prompt(self, generation: int) -> None:
        try:
            # Keys typed before the prompt must not count as confirming it.
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except (OSError, termios.error):
            pass
        with self._lock:
            self._generation = generation
        print(f"\nA phone asked to pair. If the Muse app on your phone is asking you to "
              f"confirm on the device, press Enter here within {BUTTON_CONFIRM_TIMEOUT_S} "
              "seconds. If it isn't, press nothing.", flush=True)

    def ended(self, generation: int, confirmed: bool) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._generation = 0
        print("Confirmed. Finish setup in the Muse app." if confirmed
              else "That pairing request ended without being confirmed.", flush=True)

    def _showing(self) -> int:
        with self._lock:
            return self._generation

    def _read(self) -> None:
        while not self._stopped.is_set():
            shown = self._showing()
            try:
                ready, _, _ = select.select([self._fd], [], [], CONFIRM_POLL_S)
                if not ready:
                    continue
                data = os.read(self._fd, 1024)
            except OSError:
                return
            if not data:
                return
            self._press(shown, data)

    def _press(self, shown: int, data: bytes) -> None:
        if b"\n" not in data and b"\r" not in data:
            return
        current = self._showing()
        if shown and shown == current:
            self._confirm(shown)
        elif current:
            print("Press Enter again to confirm the request shown above.", flush=True)


def _verify_and_save(credentials: Credentials, commit: Callable[[Callable[[], bool]], bool],
                     sdk_token_hint: str | None = None) -> None:
    # Only Meta's hosts are stored: the service sends its bearer tokens to
    # these endpoints, so an untrusted one from provisioning is dropped.
    api_url = credentials.api_url if muse_api.trusted_api_url(credentials.api_url) else ""
    api_url_v2 = credentials.api_url_v2 if muse_api.trusted_api_url(credentials.api_url_v2) else ""
    vms, status = muse_api.fetch_vms_with_status(
        credentials.access_token, muse_api.api_root(api_url_v2),
    )
    if not vms:
        log.warning("device token check failed (HTTP %s, %d VMs)", status, len(vms))
        raise ProvisionFailed("auth_failed")

    record = {
        "access_token": credentials.access_token,
        "refresh_token": credentials.refresh_token,
        "token_type": "device",
        "username": credentials.username,
        "api_url": api_url,
        "api_url_v2": api_url_v2,
        "noise_host": muse_api.trusted_noise_host(credentials.noise_host),
        "access_token_saved_at": int(time.time()),
    }
    if sdk_token_hint:
        record["sdk_token_hint"] = sdk_token_hint

    def save() -> bool:
        config.save_json(config.PAIRING_FILE, record)
        return True

    if not commit(save):
        raise ProvisionFailed("error_storage")


def cmd_pair(args: argparse.Namespace) -> int:
    if config.load_json(config.PAIRING_FILE) and not args.force:
        print("Already paired. Run `musegadget unpair` first, or pass --force.", file=sys.stderr)
        return 1

    try:
        sdk_token = config.sdk_token()
    except ValueError as exc:
        print(f"Can't pair: {exc}.", file=sys.stderr)
        return 1
    if not sdk_token:
        print("No SDK token yet. Add one first: `bash omuseful-token.sh`.", file=sys.stderr)
        return 1
    if sys.stdin is None or not sys.stdin.isatty():
        print("Run pairing in a terminal: you confirm it there by pressing Enter.",
              file=sys.stderr)
        return 1
    from musegadget.ble_server import BleServer

    ident = identity.load_or_create()
    pairing = PairingSession(
        node_id=ident.node_id,
        device_id=ident.device_id,
        mac=ident.mac,
        firmware_version=__version__,
        sdk_token=sdk_token,
    )
    completed = threading.Event()
    controller: SetupController | None = None
    terminal: _TerminalConfirmation | None = None

    server = BleServer(
        ident.ble_name,
        on_write=lambda packet, peer: controller.on_write(packet, peer),
        on_disconnect=lambda peer: controller.on_disconnect(peer),
    )

    def finish() -> None:
        completed.set()
        threading.Timer(BLE_SHUTDOWN_DELAY_S, server.stop).start()

    terminal = _TerminalConfirmation(
        sys.stdin.fileno(), lambda generation: controller.confirm(generation),
    )
    controller = SetupController(
        pairing=pairing,
        identity=ident,
        version=__version__,
        transport=server,
        network=_SystemNetwork(),
        provision=lambda credentials, commit: _verify_and_save(
            credentials, commit, sdk_token_hint=sdk_token[:12],
        ),
        on_complete=finish,
        on_confirmation_required=terminal.prompt,
        on_confirmation_ended=terminal.ended,
    )
    window = threading.Timer(args.timeout, server.stop)
    window.daemon = True

    print(f"Setup open for {args.timeout // 60} minutes. In the Muse app, add a device")
    print(f"and choose {ident.ble_name}.")
    print("After you confirm here, your SDK token is sent to the phone in the encrypted "
          "pairing confirmation. Pair somewhere private.")
    controller.start()
    terminal.start()
    window.start()
    pairing_marker = None
    try:
        import grp
        from pathlib import Path

        from musegadget.omuseful import CHAT_GROUP, atomic_json
        pairing_marker = Path("/run/omuseful/pairing.json")
        atomic_json(pairing_marker, {"expires_at": time.time() + args.timeout},
                    0o640, grp.getgrnam(CHAT_GROUP).gr_gid)
    except (OSError, KeyError):
        pairing_marker = None
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"Bluetooth setup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        terminal.stop()
        window.cancel()
        controller.stop()
        if pairing_marker is not None:
            pairing_marker.unlink(missing_ok=True)

    if completed.is_set():
        print("Paired.")
        return 0
    print("Setup window closed without pairing.", file=sys.stderr)
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    from musegadget.omuseful import ChatStatus
    from musegadget.service import run_service

    try:
        sdk_token = config.sdk_token()
    except ValueError as exc:
        log.warning("running without an SDK token: %s", exc)
        sdk_token = None
    try:
        chat = ChatStatus()
        chat.prepare()
    except (OSError, KeyError, PermissionError) as exc:
        print(f"Omuseful chat companion is not ready: {exc}", file=sys.stderr)
        return 1
    log.info("musegadget %s: Omuseful chat-only device; no commands are registered", __version__)
    run_service(identity.load_or_create(), chat, sdk_token)
    return 0


def cmd_send_user_msg(args: argparse.Namespace) -> int:
    import json
    import socket

    message = sys.stdin.read() if args.message == ["-"] else " ".join(args.message)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(90)
            sock.connect(str(config.socket_path()))
            request = {"message": message}
            if args.session_id:
                request["session_id"] = args.session_id
            sock.sendall(json.dumps(request).encode() + b"\n")
            reply = json.loads(sock.makefile("rb").readline())
    except (OSError, ValueError) as exc:
        print(f"Could not reach the musegadget service: {exc}", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"Not delivered: {reply.get('error') or reply}", file=sys.stderr)
        return 1
    print("Sent to your Muse.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    ident = identity.load_or_create()
    paired = config.load_json(config.PAIRING_FILE) is not None
    print(f"version:   {__version__}")
    print(f"node id:   {ident.node_id}")
    print(f"BLE name:  {ident.ble_name}")
    print(f"paired:    {'yes' if paired else 'no'}")
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    config.delete_json(config.PAIRING_FILE)
    print("Pairing removed. The device identity is kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musegadget")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="open Bluetooth setup so the Muse app can pair")
    pair.add_argument("--timeout", type=int, default=DEFAULT_SETUP_WINDOW_S,
                      help="seconds to keep setup open (default: %(default)s)")
    pair.add_argument("--force", action="store_true", help="pair even if already paired")
    pair.set_defaults(func=cmd_pair)

    run = sub.add_parser("run", help="stay connected to the Muse as a chat-only device")
    run.set_defaults(func=cmd_run)

    send = sub.add_parser("send-user-msg", help="send a message to your Muse from this device")
    send.add_argument("message", nargs="+", help="the message, or - to read it from stdin")
    send.add_argument("--session-id",
                     help="send to this side chat (a new id starts one) instead of the main chat")
    send.set_defaults(func=cmd_send_user_msg)

    sub.add_parser("info", help="show device identity").set_defaults(func=cmd_info)
    sub.add_parser("unpair", help="forget the saved pairing").set_defaults(func=cmd_unpair)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except PermissionError as exc:
        # The state directory is root-only on an installed device.
        print(f"Can't access {exc.filename or config.state_dir()}; run this with sudo.",
              file=sys.stderr)
        return 1
