# Omuseful chat companion

A chat-only Linux Muse device: it lets the Omuseful plugin chat with your Muse
from the Omarchy bar. It registers no commands and refuses any it receives, so
your Muse cannot run programs or touch files on this computer.

It is a modified copy of the Linux part of Meta's
[Muse gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk)
(Apache-2.0, see [LICENSE](LICENSE)), trimmed to chat: the command executor
and file operations are removed.

## Set up

```bash
bash ~/.config/omarchy/plugins/omuseful/companion/omuseful-setup.sh
```

One run installs the companion as a system service, adds you to the
`omuseful` group (access to the local chat socket), asks for your Muse SDK
token with hidden input, and pairs with the Muse app over Bluetooth. Get a
token at <https://gadgets.muse.ai/settings/sdk-tokens> and read the
[SDK terms](https://gadgets.muse.ai/sdk-terms).

```bash
bash omuseful-token.sh              # replace the SDK token
sudo musegadget pair --force        # pair again, required after replacing a token
bash omuseful-setup.sh --uninstall  # remove the service, token, and pairing
```

## What it runs

- `musegadget run` as a root systemd service, sandboxed by
  `src/musegadget/data/omuseful-hardening.conf` (only `CAP_CHOWN` and
  `CAP_FOWNER`, a system-call filter, no home access).
- A local socket, `/run/musegadget/musegadget.sock`, for the `omuseful` group.
- A non-secret status file, `/run/omuseful/status.json`.

VM lookup and token refresh responses are capped at 1 MiB before JSON parsing;
HTTP compression is refused, and redirect/error bodies are discarded without
reading them. The base service has a 256 MiB memory limit, a 192 MiB reclaim
threshold, and no swap allowance. Re-run **Setup → Run setup** after updating
the plugin to apply companion fixes and service limits.

During pairing, the terminal asks you to verify the Muse app and press Enter
before provisioning is allowed. After confirmation, the SDK token is included
in the encrypted pairing confirmation so the phone can register it with Muse.
Community pairing is encrypted but cannot authenticate an active nearby
attacker: pair somewhere private (see [SECURITY.md](../SECURITY.md#pairing)).
The SDK token is recorded only during pairing, not during device-token refresh;
pair again after replacing it. A root-only token hint detects later changes
without exposing the token to the plugin.

The SDK token, device identity, and pairing stay root-only in
`/var/lib/musegadget`. Logs never contain your messages, Muse's replies,
links, or tokens (they do name the device and its Muse VM).
No script is downloaded and run: the installer builds from a private copy of
this folder, takes `uv`, the build backend, and system libraries from the Arch
repositories, and downloads only the pinned, hash-checked `websockets` package
(`data/requirements.lock`).

Pairing is the only step that uses Bluetooth. So that phones pair reliably,
setup sets `ExchangeMTU = 256` in `/etc/bluetooth/main.conf` and turns off
BlueZ's battery plugin; `--uninstall` restores both.

## Changes from upstream

- `service.py`: chat-only registration; local chat socket with streaming;
  bounded local connections; a transient read error never unpairs.
- `link_client.py`: waits for the reply on `/chat/subscribe`, matched to your
  message (and kept to its chat), including link cards.
- `omuseful.py`: status file and chat-socket access for the `omuseful` group.
- `cli.py`, `install.sh`, `data/`: chat-only service, local source, sandbox.
- `pairing.py`: the SDK token is handed to the phone only in the encrypted
  confirmation after local terminal consent; `service.py` no longer refreshes
  solely to try to register a token.
- Removed: `executor.py`, `fileops.py`, and the examples.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest
```

No test needs Bluetooth, credentials, or pairing.
