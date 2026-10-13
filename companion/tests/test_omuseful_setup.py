# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import pytest

from musegadget import cli


def test_pairing_requires_token_before_bluetooth_is_imported(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_json", lambda name: None)
    monkeypatch.setattr(cli.config, "sdk_token", lambda: None)
    assert cli.cmd_pair(argparse.Namespace(force=False, timeout=600)) == 1
    assert "omuseful-token.sh" in capsys.readouterr().err


def test_private_token_helper_help_does_not_require_privileges():
    script = Path(__file__).parents[1] / "omuseful-token.sh"
    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "root-only" in result.stdout


def test_private_token_helper_refuses_noninteractive_invocation():
    script = Path(__file__).parents[1] / "omuseful-token.sh"
    result = subprocess.run(["bash", str(script)], input="", capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "interactive terminal" in result.stderr


def test_token_helper_does_not_take_a_token_through_arguments():
    script = Path(__file__).parents[1] / "omuseful-token.sh"
    result = subprocess.run(["bash", str(script), "SYNTHETIC-NOT-A-TOKEN"],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "argument" in result.stderr
    assert "SYNTHETIC-NOT-A-TOKEN" not in result.stderr


def test_token_helper_source_keeps_secret_out_of_arguments_and_environment():
    source = (Path(__file__).parents[1] / "omuseful-token.sh").read_text()
    assert source.index("set +x") < source.index("read -r -s")
    assert "trap 'unset TOKEN' EXIT" in source
    assert "export TOKEN" not in source and "--sdk-token" not in source
    assert 'printf \'%s\\n\' "$TOKEN" | sudo install' in source
    assert "-m 0600 /dev/stdin /var/lib/musegadget/sdk_token" in source


def test_setup_is_chat_only_and_needs_no_execution_account():
    source = (Path(__file__).parents[1] / "omuseful-setup.sh").read_text()
    assert "install.sh --from . --chat-only" in source
    assert "--run-as" not in source and "useradd" not in source
    assert "--no-pair" not in source  # setup ends by pairing with the Muse app


def test_setup_help_explains_chat_only():
    script = Path(__file__).parents[1] / "omuseful-setup.sh"
    result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "cannot run" in result.stdout and "SDK token" in result.stdout


def test_installer_is_chat_only_local_and_downloads_nothing():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert "run any command" not in source and "RUN_AS" not in source
    for pattern in ("curl", "wget", "git+", "| sh", "| bash"):
        assert pattern not in source, pattern
    assert 'DEFAULT_SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"' in source
    assert "--require-hashes" in source


def test_installer_never_builds_inside_the_plugin_folder():
    """A root build in the plugin folder leaves root-owned files that block
    `omarchy plugin remove` and updates."""
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert 'build_src="$(as_root mktemp -d /tmp/omuseful-build.XXXXXX)"' in source
    assert '--no-build-isolation "$build_src"' in source
    assert '--reinstall "$SOURCE"' not in source


def test_installer_trusts_only_packaged_tools_and_local_source():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert "UV_BIN=/usr/bin/uv" in source and 'stat -c %u "$UV_BIN"' in source
    assert "--no-build-isolation" in source and "python-setuptools" in source
    assert "as_root python3 -I -" in source and "as_root python3 - " not in source
    assert '[ -d "$SOURCE" ] || die' in source
    assert "trap cleanup_build EXIT" in source


def test_installer_refuses_a_token_argument():
    script = Path(__file__).parents[1] / "install.sh"
    result = subprocess.run(["bash", str(script), "--sdk-token", "SYNTHETIC-NOT-A-TOKEN"],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "argument" in result.stderr
    assert "SYNTHETIC-NOT-A-TOKEN" not in result.stderr + result.stdout


def test_uninstall_restores_bluetooth_settings():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert 'as_root cp -p "$BLUEZ_CONF.pre-musegadget" "$BLUEZ_CONF"' in source
    assert 'as_root rm -f "$BLUEZ_DROPIN"' in source


# Set by the environment, bash, or /etc/os-release (sourced by install.sh).
_KNOWN = {"HOME", "USER", "SUDO_USER", "PATH", "BASH_SOURCE", "PRETTY_NAME", "ID", "ID_LIKE",
          "EDITOR", "REPLY", "OMUSEFUL_TERMINAL"}


@pytest.mark.parametrize("name", ["install.sh", "omuseful-setup.sh", "omuseful-token.sh"])
def test_scripts_only_read_variables_they_set(name):
    """`set -u` aborts on an unset variable; catch that before a user does."""
    source = (Path(__file__).parents[1] / name).read_text()
    # Ignore embedded Python (quoted heredocs) and comments.
    source = re.sub(r"<<'(\w+)'\n.*?\n\1\n", "\n", source, flags=re.S)
    source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    used = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", source))
    assigned = set(re.findall(r"(?:^|[\s;(])(?:local\s+|export\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=", source, re.M))
    assigned |= set(re.findall(r"\blocal\s+([A-Za-z_][A-Za-z0-9_ ]*)", source, re.M)) and {
        word for line in re.findall(r"\blocal\s+([^\n=]*)", source) for word in line.split()}
    assigned |= set(re.findall(r"\bread\b[^\n]*?\s([A-Za-z_][A-Za-z0-9_]*)\s*(?:<|\|\||$)", source, re.M))
    assigned |= set(re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", source))
    missing = used - assigned - _KNOWN
    assert not missing, f"{name} reads unset variables: {sorted(missing)}"


def test_pairing_waits_for_bluetooth_after_a_restart():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    pair = source[source.index("pair() {"):]
    assert pair.index("wait_for_bluetooth") < pair.index("musegadget pair ||")
    assert "Powered: yes" in source
