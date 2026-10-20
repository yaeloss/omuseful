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
| Run setup | Omarchy's floating installer terminal, running a fixed script from `companion/` (setup, token, or `sudo musegadget pair`, optionally `--force` for a changed token) | Only commands the setup guide offers; the script asks to continue and `sudo` asks for your password |
| Open links | Setup: fixed `https://gadgets.muse.ai/` pages. Replies: `http(s)` links, only when clicked | Checked in the helper and again in QML |
| Reply notification | Opt-in `omarchy notification send`, normal urgency, fixed headline/body and fixed `omarchy-shell shell summon omuseful` click action | No chat, link, credential, or remote command enters the notification or its argv; DND/snooze stays under Omarchy's control |
| Save notification preference | Omarchy's scoped `updateEntryInline` API | Only this widget's non-secret `notifyReplies` setting, on an explicit button click |

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

- Run commands or write files at Muse's request, or approve Muse actions.
- Use `sudo`, `pkexec`, setuid tools, or change groups or permissions.
- Open network connections or listeners of its own.
- Read, accept, store, or display an SDK token or pairing secret.
- Log or persist chat. The panel keeps at most 20 messages in memory.

The notification preference is saved by the shell, not the helper. Generic
notification text may remain in the shell's notification history. Unread counts,
delivery states, and drafts are memory-only and reset with the plugin. A failed
send can be explicitly restored as a draft; uncertain sends are never retried
automatically, since a missing acknowledgement does not prove non-delivery.

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

Incoming WebSocket messages are limited to 64 KiB by the WebSocket parser,
before the Noise handshake or decryption. This includes the combined size of
all fragments: oversized messages close the connection with code 1009, even
before the final fragment arrives. WebSocket compression is disabled, so
compressed frames are rejected without inflation. A 4-frame receive-queue
high-water mark applies backpressure. Larger application responses still use
multiple bounded Noise chunks and remain subject to their downstream limits.

Access to the chat socket is limited to the `omuseful` group (plus a
per-user ACL so members don't have to log out). Any process running as a
member can send chat as you, including other shell plugins, since all
plugins share one unsandboxed process.

Setup installs this plugin's `companion/` folder as that root service, and
the folder is writable by anything running as you. The installer therefore
records a hash of the installed source and asks for explicit confirmation
before installing a changed tree; only confirm when you expected an update.
The first install (or the first after `--purge`) has no recorded hash to
compare against, so it is trusted as-is: install only a checkout you
obtained and inspected yourself. `--yes` waives the changed-tree
confirmation (with a warning) for unattended installs, so don't use it for
interactive setup. Generated caches do not affect this hash. Symlinks in
the checked source are refused, since a link
target outside the installer's private build copy could be swapped after
hashing.

## Pairing

Bluetooth pairing uses the Muse gadget SDK's community mode
(`pairing_auth: "none"`): the setup records are encrypted with an
unauthenticated ECDH exchange, so they are safe from passive observers but
not from an active man-in-the-middle in Bluetooth range during the pairing
window. The phone must finish the encrypted handshake, and the person at the
computer must press Enter in the pairing terminal before provisioning is
allowed. Only after that confirmation, the SDK token is included in the
encrypted `pairing_confirmed` record sent to the phone. This is required by the
SDK: the phone presents it when minting the device token. Refreshing a device
token does not register or replace an SDK token.

Local confirmation is consent, not cryptographic authentication of the phone.
An active nearby man-in-the-middle could obtain the SDK token or device
credentials. Pair at home or somewhere you control, keep the window (10 minutes,
only while `sudo musegadget pair` is running) short, and revoke the SDK token and
pair again if you suspect interference. Replacing or revoking the paired SDK
token requires `sudo musegadget pair --force`; the pairing stores only its
12-character hint alongside the root-only device credentials. Neither the full
token nor its hint is published in the non-secret status file; the UI receives
only a boolean indicating a changed token.
Endpoints the pairing supplies are pinned before any token is sent to
them: API URLs must be https on `api.muse.ai` or another `*.muse.ai` host
(any Muse subdomain is trusted with the device token), the Noise host
must be `hatch.metaaivm.com` or another `*.metaaivm.com` host, and API
redirects are followed only to the same host over https on the standard
port, so a redirect can't downgrade the connection or move it elsewhere.

## Reporting

Report vulnerabilities privately through GitHub security advisories on this
repository. Never post tokens, pairing files, or real chats in issues.

## Release checklist

CI runs the plugin tests, the companion tests,
`ruff check helpers tests companion/src companion/tests`, and Qt Quick Test
coverage of the real in-memory chat state machine.

The marketplace's automated baseline should report no findings, and
`review-required` for capabilities that are accurately present: the companion's
installer, package installs, `sudo`, and its systemd service.

Before each release, on an Omarchy machine:

- `omarchy plugin validate .` on a clean source tree (no `.venv` symlinks), and
  Qt 6 `qmllint -I "$OMARCHY_PATH/shell" *.qml` (not a Qt 5 tool on PATH).
- A secret scan of the working tree and the full git history.
- Click to open and close the panel; Escape closes it;
  `omarchy-shell shell toggle omuseful` works.
- Disable, re-enable, `omarchy restart shell`, then `omarchy plugin remove`:
  no leftover helper processes (`pgrep -af muse_bridge`).
- With the companion stopped, offline, or stock: the icon dims, setup shows
  the right step, and sending is disabled while drafts remain editable.
- Fresh pairing with the current Muse phone app succeeds after terminal
  confirmation; no token appears in discovery, logs, or the public status file.
- Replace a token: the guide offers Pair again, and a fresh device token is
  minted with the replacement after explicit terminal confirmation.
- Failed, unconfirmed, accepted-but-interrupted, and complete turns have distinct
  labels; Edit & retry never sends automatically or overwrites another draft.
- Enable notifications: a completed background turn creates one fixed-text
  alert; partial/stopped/timed-out turns do not. DND/snooze suppress popups, and
  clicking the alert summons Chat even if Setup was previously selected.
- Test two monitors: reading the latest Chat on either clears unread state;
  closing one panel must not mark the other as closed.
- In the Muse app, the paired device lists no commands.

Licensing and terms (not legal advice; recheck before publishing):

- Each user brings their own SDK token; never ship one in code, examples,
  screenshots, or release assets.
- Don't imply an official Meta or Omarchy product. No trademark search has
  been done for the name "Omuseful".
- The companion release must keep the upstream Apache-2.0 license and
  notices, and must not include the Jollybot avatar, which Meta excludes from
  that license.
