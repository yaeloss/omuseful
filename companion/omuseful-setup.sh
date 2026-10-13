#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Sets up Omuseful's chat companion: install, add your SDK token, pair with the
# Muse app. Run it yourself in a terminal; the Omarchy plugin never runs it.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_USER="${SUDO_USER:-${USER:-}}"
GROUP=omuseful
DROPIN=/etc/systemd/system/musegadget.service.d/omuseful.conf

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
TASK="Setup"

# A failure must not look like success in Omarchy's installer terminal, which
# prints "Done!" unless the script exits 130.
finish() {
  local status=$?
  if (( status != 0 )); then
    printf '\n\033[31m%s did not finish.\033[0m Fix the problem above, then try again.\n' "$TASK" >&2
    if [[ ${OMUSEFUL_TERMINAL:-} == 1 ]] && : 2>/dev/null </dev/tty; then
      read -rsn1 -p 'Press any key to close...' </dev/tty || true
      echo
      exit 130
    fi
  fi
}
trap finish EXIT
usage() {
  cat <<'USAGE'
Usage: bash omuseful-setup.sh [--uninstall]

Sets up the Omuseful chat companion so you can chat with your Muse from the
Omarchy bar. It asks for your Muse SDK token (hidden input), then pairs this
computer with the Muse app over Bluetooth.

The companion is chat-only: it registers no commands, so your Muse cannot run
anything on this computer.

--uninstall   Remove the companion, its service, and the omuseful group.
USAGE
}

UNINSTALL=0
case "${1:-}" in
  "") ;;
  --uninstall) UNINSTALL=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; die "unknown option: $1" ;;
esac

[[ -t 0 && -t 1 ]] || die "run this in an interactive terminal"
[[ "$DESKTOP_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$DESKTOP_USER" != root ]] || die "run this as your desktop user, not root"
[[ -f "$HERE/install.sh" && -f "$HERE/src/musegadget/omuseful.py" ]] || die "run this from the Omuseful companion checkout"
command -v sudo >/dev/null || die "sudo is required"

if (( UNINSTALL )); then
  read -r -p "Remove the Omuseful chat companion and unpair this computer? [y/N] " CONFIRM
  [[ "$CONFIRM" == [yY] ]] || die "cancelled"
  # --purge also deletes the SDK token, identity, and pairing.
  (cd "$HERE" && bash install.sh --uninstall --purge)
  sudo rm -f "$DROPIN"
  sudo rmdir --ignore-fail-on-non-empty "$(dirname "$DROPIN")" 2>/dev/null || true
  sudo systemctl daemon-reload
  if getent group "$GROUP" >/dev/null; then sudo groupdel "$GROUP"; fi
  printf '\nRemoved. Also remove this device in the Muse app (Settings > Devices).\n'
  printf 'To remove the Omarchy plugin too: omarchy plugin remove omuseful\n'
  exit 0
fi

cat <<PLAN
Omuseful setup

Connect Omuseful to your Muse in three steps:
  1. Install the chat companion (asks for your password)
  2. Paste your Muse SDK token, from https://gadgets.muse.ai/settings/sdk-tokens
  3. Add this computer in the Muse app on your phone

Muse can only chat through Omuseful; it can't run anything on this computer.
This installs a small system service, adds you to the '$GROUP' group, and
adjusts two Bluetooth settings for pairing. --uninstall reverts everything.

PLAN
read -r -p "Continue? [Y/n] " CONFIRM
[[ -z "$CONFIRM" || "$CONFIRM" == [yY] ]] || die "cancelled"
sudo -v

getent group "$GROUP" >/dev/null || sudo groupadd --system "$GROUP"
sudo usermod -a -G "$GROUP" "$DESKTOP_USER"
sudo install -d -o root -g root -m 0755 "$(dirname "$DROPIN")"
sudo install -o root -g root -m 0644 "$HERE/src/musegadget/data/omuseful-hardening.conf" "$DROPIN"

# Meta's installer asks for the SDK token, installs the service, then pairs.
(cd "$HERE" && bash install.sh --from . --chat-only)

printf '\nOmuseful is ready. Click its icon in the Omarchy bar to chat with your Muse.\n'
