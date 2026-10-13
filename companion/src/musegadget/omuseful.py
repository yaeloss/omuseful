# SPDX-License-Identifier: Apache-2.0
"""Omuseful: a chat-only Muse device for the Omarchy desktop.

This module only publishes a non-secret status file and grants the desktop
chat group access to the local chat socket. Nothing here runs commands.
Production paths are fixed; injectable paths and owners exist for tests.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 1
VERSION = "0.1.2"
CHAT_GROUP = "omuseful"
MAX_JSON = 128 * 1024
USERNAME = re.compile(r"[a-z_][a-z0-9_-]*\$?")


def unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON value")


def atomic_json(path: Path, data: dict, mode: int, gid: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".omuseful-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        if os.geteuid() == 0:
            os.fchown(fd, 0, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(data, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def read_json(path: Path, *, gid: int, owner: int | None = None) -> dict:
    # NONBLOCK prevents a substituted FIFO from holding the root service hostage.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MAX_JSON or info.st_mode & 0o007
                or info.st_gid != gid or (owner is not None and info.st_uid != owner)):
            raise ValueError("unsafe local JSON file")
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError("local JSON file too large")
    decoded = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(decoded, dict):
        raise TypeError("expected a JSON object")
    return decoded


@dataclass(frozen=True)
class Paths:
    runtime: Path = Path("/run/omuseful")


def group_users(gid: int) -> list[str]:
    """Accounts in the chat group, whether or not they have logged in since."""
    names = set(grp.getgrgid(gid).gr_mem)
    names.update(row.pw_name for row in pwd.getpwall() if row.pw_gid == gid)
    return sorted(name for name in names if USERNAME.fullmatch(name) and name != "root")


class ChatStatus:
    """Status heartbeat and chat-socket access for the Omuseful desktop panel."""

    def __init__(self, *, paths: Paths | None = None, gid: int | None = None,
                 owner_uid: int = 0) -> None:
        self.paths = paths or Paths()
        self.gid = grp.getgrnam(CHAT_GROUP).gr_gid if gid is None else gid
        self.owner_uid = owner_uid

    def prepare(self) -> None:
        if os.geteuid() != self.owner_uid:
            raise PermissionError("Omuseful requires its privileged service account")
        runtime = self.paths.runtime
        runtime.mkdir(mode=0o755, parents=True, exist_ok=True)
        info = runtime.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid:
            raise PermissionError("unsafe Omuseful directory ownership")
        if os.geteuid() == 0:
            os.chown(runtime, 0, self.gid)
        os.chmod(runtime, 0o755)

    def refresh_access(self, socket_path: Path) -> None:
        """Let current chat-group members use the socket without logging out.

        The socket is root:omuseful 0660. A desktop session that started before
        its user joined the group lacks the group, so members also get a direct
        ACL on the socket, and only on the socket.
        """
        if os.geteuid() != 0 or shutil.which("setfacl") is None or not socket_path.exists():
            return
        try:
            users = group_users(self.gid)
        except (KeyError, OSError):
            return
        self._setfacl(["-b", str(socket_path)])
        for name in users:
            self._setfacl(["-m", f"u:{name}:rw", str(socket_path)])

    @staticmethod
    def _setfacl(args: list[str]) -> None:
        try:
            subprocess.run(["setfacl", *args], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def write_status(self, *, paired: bool, connected: bool, ble_name: str,
                     pairing: bool = False, sdk_token_configured: bool = False) -> None:
        atomic_json(self.paths.runtime / "status.json", {
            "schema_version": SCHEMA, "backend": "omuseful", "version": VERSION,
            "chat_only": True, "paired": paired, "connected": connected,
            "pairing": pairing, "ble_name": ble_name,
            "sdk_token_configured": sdk_token_configured, "updated_at": time.time(),
        }, 0o644, self.gid)
