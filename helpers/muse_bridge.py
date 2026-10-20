#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Unprivileged, stdlib-only JSON bridge for Omuseful's QML surfaces.

Omuseful is a chat interface to the Muse app. This helper only reads the
companion's non-secret status file and relays chat over its local socket.
It runs no commands, elevates nothing, opens no network listener, and never
reads or accepts credentials.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import shlex
import socket
import stat
import struct
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 1
MAX_JSON = 128 * 1024
MAX_MESSAGE = 60 * 1024
STATUS = Path("/run/omuseful/status.json")
SOCKET = Path("/run/musegadget/musegadget.sock")
STATUS_FIELDS = ("paired", "connected", "pairing", "ble_name", "sdk_token_configured",
                 "sdk_token_needs_pairing", "version")
MAX_REPLY = 16384
# The companion runs as root; never trust a status file or socket owned by anyone else.
TRUSTED_UID = 0

# Bidi marks, embeddings, overrides, and isolates (written as escapes on purpose).
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                           "\u2066\u2067\u2068\u2069")


def unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON value")


def parse(raw: bytes):
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


def read_object(path: Path, limit: int = MAX_JSON) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                or info.st_uid != TRUSTED_UID or info.st_mode & 0o002):
            raise ValueError("unsafe, untrusted, or oversized local file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("local file too large")
    data = parse(raw)
    if not isinstance(data, dict):
        raise TypeError("expected a JSON object")
    return data


def visible_text(value) -> str:
    """Show control characters and bidi overrides instead of rendering them,
    while keeping emoji joiners and other ordinary formatting."""
    return "".join(
        f"\\u{ord(c):04x}"
        if (unicodedata.category(c) == "Cc" and c not in "\n\t") or c in _BIDI_CONTROLS
        else c
        for c in str(value)
    )


_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,2048})\)|(https?://[^\s<>\"'`)\]]{1,2048})")
_INLINE = re.compile(r"`([^`\n]{1,500})`|\*\*([^*\n]{1,500})\*\*|(?<![\w*])[*_]([^*_\n]{1,500})[*_](?![\w*])")
_TRAILING = ".,;:!?"
# What a browser will show as the host: letters, digits, dots, hyphens (an IDN
# arrives as xn--...), or a bracketed IPv6 address. No backslash tricks.
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def safe_url(url) -> str:
    """An http(s) URL with a host, or empty. Only these are ever rendered as links.

    Printable ASCII only: no control or bidi characters (which could reach the
    clipboard) and no Unicode lookalike domains (an IDN must arrive as xn--,
    so the shown domain is the real one). No user@ part, which hides the host.
    """
    if (not isinstance(url, str) or len(url) > 2048 or "\\" in url
            or any(not 0x21 <= ord(c) <= 0x7E for c in url)):
        return ""
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return ""
    host = parts.hostname
    if ":" in host:  # urlsplit strips the brackets of an IPv6 literal
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return ""
    elif not _HOST.fullmatch(host):
        return ""
    return url


def _anchor(url: str, label: str = "") -> str:
    host = urlsplit(url).hostname or ""
    text = html.escape(label or url)
    if label and label.strip() != url:
        # Show where a labelled link really goes.
        text += " <small>(" + html.escape(host) + ")</small>"
    return '<a href="' + html.escape(url, quote=True) + '">' + text + "</a>"


def _inline(text: str) -> str:
    out, last = [], 0
    for match in _INLINE.finditer(text):
        out.append(html.escape(text[last:match.start()]))
        code, bold, italic = match.groups()
        if code is not None:
            out.append("<code>" + html.escape(code) + "</code>")
        elif bold is not None:
            out.append("<b>" + html.escape(bold) + "</b>")
        else:
            out.append("<i>" + html.escape(italic) + "</i>")
        last = match.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def _line(text: str) -> str:
    out, last = [], 0
    for match in _LINK.finditer(text):
        label, target, bare = match.groups()
        url = target or bare
        trail = ""
        if bare:
            while url and url[-1] in _TRAILING:
                trail, url = url[-1] + trail, url[:-1]
        if not safe_url(url):
            continue
        out.append(_inline(text[last:match.start()]))
        out.append(_anchor(url, label or ""))
        out.append(html.escape(trail))
        last = match.end()
    out.append(_inline(text[last:]))
    return "".join(out)


def render_html(text: str) -> str:
    """A small, fully escaped HTML subset for a Muse reply: bold, italic, code,
    bullets, line breaks, and http(s) links. Nothing from the reply is passed
    through as markup, so it cannot add images, styles, or scripts."""
    lines = []
    for raw in str(text).split("\n"):
        stripped = raw.lstrip()
        heading = re.match(r"#{1,6}\s+(.*)", stripped)
        bullet = re.match(r"[-*•]\s+(.*)", stripped)
        if heading:
            lines.append("<b>" + _line(heading.group(1)) + "</b>")
        elif bullet:
            lines.append("&nbsp;&nbsp;• " + _line(bullet.group(1)))
        else:
            lines.append(_line(raw))
    return "<br>".join(lines)


def link_cards(raw, text: str = "") -> list[dict]:
    """Links Muse sent outside its text (e.g. "Open the listing" cards), as
    title + domain cards. Only http(s); links already in the text are skipped."""
    cards = []
    for item in raw if isinstance(raw, list) else []:
        url = item.get("url") if isinstance(item, dict) else None
        if not safe_url(url) or url in text or len(cards) >= 10:
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        host = urlsplit(url).hostname or ""
        cards.append({"url": url, "title": visible_text(title)[:200] or host,
                      "domain": host[4:] if host.startswith("www.") else host})
    return cards


def backend_status(path: Path = STATUS) -> dict:
    try:
        raw = read_object(path)
    except FileNotFoundError:
        return {"available": False, "state": "setup_required",
                "label": "Companion not installed or stopped"}
    except (OSError, ValueError, TypeError):
        return {"available": False, "state": "offline", "label": "Companion status unreadable"}
    if (type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA
            or raw.get("backend") != "omuseful" or raw.get("chat_only") is not True):
        return {"available": False, "state": "incompatible",
                "label": "Chat-only Omuseful companion required"}
    stamp = raw.get("updated_at")
    if type(stamp) not in (int, float) or not -2 <= time.time() - stamp <= 4:
        return {"available": False, "state": "offline", "label": "Companion offline"}
    status = {key: raw[key] for key in STATUS_FIELDS if key in raw}
    for flag in ("paired", "connected", "pairing", "sdk_token_configured", "sdk_token_needs_pairing"):
        status[flag] = raw.get(flag) is True
    if not (isinstance(status.get("version"), str) and len(status["version"]) <= 32):
        status.pop("version", None)
    status["ble_name"] = visible_text(status.get("ble_name", ""))[:64]
    status["available"] = True
    if status["pairing"]:
        status["label"] = "Pairing · " + status["ble_name"]
    elif not status["paired"]:
        status["label"] = "Not paired"
    elif status["connected"]:
        status["label"] = "Connected · " + status["ble_name"]
    else:
        status["label"] = "Paired, offline"
    # This label also reaches the shell-owned bar tooltip. Neutralize markup
    # without relying on the host's Text format; other name displays are plain.
    status["label"] = status["label"].replace("<", "‹").replace(">", "›")
    return status


def chat_access(socket_path: Path = SOCKET) -> bool:
    return os.access(socket_path, os.R_OK | os.W_OK)


# The companion ships inside this plugin, next to the helper's folder.
COMPANION = Path(__file__).resolve().parent.parent / "companion"


def companion_command(script: str) -> str:
    """A copyable command for a bundled companion script (never run here),
    shell-quoted so an unusual install path cannot break or inject into it."""
    path = str(COMPANION / script)
    home = os.path.expanduser("~")
    if path.startswith(home + "/"):
        return 'bash "$HOME"' + shlex.quote(path[len(home):])
    return "bash " + shlex.quote(path)


SETUP_COMMAND = companion_command("omuseful-setup.sh")


def terminal_command(command: str) -> str:
    """What the Run buttons open: the same command, telling the script it runs
    in Omarchy's installer terminal (so a failure doesn't end with "Done!")."""
    return "OMUSEFUL_TERMINAL=1 " + command
TOKEN_URL = "https://gadgets.muse.ai/settings/sdk-tokens"
PAIR_STEPS = ("In the Muse app: Settings → Devices → turn on Developer mode → Add Device (+), "
              "then choose the MuseGadget name shown in the terminal. When the terminal says "
              "a phone requested pairing, verify the app and press Enter there to confirm.")


PAIR_COMMAND = "sudo musegadget pair"


def setup_guide(status: dict, access: bool) -> dict:
    """Plain-language setup, derived only from non-secret status.

    Not installed: one step (get a token, run setup). Installed: add the SDK
    token, then pair with the Muse app. Each step can open Omarchy's floating
    terminal with a fixed command ("run"), or copy it ("command").
    """
    version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", status.get("version", ""))
    if version and tuple(map(int, version.groups())) < (0, 1, 4):
        status = {"available": False, "state": "incompatible"}
    if status.get("available") is not True:
        state = status.get("state")
        body = ("1. Get your SDK token from Muse.\n"
                "2. Click Run setup. It asks for the token and your password, then shows "
                "how to add this computer in the Muse app on your phone.\n\n"
                "Muse can only chat through Omuseful; it can't run anything on this computer.")
        if state == "offline":
            body = "The chat companion isn't responding. Run setup again to repair it."
        elif state == "incompatible":
            body = "An older companion is installed. Run setup again to update it."
        return {"current_step": 0, "complete": False, "steps": [{
            "id": "setup", "title": "Set up Omuseful", "done": False, "body": body,
            "run": terminal_command(SETUP_COMMAND), "run_label": "Run setup",
            "command": SETUP_COMMAND, "command_label": "Copy command", "command_enabled": True,
            "url": TOKEN_URL, "url_label": "Get SDK token",
        }]}

    token = status.get("sdk_token_configured") is True
    paired = status.get("paired") is True
    pairing = status.get("pairing") is True
    token_changed = status.get("sdk_token_needs_pairing") is True
    if pairing:
        pair_body = "Pairing is open. " + PAIR_STEPS.replace(
            "the MuseGadget name shown in the terminal", status.get("ble_name") or "the name shown in the terminal")
    elif token_changed:
        pair_body = "Your SDK token changed. Pair again to register it with Muse. " + PAIR_STEPS
    elif paired:
        pair_body = "Paired with the Muse app."
    else:
        pair_body = "Have your phone nearby. " + PAIR_STEPS
    can_pair = token and (not paired or token_changed) and not pairing
    pair_command = PAIR_COMMAND + " --force" if token_changed else PAIR_COMMAND
    steps = [
        {"id": "token", "title": "Add your SDK token", "done": token,
         "body": "Get a token from Muse, click Add token, and paste it when asked "
                 "(it stays hidden). Never paste it anywhere else.",
         "run": terminal_command(companion_command("omuseful-token.sh")), "run_label": "Add token",
         "command": companion_command("omuseful-token.sh"),
         "command_label": "Copy command", "command_enabled": True,
         "url": TOKEN_URL, "url_label": "Get SDK token",
         "secondary_url": "https://gadgets.muse.ai/sdk-terms", "secondary_url_label": "Token terms"},
        {"id": "pair", "title": "Pair with the Muse app", "done": paired and not token_changed, "body": pair_body,
         "run": pair_command if can_pair else "", "run_label": "Pair again" if token_changed else "Pair",
         "command": pair_command, "command_label": "Copy command", "command_enabled": can_pair,
         "disabled_hint": "Add your SDK token first." if not token else ""},
    ]
    return {"current_step": 0 if not token else 1,
            "complete": token and paired and not token_changed and status.get("connected") is True and access,
            "steps": steps}


def snapshot(status_path: Path = STATUS, socket_path: Path = SOCKET) -> dict:
    status = backend_status(status_path)
    access = status["available"] and chat_access(socket_path)
    error = ""
    if status["available"] and not access:
        error = "Chat access missing. Log out and back in, then reopen Omuseful."
    return {"status": status, "error": error, "setup": setup_guide(status, access)}


class DeliveryError(ValueError):
    """An interrupted send, with a conservative account of its delivery."""

    def __init__(self, message: str, delivery: str) -> None:
        super().__init__(message)
        self.delivery = delivery


def delivery_error(delivery: str) -> dict:
    # Never expose raw exceptions, server errors, or response bodies to the UI.
    errors = {
        "failed": "Message not sent. Edit it and try again when Muse is connected.",
        "uncertain": "Delivery could not be confirmed. Check the Muse app before resending.",
        "accepted": "Muse accepted the message, but the reply connection stopped. Check the Muse app.",
    }
    return {"ok": False, "delivery": delivery, "error": errors[delivery]}


def send(message, on_event, status_path: Path = STATUS, socket_path: Path = SOCKET) -> dict:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is empty")
    if not backend_status(status_path).get("connected"):
        raise ValueError("Muse is not connected")
    body = json.dumps({"message": message, "stream": True}).encode() + b"\n"
    if len(body) > MAX_MESSAGE:
        raise ValueError("message is too long")
    delivery, accepted, has_reply = "failed", False, False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(335)
            sock.connect(str(socket_path))
            # Talk only to the root companion, never to an impostor on that path.
            _, peer_uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                 struct.calcsize("3i")))
            if peer_uid != TRUSTED_UID:
                raise ValueError("chat socket is not owned by the companion")
            # A partial write or a missing acknowledgement is not proof of
            # failure. Never encourage an automatic retry of an ambiguous send.
            delivery = "uncertain"
            sock.sendall(body)
            with sock.makefile("rb") as stream:
                for _ in range(10000):
                    raw = stream.readline(MAX_JSON + 1)
                    if len(raw) > MAX_JSON or not raw.endswith(b"\n"):
                        raise ValueError("invalid service reply")
                    reply = parse(raw)
                    if not isinstance(reply, dict):
                        raise ValueError("invalid service reply")
                    if "ok" in reply:
                        if type(reply["ok"]) is not bool:
                            raise ValueError("invalid delivery result")
                        break
                    event = reply.get("event")
                    if event == "accepted":
                        accepted = True
                        on_event({"event": "accepted"})
                    elif event == "progress" and reply.get("phase") in ("working", "waiting"):
                        on_event({"event": "progress", "phase": reply["phase"]})
                    elif (event == "reply" and isinstance(reply.get("id"), str)
                          and 0 < len(reply["id"]) <= 256 and isinstance(reply.get("text"), str)):
                        # Bound what reaches the shell's main thread, after escaping.
                        text = visible_text(reply["text"][:MAX_REPLY])[:MAX_REPLY]
                        links = link_cards(reply.get("links"), text)
                        if text or links:
                            accepted, has_reply = True, True
                        on_event({"event": "reply", "id": reply["id"], "text": text,
                                  "html": render_html(text), "links": links,
                                  "done": reply.get("done") is True})
                else:
                    raise ValueError("too many service events")
    except BrokenPipeError:
        # A lost stdout consumer is handled by main just like other hangups.
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise DeliveryError(str(exc), "accepted" if accepted else delivery) from exc
    if reply.get("ok") is not True:
        status = reply.get("status")
        rejected = (reply.get("delivery") == "failed"
                    or (type(status) is int and 400 <= status < 500 and status != 408))
        return delivery_error("accepted" if accepted else "failed" if rejected else "uncertain")
    result = {"ok": True}
    for key in ("reply", "warning"):
        if isinstance(reply.get(key), str) and reply[key]:
            result[key] = visible_text(reply[key][:MAX_REPLY])[:MAX_REPLY]
    if "reply" in result:
        result["html"] = render_html(result["reply"])
    links = link_cards(reply.get("links"), result.get("reply", ""))
    if "reply" in result or links:
        result["links"] = links
    has_reply = has_reply or bool(result.get("reply") or links)
    result["delivery"] = "complete" if has_reply and not result.get("warning") else "accepted"
    return result


def emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("watch", "send"))
    args = parser.parse_args()
    try:
        if args.action == "watch":
            while True:
                emit(snapshot())
                time.sleep(1)
        else:
            line = sys.stdin.buffer.readline(MAX_JSON + 1)
            if len(line) > MAX_JSON:
                raise ValueError("request too large")
            data = parse(line)
            if not isinstance(data, dict):
                raise TypeError("expected a JSON object")
            emit(send(data.get("message"), emit))
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except DeliveryError as exc:
        emit(delivery_error(exc.delivery))
        return 1
    except (OSError, ValueError, TypeError):
        emit(delivery_error("failed"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
