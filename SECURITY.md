# Security

Omuseful is a chat interface. The goals: **Muse can't run anything on this
computer through Omuseful**, and the plugin never handles credentials.

What does run locally: this plugin (QML inside the Omarchy shell, plus one
unprivileged Python helper) and the chat companion in [`companion/`](companion/),
a sandboxed root system service that relays chat to Muse. The plugin never
installs or starts the companion; you run its setup yourself, once.

## What the plugin does

| Action | How | Bounds |
|---|---|---|
| Read companion status | `/run/omuseful/status.json`: `O_NOFOLLOW`, regular file, owned by root, not world-writable, ≤128 KiB, strict JSON | Only known, non-secret fields are kept |
| Send chat | JSON line to `/run/musegadget/musegadget.sock`; the peer must be root (`SO_PEERCRED`) | ≤60 KiB message; ≤128 KiB per reply line; ≤16 KiB of reply text shown |
| Copy text | `wl-copy`, text on stdin | Never in arguments or a shell |
| Run setup | Omarchy's floating installer terminal, running a fixed script from `companion/` (setup, token, or `sudo musegadget pair`) | Only commands the setup guide offers; the script asks to continue and `sudo` asks for your password |
| Open links | Setup: fixed `https://gadgets.muse.ai/` pages. Replies: `http(s)` links, only when clicked | Checked in the helper and again in QML |

The helper runs as `/usr/bin/python3 -I -B` (isolated: no user site-packages,
`PYTHON*` variables, or script-directory imports) and uses only the standard
library. Every process is started with a fixed argument list; nothing goes
through a shell.

## How replies are shown

The helper escapes all reply text, then builds a small HTML subset itself:
bold, italic, code, bullets, headings, and links. Nothing from a reply is
passed through as markup, so a reply cannot add images, styles, scripts, or
anything that loads from the network. Control and bidi characters are shown
as `\uXXXX`.

A text becomes a link only if it is an `http(s)` URL made of printable ASCII,
with a host and no `user@` part. That rules out lookalike Unicode domains
(an IDN arrives as `xn--…`), hidden control characters, and links that would
copy terminal escape sequences to the clipboard. A labelled link also shows
its real domain, and link cards show the domain with the end kept visible
(`…evil.example`). Links open in your browser only when you click them.

## What it never does

- Run commands for Muse, approve actions, or write files.
- Use `sudo`, `pkexec`, setuid tools, or change groups or permissions.
- Open network connections or listeners of its own.
- Read, accept, store, or display an SDK token or pairing secret.
- Log or persist chat. The panel keeps at most 20 messages in memory.

## The companion's boundary

The companion registers an empty command list with Muse and refuses any
command it is sent, so the remote side cannot run programs or touch files.
The command executor and file operations from Meta's SDK are not included at
all. It runs as a root service so it can hold the SDK token privately, with
systemd sandboxing (`companion/src/musegadget/data/omuseful-hardening.conf`):
only `CAP_CHOWN` and `CAP_FOWNER`, a system-call filter, no device or home
access. Its setup builds from a private copy of this folder, takes `uv`, the
build backend, and libraries from the Arch repositories, and downloads only the
pinned, hash-checked `websockets` package; no script is downloaded and run.
Install only this chat-only build. A stock Muse gadget registers `system.run`
and file commands and writes no Omuseful status, so the plugin never treats
it as ready. Don't pair a stock gadget on a machine you care about.

Access to the chat socket is limited to the `omuseful` group (plus a
per-user ACL so members don't have to log out). Any process running as a
member can send chat as you, including other shell plugins, since all
plugins share one unsandboxed process.

## Reporting

Report vulnerabilities privately through GitHub security advisories on this
repository. Never post tokens, pairing files, or real chats in issues.

## Release checklist

CI runs the plugin tests, the companion tests, and `ruff check helpers tests`.

The marketplace's automated baseline should report no findings, and
`review-required` for capabilities that are accurately present: the companion's
installer, package installs, `sudo`, and its systemd service.

Before each release, on an Omarchy machine:

- `omarchy plugin validate .` and `qmllint -I "$OMARCHY_PATH/shell" *.qml`.
- A secret scan of the working tree and the full git history.
- Click to open and close the panel; Escape closes it;
  `omarchy-shell shell toggle omuseful` works.
- Disable, re-enable, `omarchy restart shell`, then `omarchy plugin remove`:
  no leftover helper processes (`pgrep -af muse_bridge`).
- With the companion stopped, offline, or stock: the icon dims, setup shows
  the right step, and chat is disabled.
- In the Muse app, the paired device lists no commands.

Licensing and terms (not legal advice; recheck before publishing):

- Each user brings their own SDK token; never ship one in code, examples,
  screenshots, or release assets.
- Don't imply an official Meta or Omarchy product. No trademark search has
  been done for the name "Omuseful".
- The companion release must keep the upstream Apache-2.0 license and
  notices, and must not include the Jollybot avatar, which Meta excludes from
  that license.
