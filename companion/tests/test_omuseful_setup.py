# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import configparser
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from musegadget import cli
from musegadget.identity import Identity


def test_pairing_requires_token_before_bluetooth_is_imported(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_json", lambda name: None)
    monkeypatch.setattr(cli.config, "sdk_token", lambda: None)
    assert cli.cmd_pair(argparse.Namespace(force=False, timeout=600)) == 1
    assert "omuseful-token.sh" in capsys.readouterr().err


def test_pair_command_wires_the_private_token_into_pairing_without_real_bluetooth(monkeypatch, capsys):
    token = "mgst_" + "A" * 43
    captured = {}
    monkeypatch.setattr(cli.config, "load_json", lambda name: None)
    monkeypatch.setattr(cli.config, "sdk_token", lambda: token)
    monkeypatch.setattr(cli.identity, "load_or_create", lambda: Identity("02:00:00:00:00:01"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 0))

    def controller(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(start=lambda: None, stop=lambda: None, confirm=lambda generation: True)

    def server(*args, **kwargs):
        return SimpleNamespace(run=lambda: captured["on_complete"](), stop=lambda: None)

    class Timer:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    def no_system_group(name):
        # Prevent both writes and cleanup against the real /run/omuseful.
        raise KeyError(name)

    monkeypatch.setitem(sys.modules, "musegadget.ble_server", SimpleNamespace(BleServer=server))
    monkeypatch.setattr(cli, "SetupController", controller)
    monkeypatch.setattr(cli.threading, "Timer", Timer)
    monkeypatch.setattr("grp.getgrnam", no_system_group)
    monkeypatch.setattr(cli, "_TerminalConfirmation", lambda *args: SimpleNamespace(
        start=lambda: None, stop=lambda: None, prompt=lambda generation: None,
        ended=lambda generation, confirmed: None,
    ))
    saved = []
    monkeypatch.setattr(cli, "_verify_and_save", lambda *args, **kwargs: saved.append(kwargs))
    assert cli.cmd_pair(argparse.Namespace(force=False, timeout=600)) == 0
    assert captured["pairing"]._sdk_token == token
    captured["provision"](None, None)
    assert saved == [{"sdk_token_hint": token[:12]}]
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_provision_saves_only_a_token_hint_in_root_only_pairing_state(tmp_path, monkeypatch):
    token = "mgst_" + "A" * 43
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.muse_api, "fetch_vms_with_status", lambda *args: ([{"vm_id": "synthetic"}], 200))
    credentials = cli.Credentials("access", "refresh", "synthetic", "", "https://api.muse.ai", "")
    cli._verify_and_save(credentials, lambda save: save(), sdk_token_hint=token[:12])
    path = tmp_path / "pairing.json"
    assert json.loads(path.read_text())["sdk_token_hint"] == token[:12]
    assert token not in path.read_text() and path.stat().st_mode & 0o777 == 0o600


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
    assert "install.sh --from . --chat-only --setup-user" in source
    assert "--run-as" not in source and "useradd" not in source
    assert "--no-pair" not in source  # setup ends by pairing with the Muse app


def test_setup_delegates_access_changes_to_installer_after_source_check():
    source = (Path(__file__).parents[1] / "omuseful-setup.sh").read_text()
    assert "groupadd" not in source and "usermod" not in source
    assert "omuseful-hardening.conf" not in source


@pytest.mark.parametrize("with_dropin", [False, True])
def test_installed_service_has_a_memory_budget(tmp_path, with_dropin):
    companion = Path(__file__).parents[1]
    data = companion / "src/musegadget/data"
    source = (companion / "install.sh").read_text()
    install_service = source[source.index("install_service() {"):source.index("\nwait_for_bluetooth()")]
    unit = tmp_path / "musegadget.service"
    # Exercise the installer's real unit-copy step without privileges or a
    # running service. The base unit must be bounded even without --setup-user.
    script = ('as_root() { "$@"; }\nsay() { :; }\nsystemd_running() { return 1; }\n'
              f'DATA_DIR="{data}"\nUNIT="{unit}"\n' + install_service + '\ninstall_service\n')
    subprocess.run(["bash", "-euc", script], check=True, capture_output=True, timeout=10)
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(unit)
    if with_dropin:
        settings.read(data / "omuseful-hardening.conf")
    service = settings["Service"]
    assert service["MemoryMax"].endswith("M")
    assert 0 < int(service["MemoryHigh"][:-1]) < int(service["MemoryMax"][:-1]) <= 256
    assert service["MemorySwapMax"] == "0"
    assert service["OOMPolicy"] == "kill"
    assert service["Restart"] == "always"


def test_source_is_checked_before_the_sdk_token_prompt():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    main = source[source.index("main() {"):]
    assert main.index("prepare_source\n") < main.index("prompt_sdk_token\n")
    assert main.index("prepare_source\n") < main.index("install_packages\n")
    assert main.index("prepare_source\n") < main.index("enable_bluez\n")
    assert main.index("prepare_source\n") < main.index("configure_omuseful_access\n")


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
    assert 'BUILD_TMP="$(as_root mktemp -d /tmp/omuseful-build.XXXXXX)"' in source
    assert '--no-build-isolation "$BUILD_TMP"' in source
    assert '--reinstall "$SOURCE"' not in source


def test_installer_trusts_only_packaged_tools_and_local_source():
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert "UV_BIN=/usr/bin/uv" in source and 'stat -c %u "$UV_BIN"' in source
    assert "--no-build-isolation" in source and "python-setuptools" in source
    assert "as_root python3 -I -" in source and "as_root python3 - " not in source
    assert '[ -d "$SOURCE" ] || die' in source
    assert "trap cleanup_build EXIT" in source


def test_installer_gates_a_changed_source_against_the_last_install():
    """Setup installs this folder as a root service, so a source tree that
    changed since the last install needs an explicit confirmation."""
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    assert 'SOURCE_HASH_FILE="$STATE_DIR/source.sha256"' in source
    assert "verify_source_hash" in source and "record_source_hash" in source
    assert "changed since it was last installed" in source
    assert "--setup-user USER" in source
    assert 'if [ "$ASSUME_YES" = 1 ]; then' in source  # --yes stays unattended


def _gate_harness(tmp_path):
    """The real hash/verify functions from install.sh, with root stubbed out."""
    source = (Path(__file__).parents[1] / "install.sh").read_text()
    functions = source[source.index("source_tree_hash() {"):source.index("\ninstall_musegadget()")]
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "mod.py").write_text("x = 1\n")
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "harness.sh"
    script.write_text(
        'as_root() { "$@"; }\n'
        'warn() { printf "%s\\n" "$*" >&2; }\n'
        'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
        f'SOURCE_HASH_FILE="{state}/source.sha256"\nSTATE_DIR="{state}"\n'
        + functions
        + '\ncase "$1" in\n'
        + f'  hash) source_tree_hash "{tree}" ;;\n'
        + '  record) record_source_hash "$2" ;;\n'
        + '  verify) ASSUME_YES="$2" verify_source_hash "$3" ;;\n'
        + "esac\n")
    return script, tree


def _run_gate(script, *args, detached=False):
    command = (["setsid"] if detached else []) + ["bash", str(script), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)


def test_installer_gate_behaviour(tmp_path):
    script, tree = _gate_harness(tmp_path)
    first = _run_gate(script, "hash")
    assert first.returncode == 0 and len(first.stdout.strip()) == 64
    assert _run_gate(script, "hash").stdout == first.stdout  # deterministic
    # First install: nothing recorded, so nothing is gated yet.
    assert _run_gate(script, "verify", "0", first.stdout.strip(), detached=True).returncode == 0
    assert _run_gate(script, "record", first.stdout.strip()).returncode == 0
    # Unchanged tree passes without confirmation; a changed one does not.
    assert _run_gate(script, "verify", "0", first.stdout.strip(), detached=True).returncode == 0
    (tree / "pkg" / "mod.py").write_text("x = 2\n")
    changed = _run_gate(script, "hash").stdout.strip()
    assert changed != first.stdout.strip()
    refused = _run_gate(script, "verify", "0", changed, detached=True)
    assert refused.returncode != 0 and "changed since it was last installed" in refused.stderr
    # --yes is the documented unattended waiver.
    waived = _run_gate(script, "verify", "1", changed, detached=True)
    assert waived.returncode == 0 and "Continuing because --yes" in waived.stderr
    # A symlinked file is refused: its target could be swapped after hashing.
    (tree / "pkg" / "link.py").symlink_to(tree / "pkg" / "mod.py")
    linked = _run_gate(script, "hash")
    assert linked.returncode != 0 and "symlink in the source tree" in linked.stderr


def test_generated_caches_do_not_trigger_the_source_gate(tmp_path):
    script, tree = _gate_harness(tmp_path)
    initial = _run_gate(script, "hash").stdout
    for name in (".pytest_cache", ".ruff_cache", "__pycache__", ".venv"):
        cache = tree / name
        cache.mkdir()
        (cache / "changed-by-a-tool").write_text("generated content\n")
        (cache / "tool-link").symlink_to(cache / "changed-by-a-tool")
    assert _run_gate(script, "hash").stdout == initial


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
