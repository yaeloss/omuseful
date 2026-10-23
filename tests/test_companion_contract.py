# SPDX-License-Identifier: MIT
"""Contract between the plugin's helper and the bundled companion (companion/).

Run with PYTHONPATH=companion/src (as CI does); skipped if it is not importable.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest

companion = pytest.importorskip("musegadget.omuseful", reason="companion not installed")
service = pytest.importorskip("musegadget.service")
top = pytest.importorskip("musegadget", reason="companion not installed")

spec = importlib.util.spec_from_file_location("contract_bridge", Path(__file__).parents[1] / "helpers" / "muse_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_companion_status_is_read_as_chat_only(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "TRUSTED_UID", os.getuid())  # test files are not root-owned
    chat = companion.ChatStatus(paths=companion.Paths(tmp_path / "run"),
                                gid=os.getgid(), owner_uid=os.getuid())
    chat.prepare()
    chat.write_status(paired=True, connected=True, ble_name="MuseGadgetABCDEF")
    status = bridge.backend_status(chat.paths.runtime / "status.json")
    assert status["available"] and status["connected"]


def test_companion_registers_no_commands_and_refuses_any():
    assert service.OMUSEFUL_COMMAND_SPECS == {}
    assert service.refuse_command("system.run", {"command": "id"})["ok"] is False


def test_released_versions_agree():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text())["version"]
    with open(root / "companion" / "pyproject.toml", "rb") as file:
        packaged = tomllib.load(file)["project"]["version"]
    assert manifest == packaged == top.__version__ == companion.VERSION
    current = tuple(map(int, manifest.split(".")))
    assert current >= bridge.MIN_COMPANION_VERSION


def test_bridge_message_fits_companion_request_limit():
    assert bridge.MAX_MESSAGE < service.MAX_LOCAL_REQUEST


def test_token_change_projects_only_a_boolean_into_the_plugin(tmp_path, monkeypatch):
    token = "mgst_" + "B" * 43
    hint = "mgst_AAAAAAA"
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(tmp_path / "private"))
    monkeypatch.setattr(bridge, "TRUSTED_UID", os.getuid())
    service.config.save_json(service.config.PAIRING_FILE, {"sdk_token_hint": hint})
    chat = companion.ChatStatus(paths=companion.Paths(tmp_path / "run"),
                                gid=os.getgid(), owner_uid=os.getuid())
    chat.prepare()
    backend = service.Service(identity=service.Identity("02:00:00:00:00:01"), chat=chat, sdk_token=token)
    backend._write_status(connected=True)
    raw = (chat.paths.runtime / "status.json").read_text()
    assert token not in raw and hint not in raw
    status = bridge.backend_status(chat.paths.runtime / "status.json")
    assert status["sdk_token_needs_pairing"] is True
    assert "sdk_token_hint" not in json.dumps(status)
    guide = bridge.setup_guide(status, True)
    assert not guide["complete"] and guide["steps"][1]["run"] == "sudo musegadget pair --force"
