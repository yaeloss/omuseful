# SPDX-License-Identifier: MIT
"""Run the actual in-memory QML state machine without Quickshell or credentials."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_chat_state_in_qml_runtime():
    # Arch also ships a Qt 5 runner at /usr/bin/qmltestrunner. Prefer Qt 6,
    # matching Quickshell and the version used in the QML CI job.
    runner = "/usr/lib/qt6/bin/qmltestrunner"
    if not Path(runner).is_file():
        runner = shutil.which("qmltestrunner6") or shutil.which("qmltestrunner")
    if not runner:
        pytest.skip("Qt Quick Test is not installed; the QML CI job runs this suite")
    result = subprocess.run(
        [runner, "-input", str(Path(__file__).parent / "qml")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
             # tst_LinkSafety.qml loads the shared vectors from a local file.
             "QML_XHR_ALLOW_FILE_READ": "1"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
