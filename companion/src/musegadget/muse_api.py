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

"""Client for the Muse device API: leased VM lookup."""

from __future__ import annotations

import http.client
import json
import logging
import platform
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from musegadget import __version__

log = logging.getLogger(__name__)

API_BASE = "https://api.muse.ai"
FETCH_PATH = "/fetch_vms"
REFRESH_PATH = "/device_token/refresh"
# VM metadata and token pairs are small. Leave room for large VM lists, but
# bound the bytes held before JSON decoding, independently of socket timeouts.
MAX_API_RESPONSE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# The pairing supplies these endpoints, but the device sends its bearer
# tokens to them, so only Meta's hosts are ever trusted with credentials.
API_HOST = "api.muse.ai"
API_HOST_SUFFIX = ".muse.ai"
NOISE_HOST = "hatch.metaaivm.com"
NOISE_HOST_SUFFIX = ".metaaivm.com"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _valid_host(host: str) -> bool:
    return len(host) <= 253 and all(
        _HOST_LABEL.fullmatch(label) for label in host.split(".")
    )


def _trusted_host(host: str | None, exact: str, suffix: str) -> bool:
    return bool(host) and _valid_host(host) and (host == exact or host.endswith(suffix))


def trusted_api_url(url: object) -> bool:
    """True for an https Muse API URL with no userinfo or unusual port."""
    if not isinstance(url, str) or "\\" in url:
        return False
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and _trusted_host(parts.hostname, API_HOST, API_HOST_SUFFIX)
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
        and not parts.query
        and not parts.fragment
    )


def api_root(api_url_v2: str = "") -> str:
    """The URL that API paths are appended to: ``api_url_v2``, or the Muse API.

    An untrusted ``api_url_v2`` from the pairing is ignored rather than
    trusted with the device token. ``api_url`` is ignored entirely; only
    older firmware reads it, adding ``/hatch``.
    """
    candidate = (api_url_v2 or "").strip().rstrip("/")
    if candidate and trusted_api_url(candidate):
        return candidate
    if candidate:
        log.warning("ignoring untrusted Muse API URL from the pairing")
    return API_BASE


def trusted_noise_host(value: object) -> str:
    """The Noise host from the pairing, or Meta's default when untrusted."""
    host = value.strip().lower() if isinstance(value, str) else ""
    if _trusted_host(host, NOISE_HOST, NOISE_HOST_SUFFIX):
        return host
    if host:
        log.warning("ignoring untrusted Noise host from the pairing")
    return NOISE_HOST


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would carry the Authorization header elsewhere.

    Only an https redirect to the same host on the standard port is
    followed: a downgrade to http or a move to another port on the same
    host would still receive the bearer token.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib normally drains redirect bodies with an unbounded fp.read().
        # Its HTTPResponse.read() returns b"" after close; each hop already uses
        # a new connection, so discard the body before delegating URL/loop checks.
        fp.close()
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = urlsplit(newurl)
            port = target.port
        except ValueError:
            return None
        if (
            target.scheme != "https"
            or port not in (None, 443)
            or target.hostname != urlsplit(req.full_url).hostname
            or target.username is not None
            or target.password is not None
        ):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request, timeout: float):
    request.add_header("Accept-Encoding", "identity")
    opener = urllib.request.build_opener(_SameHostRedirectHandler)
    return opener.open(request, timeout=timeout)


class _InvalidResponse(ValueError):
    """An unusable API response; messages must not include response contents."""


def _read_json(resp):
    """Read at most 1 MiB + one overflow byte, then decode bounded JSON.

    Content-Length is only an early rejection hint. Enforce the actual byte
    budget too, including chunked and EOF-delimited responses. urllib doesn't
    decompress bodies; explicitly refuse content encodings rather than inflate
    an attacker-controlled response or pass compressed bytes to the decoder.
    """
    if any(value.strip().lower() != "identity"
           for value in resp.headers.get_all("Content-Encoding", [])):
        raise _InvalidResponse("unsupported API response content encoding")
    for value in resp.headers.get_all("Content-Length", []):
        value = value.strip()
        if not re.fullmatch(r"[0-9]+", value):
            raise _InvalidResponse("invalid API response length")
        # Avoid converting an arbitrarily long decimal header into a bigint.
        digits = value.lstrip("0") or "0"
        if len(digits) > len(str(MAX_API_RESPONSE_BYTES)) or int(digits) > MAX_API_RESPONSE_BYTES:
            raise _InvalidResponse("API response exceeds 1 MiB limit")

    body = bytearray()
    while True:
        chunk = resp.read(min(_READ_CHUNK_BYTES, MAX_API_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > MAX_API_RESPONSE_BYTES:
            raise _InvalidResponse("API response exceeds 1 MiB limit")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        # Includes malformed encoding, excessive integer digits and nesting.
        # Never log body excerpts (which could contain credentials).
        raise _InvalidResponse("invalid API response JSON") from None


def user_agent() -> str:
    details = []
    try:
        model = Path("/proc/device-tree/model").read_text(errors="replace")
        model = model.replace("\x00", "").strip()
        if model:
            details.append(model)
    except OSError:
        pass
    details.append(f"{platform.system()} {platform.machine()}".strip())
    return (
        f"musegadget/{__version__} ({'; '.join(d for d in details if d)}) "
        f"Python/{platform.python_version()}"
    )


def fetch_vms_with_status(
    access_token: str, root: str = api_root()
) -> tuple[list[dict], int | None]:
    """Leased VMs for the device token, plus the HTTP status if one arrived.

    Returns ``(vms, status)``; ``status`` is ``None`` when no HTTP response
    was obtained or the response was unusable. A 401 means the device token
    was rejected, which a retry won't fix; ``None`` is worth retrying.
    """
    req = urllib.request.Request(root + FETCH_PATH, method="GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("X-API-Version", "1.0.0")
    req.add_header("User-Agent", user_agent())
    try:
        with _open(req, timeout=15) as resp:
            status = resp.getcode()
            data = _read_json(resp)
    except urllib.error.HTTPError as exc:
        exc.close()  # Error bodies aren't used; never drain them.
        log.error("VM fetch failed: HTTP %d", exc.code)
        return [], exc.code
    except (urllib.error.URLError, _InvalidResponse, OSError, http.client.HTTPException) as exc:
        log.error("VM fetch failed: %s", exc)
        return [], None

    if not isinstance(data, dict):
        log.error("VM fetch: unexpected response type %s", type(data).__name__)
        return [], status
    if data.get("error_title") or data.get("backend_error_code"):
        log.error("VM fetch error: %s %s",
                  data.get("error_title") or "", data.get("backend_error_code") or "")
        return [], status
    vm_list = data.get("vm_list")
    if not isinstance(vm_list, list):
        log.error("VM fetch: missing vm_list")
        return [], status

    vms = []
    for entry in vm_list:
        if not isinstance(entry, dict):
            continue
        vm_url = entry.get("vm_ws_url") or entry.get("vm_url")
        vm_token = entry.get("vm_auth_token")
        if vm_url and vm_token:
            vms.append({
                "vm_url": vm_url,
                "vm_auth_token": vm_token,
                "vm_name": entry.get("vm_name", ""),
                "vm_id": entry.get("vm_id", ""),
                "is_default": bool(entry.get("default", False)),
            })
    log.info("VM fetch: %d VMs", len(vms))
    return vms, status


def _post_refresh(
    auth_header: str, device_id: str, root: str, sdk_token: str | None,
) -> dict | None:
    body = {"device_id": device_id}
    if sdk_token:
        body["sdk_token"] = sdk_token
    req = urllib.request.Request(
        root + REFRESH_PATH,
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", auth_header)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", user_agent())
    with _open(req, timeout=15) as resp:
        data = _read_json(resp)
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        data = data["payload"]
    if isinstance(data, dict) and data.get("access_token") and data.get("refresh_token"):
        return data
    log.warning("token refresh response missing tokens")
    return None


def refresh_device_token(
    refresh_token: str, device_id: str, root: str = api_root(),
    sdk_token: str | None = None,
) -> tuple[dict | None, int | None]:
    """Rotate the device token pair with the refresh token.

    Returns ``(tokens, status)``: ``tokens`` is None on failure, and
    ``status`` is the HTTP status that ended the attempt, or None for a
    transport failure or unusable response. A 401 means the pairing is gone
    and the device must be paired again.

    The access token is never presented, as on the ESP32: the server can
    accept it and answer 200 with replacement tokens that every endpoint then
    rejects, which would overwrite working credentials.
    """
    # Apps now hand over refresh tokens that already carry the hatch_refresh:
    # prefix; doubling it makes the server reject it. Same rule as the ESP32.
    raw_refresh = refresh_token.rsplit(":", 1)[-1]
    try:
        return _post_refresh(f"Bearer hatch_refresh:{raw_refresh}", device_id, root, sdk_token), 200
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            log.warning("token refresh rejected: device must be paired again")
        else:
            log.warning("token refresh failed: HTTP %d", exc.code)
        return None, exc.code
    except (urllib.error.URLError, _InvalidResponse, OSError, http.client.HTTPException) as exc:
        log.warning("token refresh failed: %s", exc)
        return None, None
