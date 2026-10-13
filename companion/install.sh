#!/usr/bin/env bash
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Install the Omuseful chat companion (a chat-only Linux Muse device).
# Modified by yaeloss for Omuseful: chat-only, local source, packaged uv.
#
#   bash install.sh [--yes] [--no-pair]
#   bash install.sh --uninstall [--purge]
#
# Installs system packages (including uv) and musegadget, built from this
# folder, into /opt/musegadget; sets BlueZ's GATT MTU for the Android app;
# installs and starts the musegadget service; then opens Bluetooth pairing for
# the Muse app. Normally run by omuseful-setup.sh.

set -euo pipefail

PREFIX="/opt/musegadget"
VENV="$PREFIX/venv"
UNIT="/etc/systemd/system/musegadget.service"
STATE_DIR="/var/lib/musegadget"
BLUEZ_CONF="/etc/bluetooth/main.conf"
BLUEZ_DROPIN="/etc/systemd/system/bluetooth.service.d/zz-musegadget.conf"
# Always this folder: nothing is fetched from another repository.
DEFAULT_SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Everything, including uv and the build backend, from the signed Arch repos.
ARCH_PACKAGES=(bluez python python-dbus python-gobject python-cryptography python-setuptools uv ca-certificates)

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: install.sh [options]

  --from FOLDER     Build musegadget from FOLDER. Default: this folder.
  --chat-only       Accepted for compatibility; Omuseful is always chat-only,
                    so the Muse can't run anything on this computer.
                    The SDK token is asked for with hidden input and saved,
                    readable only by root, in $STATE_DIR/sdk_token.
  --yes             Don't ask for confirmation.
  --no-pair         Install without opening Bluetooth pairing.
  --uninstall       Remove musegadget. Keeps the device identity and pairing
                    in $STATE_DIR unless --purge is also given.
  -h, --help        Show this help.
EOF
}


as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

systemd_running() { [ -d /run/systemd/system ]; }

# --- Checks ------------------------------------------------------------------

check_system() {
    [ "$(uname -s)" = Linux ] || die "Omuseful's companion runs on Linux."
    [ -r /etc/os-release ] || die "cannot identify this Linux distribution (/etc/os-release missing)."
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-} ${ID_LIKE:-}" in
        *arch*) ;;
        *) die "${PRETTY_NAME:-This system} is not supported. Omuseful's companion needs Arch Linux (Omarchy)." ;;
    esac
    command -v pacman >/dev/null || die "pacman not found."
    case "$(uname -m)" in x86_64|aarch64) ;; *) die "unsupported architecture: $(uname -m)" ;; esac
    if ! systemd_running; then
        warn "systemd is not running; the service will be installed but not started."
    fi
    if [ -z "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
        warn "no Bluetooth adapter found. You can install, but pairing with the Muse app needs Bluetooth LE."
    fi
}

choose_account() {
    say "Chat-only device: your Muse can chat through it but can't run anything on this machine."
}

# --- Install -----------------------------------------------------------------

install_packages() {
    local missing=() pkg
    for pkg in "${ARCH_PACKAGES[@]}"; do
        pacman -Qq "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if [ "${#missing[@]}" -eq 0 ]; then return; fi
    say "Installing system packages: ${missing[*]}"
    as_root pacman -S --needed --noconfirm "${missing[@]}"
}

install_uv() {
    # Root runs only the packaged uv, never one found earlier on the user's PATH.
    UV_BIN=/usr/bin/uv
    [ -x "$UV_BIN" ] && [ "$(stat -c %u "$UV_BIN")" = 0 ] ||
        die "uv is missing; install it with: sudo pacman -S uv"
}

enable_bluez() {
    if systemd_running; then
        say "Enabling the BlueZ service"
        as_root systemctl enable --now bluetooth.service
    fi
}

install_musegadget() {
    local uv="${UV_BIN:-$PREFIX/bin/uv}"
    # The system interpreter is required: dbus and gi come from pacman and are
    # built for it.
    if [ ! -x "$VENV/bin/python" ]; then
        say "Creating $VENV"
        as_root "$uv" venv --quiet --python /usr/bin/python3 --system-site-packages "$VENV"
    fi
    say "Installing musegadget from $SOURCE"
    local build_src="$SOURCE"
    if [ -d "$SOURCE" ]; then
        # Build from a private root-owned copy: root never writes build files
        # into (or reads mid-edit from) the user's plugin folder.
        build_src="$(as_root mktemp -d /tmp/omuseful-build.XXXXXX)"
        BUILD_TMP="$build_src"
        as_root cp -R "$SOURCE/." "$build_src/"
        as_root rm -rf "$build_src/build" "$build_src"/src/*.egg-info
    fi
    # The build backend is the system python-setuptools: nothing is fetched.
    as_root "$uv" pip install --quiet --python "$VENV/bin/python" --no-deps --reinstall \
        --no-build-isolation "$build_src"
    cleanup_build
    local data
    data="$(as_root "$VENV/bin/python" -I -c 'import musegadget, os; print(os.path.join(os.path.dirname(musegadget.__file__), "data"))')"
    as_root "$uv" pip install --quiet --python "$VENV/bin/python" --require-hashes \
        -r "$data/requirements.lock"
    as_root ln -sf "$VENV/bin/musegadget" /usr/local/bin/musegadget
    DATA_DIR="$data"
}

configure_bluez() {
    # The Muse Android app writes (MTU - 3)-byte packets and fails above 512,
    # so keep the negotiated MTU at 256, as the ESP32 firmware does.
    local result
    result="$(as_root python3 -I - "$BLUEZ_CONF" <<'EOF'
import re, shutil, sys
path = sys.argv[1]
try:
    text = open(path).read()
except FileNotFoundError:
    text = ""
active = re.search(r"^\s*ExchangeMTU\s*=\s*(\d+)\s*$", text, re.M)
if active and int(active.group(1)) <= 256:
    print("unchanged"); sys.exit()
line = "ExchangeMTU = 256"
if active:
    text = text[:active.start()] + line + text[active.end():]
elif re.search(r"^\s*#\s*ExchangeMTU\s*=.*$", text, re.M):
    text = re.sub(r"^\s*#\s*ExchangeMTU\s*=.*$", line, text, count=1, flags=re.M)
elif re.search(r"^\[GATT\]\s*$", text, re.M):
    text = re.sub(r"^\[GATT\]\s*$", "[GATT]\n" + line, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + ("\n\n" if text else "") + "[GATT]\n" + line + "\n"
if text != "":
    try:
        shutil.copy2(path, path + ".pre-musegadget")
    except FileNotFoundError:
        pass
open(path, "w").write(text)
print("changed")
EOF
)"
    if [ "$result" = changed ]; then
        say "Set BlueZ ExchangeMTU = 256 (backup: $BLUEZ_CONF.pre-musegadget)"
        if systemd_running; then as_root systemctl restart bluetooth; fi
    fi
    disable_bluez_battery
}

# BlueZ's battery plugin reads a connecting phone's battery level. iPhones
# answer that only over a bonded link, so BlueZ then asks the phone to bond,
# which this device refuses, and iOS pairing can drop. Gadgets never bond.
disable_bluez_battery() {
    systemd_running || return 0
    local unit_text result
    unit_text="$(systemctl cat bluetooth.service 2>/dev/null || true)"
    result="$(as_root python3 -I - "$BLUEZ_DROPIN" "$unit_text" <<'EOF'
import os, re, sys
path, unit_text = sys.argv[1], sys.argv[2]
# The last non-empty ExecStart= across the unit and its drop-ins is the one
# systemd runs. Leave anything quoted alone rather than risk rewriting it.
lines = re.findall(r"^ExecStart=(.*)$", unit_text, re.M)
command = next((l.strip() for l in reversed(lines) if l.strip()), "")
if not command or any(c in command for c in "\"'\\$%;"):
    print("skipped"); sys.exit()
args = command.split()
for i, arg in enumerate(args):
    inline = arg.startswith("--noplugin=")
    if inline or (arg in ("-P", "--noplugin") and i + 1 < len(args)):
        names = arg.split("=", 1)[1] if inline else args[i + 1]
        if "battery" in names.split(","):
            print("unchanged"); sys.exit()
        names = names + ",battery" if names else "battery"
        if inline:
            args[i] = "--noplugin=" + names
        else:
            args[i + 1] = names
        break
else:
    args.append("--noplugin=battery")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write("# Written by the musegadget installer: never ask phones to bond.\n"
            "[Service]\nExecStart=\nExecStart=" + " ".join(args) + "\n")
print("changed")
EOF
)"
    case "$result" in
        changed)
            say "Turned off BlueZ's battery plugin so phones are never asked to bond ($BLUEZ_DROPIN)"
            as_root systemctl daemon-reload
            as_root systemctl restart bluetooth
            ;;
        skipped) say "Left BlueZ plugins as they are: could not read bluetooth.service's command line." ;;
    esac
}

save_sdk_token() {
    if [ -z "$SDK_TOKEN" ]; then
        if ! as_root test -s "$STATE_DIR/sdk_token"; then
            say "No SDK token yet. Get one at gadgets.muse.ai, then run omuseful-token.sh."
        fi
        return 0
    fi
    say "Saving your SDK token"
    as_root install -d -m 0700 "$STATE_DIR"
    printf '%s\n' "$SDK_TOKEN" | as_root install -m 0600 /dev/stdin "$STATE_DIR/sdk_token"
}

prompt_sdk_token() {
    if [ -n "$SDK_TOKEN" ] || [ "$NO_PAIR" = 1 ] || [ "$ASSUME_YES" = 1 ]; then
        return 0
    fi
    [ -r /dev/tty ] || return 0
    local have=0 attempt
    if as_root test -s "$STATE_DIR/sdk_token"; then have=1; fi
    for attempt in 1 2 3; do
        if [ "$have" = 1 ]; then
            printf '\nPress Enter to keep your Muse SDK token, or paste a new one (hidden): ' >/dev/tty
        else
            printf '\nPaste your Muse SDK token (hidden).\nGet one at https://gadgets.muse.ai/settings/sdk-tokens\nToken: ' >/dev/tty
        fi
        IFS= read -r -s SDK_TOKEN </dev/tty || SDK_TOKEN=""
        printf '\n' >/dev/tty
        if [ -z "$SDK_TOKEN" ] && [ "$have" = 1 ]; then return 0; fi
        if [[ "$SDK_TOKEN" =~ ^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$ ]]; then return 0; fi
        printf 'That does not look like an SDK token (it starts with mgst_). Try again.\n' >/dev/tty
    done
    SDK_TOKEN=""
    die "no valid SDK token; run setup again when you have one."
}

install_service() {
    say "Installing the musegadget service"
    as_root install -m 0644 "$DATA_DIR/musegadget.service" "$UNIT"
    if systemd_running; then
        as_root systemctl daemon-reload
        as_root systemctl enable --now musegadget.service >/dev/null 2>&1
        as_root systemctl restart musegadget.service
    fi
}

wait_for_bluetooth() {
    # Setup may have just restarted bluetoothd; advertising too early fails
    # with "Invalid Parameters" and the phone never sees this computer.
    local tries
    for tries in $(seq 20); do
        if bluetoothctl show 2>/dev/null | grep -q 'Powered: yes'; then
            sleep 3
            return 0
        fi
        sleep 1
    done
    warn "Bluetooth is not ready; pairing may fail. Check that it is on, then run 'sudo musegadget pair'."
}

pair() {
    if as_root test -s "$STATE_DIR/pairing.json"; then
        say "Already paired; the service will reconnect to your Muse."
        return
    fi
    if [ "$NO_PAIR" = 1 ]; then
        say "Skipping pairing. Run 'sudo musegadget pair' when you're ready."
        return
    fi
    cat <<EOF

Now add this computer in the Muse app on your phone:
  1. Settings > Devices > turn on Developer mode
  2. Tap Add Device (+) and choose the name shown below
  3. If asked for Wi-Fi, pick the network shown; no password is needed
Keep this window open until it says Paired.

EOF
    wait_for_bluetooth
    as_root /usr/local/bin/musegadget pair || warn "not paired. Run 'sudo musegadget pair' to try again."
}

summary() {
    if as_root test -s "$STATE_DIR/pairing.json"; then
        say "Paired with your Muse."
    else
        warn "Not paired yet. Run setup again, or 'sudo musegadget pair', when your phone is ready."
    fi
}

# --- Uninstall ---------------------------------------------------------------

uninstall() {
    say "Removing musegadget"
    if systemd_running && [ -f "$UNIT" ]; then
        as_root systemctl disable --now musegadget.service >/dev/null 2>&1 || true
    fi
    as_root rm -f "$UNIT" /usr/local/bin/musegadget
    if systemd_running; then as_root systemctl daemon-reload; fi
    as_root rm -rf "$PREFIX"
    if [ "$PURGE" = 1 ]; then
        as_root rm -rf "$STATE_DIR"
        say "Removed the device identity and pairing too. Remove the device in the Muse app as well."
    else
        say "Kept the device identity and pairing in $STATE_DIR (use --purge to remove them)."
    fi
    if [ -f "$BLUEZ_DROPIN" ]; then
        as_root rm -f "$BLUEZ_DROPIN"
        if systemd_running; then
            as_root systemctl daemon-reload
            as_root systemctl restart bluetooth
        fi
        say "Turned BlueZ's battery plugin back on."
    fi
    if [ -f "$BLUEZ_CONF.pre-musegadget" ]; then
        as_root cp -p "$BLUEZ_CONF.pre-musegadget" "$BLUEZ_CONF"
        as_root rm -f "$BLUEZ_CONF.pre-musegadget"
        if systemd_running; then as_root systemctl restart bluetooth; fi
        say "Restored the original BlueZ settings ($BLUEZ_CONF)."
    fi
    as_root rmdir --ignore-fail-on-non-empty "$(dirname "$BLUEZ_DROPIN")" 2>/dev/null || true
}

cleanup_build() {
    if [ -n "${BUILD_TMP:-}" ]; then
        as_root rm -rf "$BUILD_TMP"
        BUILD_TMP=""
    fi
}

main() {
    SOURCE="$DEFAULT_SOURCE" SDK_TOKEN="" ASSUME_YES=0 NO_PAIR=0 UNINSTALL=0 PURGE=0 BUILD_TMP=""
    trap cleanup_build EXIT
    while [ $# -gt 0 ]; do
        case "$1" in
            --from) SOURCE="${2:?--from needs a value}"; shift 2 ;;
            --chat-only) shift ;;
            --sdk-token) die "never pass a token as an argument; the installer asks for it with hidden input." ;;
            --yes|-y) ASSUME_YES=1; shift ;;
            --no-pair) NO_PAIR=1; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            --purge) PURGE=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
    done
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null || die "run this as root, or install sudo."
        sudo true || die "this installer needs sudo."
    fi
    if [ "$UNINSTALL" = 1 ]; then uninstall; return; fi
    [ -d "$SOURCE" ] || die "--from must be a local folder (Omuseful installs only from its own source)."
    SOURCE="$(cd "$SOURCE" && pwd)"

    check_system
    prompt_sdk_token
    if [ -n "$SDK_TOKEN" ] && ! [[ "$SDK_TOKEN" =~ ^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$ ]]; then
        die "that SDK token is not valid; copy it again from gadgets.muse.ai."
    fi
    choose_account
    install_packages
    enable_bluez
    install_uv
    install_musegadget
    configure_bluez
    save_sdk_token
    install_service
    pair
    summary
}

main "$@"
