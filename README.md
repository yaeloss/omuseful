# Omuseful

**Chat with your Muse agent from the Omarchy bar.**

Omuseful is a small Omarchy shell plugin: a bar icon and a chat panel for the
Muse app. Muse does its work in its own app and cloud, as it always does.
Omuseful only shows the connection status and relays your messages and Muse's
replies. **Nothing runs on this computer on Muse's behalf.**

Omuseful is possible because Meta published the Muse gadget SDK as open source (Apache-2.0). The companion in companion/ is the SDK’s Linux device, trimmed to chat only. 

**Thanks to the Muse team for opening it up to the community.**

> Independent community project, not made or endorsed by Meta or Omarchy.

## How it works

```text
Omuseful panel ──▶ local helper ──▶ chat-only companion ──▶ Muse app / cloud
 (bar + chat)      (Python, stdlib)  (Unix socket, BLE-paired)
```

- **Plugin:** QML for the bar icon, chat panel, and setup guide, plus one
  stdlib Python helper. The helper reads the companion's non-secret status
  file and sends chat over its local socket. It runs no commands, opens no
  network ports, and never touches credentials.
- **Companion ([`companion/`](companion/README.md)):** a chat-only Linux Muse
  device, included in this repository and set up once with one command. It
  registers no commands with Muse and refuses any that arrive, so Muse cannot
  run programs or read/write files here. It holds your SDK token, root-only.

## Requirements

- Omarchy with Quickshell shell plugins (tested on Omarchy 4.0.4, Quickshell
  0.3.1, Qt 6.11, Arch x86_64).
- `/usr/bin/python3` (3.9+) and `wl-copy` (Omarchy ships both).
- A Muse account, the Muse app on your phone, and your own SDK token
  ([terms](https://gadgets.muse.ai/sdk-terms)).
- For the companion's one-time setup: `sudo` and a Bluetooth adapter (used
  only to pair). Its packages come from the Arch repositories.

## Install

```bash
omarchy plugin add https://github.com/yaeloss/omuseful.git --enable
```

Review the source before enabling; plugins run unsandboxed inside the shell.
Enabling the plugin installs nothing else, needs no `sudo`, and changes no
system settings.

### First-time setup (about two minutes)

1. Click the chat-bubble icon in the bar, then **Set up**.
2. Click **Get SDK token** and create a token on the Muse site.
3. Click **Run setup**. A terminal opens: press Enter, type your password,
   and paste the token when asked (it stays hidden).
4. Follow the on-screen steps to add this computer in the Muse app on your
   phone. When it says **Paired**, you're done.

Setup installs a small system service. Pairing is the only step that uses
Bluetooth; chat then goes over the internet. To run it by hand instead:

```bash
bash ~/.config/omarchy/plugins/omuseful/companion/omuseful-setup.sh
```

## Usage

Click the chat-bubble icon in the bar. It is dimmed until Muse is connected.

- **Chat:** type and press Enter (Shift+Enter for a new line). Replies stream
  in with basic formatting and clickable links, and also appear in the Muse
  app. **Copy** puts a message (or a card's link) on the clipboard. While Muse is replying, the send button
  becomes **Stop**.
- **Setup:** shows what is left to do. **Run setup**, **Add token**, and
  **Pair** open a terminal that runs the matching script from the plugin's
  `companion/` folder; each asks before changing anything.

To open the panel from a keybinding:

```bash
omarchy-shell shell toggle omuseful
```

## Configure

There are no settings. To move the icon:

```bash
omarchy bar move omuseful --section right
```

## Remove

Uninstall the companion first (its uninstaller lives in the plugin folder),
then remove the plugin:

```bash
bash ~/.config/omarchy/plugins/omuseful/companion/omuseful-setup.sh --uninstall
omarchy plugin remove omuseful
```

The uninstaller removes the service, the SDK token, and the pairing, and
restores the system settings setup changed. Also remove the device in the
Muse app (Settings > Devices).

## Privacy

- Messages go from the panel through the local companion to your Muse account
  over Meta's existing encrypted connection, under
  [Muse's privacy policy](https://muse.ai/privacy).
- The panel keeps at most 20 messages in memory. They are never written to
  disk and are cleared when the shell or plugin reloads.
- The plugin never sees your SDK token; the guide shows only whether one is
  configured. No analytics, telemetry, or hosted relay.
- Copied text is visible to other clipboard users and clipboard history tools.

See [SECURITY.md](SECURITY.md) for the threat model.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff -e companion
.venv/bin/python -m pytest                      # plugin + companion contract
.venv/bin/python -m pytest companion/tests      # companion
.venv/bin/ruff check helpers tests
omarchy plugin validate .
qmllint -I "$OMARCHY_PATH/shell" *.qml
```

To try a local checkout, copy it (not a symlink) into
`~/.config/omarchy/plugins/omuseful/`, then run
`omarchy-shell shell rescanPlugins`. Before a release, run the manual check in
[SECURITY.md](SECURITY.md#release-checklist).

## License

The plugin is MIT, including the original chat-bubble mark, which is not
derived from any Muse or Jollybot artwork. The [`companion/`](companion/)
folder is a modified part of Meta's Muse gadget SDK and stays Apache-2.0
([companion/LICENSE](companion/LICENSE)). Muse's names and branding belong to
their owners. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
