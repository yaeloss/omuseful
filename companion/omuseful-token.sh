#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Adds or replaces your Muse SDK token. Run it yourself in a terminal.
set +x
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
TASK="Adding the token"

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
if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  cat <<'USAGE'
Usage: bash omuseful-token.sh

Asks for your Muse SDK token with input hidden, saves it root-only, and
restarts the chat companion. Get a token at
https://gadgets.muse.ai/settings/sdk-tokens
USAGE
  exit 0
fi
(( $# == 0 )) || die "never pass a token as an argument; the script asks for it"
[[ -t 0 && -t 1 ]] || die "run this in an interactive terminal"
command -v sudo >/dev/null || die "sudo is required"
systemctl cat musegadget >/dev/null 2>&1 || die "install the companion first: bash omuseful-setup.sh"

printf '%s\n' 'Get your token at https://gadgets.muse.ai/settings/sdk-tokens'
TOKEN=''
trap 'unset TOKEN' EXIT
IFS= read -r -s -p 'Muse SDK token (input hidden): ' TOKEN || die "cancelled"
printf '\n'
[[ "$TOKEN" =~ ^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$ ]] || die "that doesn't look like an SDK token (mgst_...); nothing saved"
sudo install -d -o root -g root -m 0700 /var/lib/musegadget
printf '%s\n' "$TOKEN" | sudo install -o root -g root -m 0600 /dev/stdin /var/lib/musegadget/sdk_token
unset TOKEN
sudo systemctl restart musegadget
printf '%s\n' 'Token saved. Pair again to use it: sudo musegadget pair --force'
