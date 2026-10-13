# SPDX-License-Identifier: MIT
"""Contract between the plugin's helper and the bundled companion (companion/).

Run with PYTHONPATH=companion/src (as CI does); skipped if it is not importable.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

companion = pytest.importorskip("musegadget.omuseful", reason="companion not installed")
service = pytest.importorskip("musegadget.service")

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
