# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from musegadget.omuseful import ChatStatus, Paths, atomic_json, read_json


@pytest.fixture
def chat(tmp_path):
    result = ChatStatus(paths=Paths(tmp_path / "run"), gid=os.getgid(), owner_uid=os.getuid())
    result.prepare()
    return result


def test_status_is_chat_only_and_non_secret(chat):
    chat.write_status(paired=True, connected=True, ble_name="MuseGadgetABCDEF",
                      sdk_token_configured=True)
    path = chat.paths.runtime / "status.json"
    row = json.loads(path.read_text())
    assert row["backend"] == "omuseful" and row["chat_only"] is True
    assert row["sdk_token_configured"] is True and abs(row["updated_at"] - time.time()) < 5
    assert not {"account", "account_home", "approval_enabled"} & set(row)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_runtime_directory_is_world_readable_only(chat):
    assert stat.S_IMODE(chat.paths.runtime.stat().st_mode) == 0o755


def test_prepare_refuses_the_wrong_owner(tmp_path):
    with pytest.raises(PermissionError):
        ChatStatus(paths=Paths(tmp_path / "run"), gid=os.getgid(), owner_uid=os.getuid() + 1).prepare()


def test_read_json_refuses_symlinks_and_world_writable_files(chat, tmp_path):
    target = chat.paths.runtime / "pairing.json"
    atomic_json(target, {"expires_at": 1}, 0o640, os.getgid())
    assert read_json(target, gid=os.getgid())["expires_at"] == 1
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_json(link, gid=os.getgid())
    os.chmod(target, 0o646)
    with pytest.raises(ValueError):
        read_json(target, gid=os.getgid())


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"value":NaN}'])
def test_read_json_rejects_noncanonical_json(chat, raw):
    path = chat.paths.runtime / "x.json"
    path.write_text(raw)
    os.chmod(path, 0o640)
    with pytest.raises(ValueError):
        read_json(path, gid=os.getgid())
