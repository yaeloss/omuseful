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

from __future__ import annotations

import io
import json
from email.message import Message

import pytest

from musegadget import muse_api

TOKENS = {"access_token": "new-a", "refresh_token": "new-r"}


@pytest.fixture
def sent(monkeypatch):
    bodies: list[dict] = []

    def urlopen(req, timeout):
        bodies.append((req.get_header("Authorization"), json.loads(req.data)))
        response = io.BytesIO(json.dumps(TOKENS).encode())
        response.headers = Message()
        return response

    monkeypatch.setattr(muse_api, "_open", urlopen)
    return bodies


def test_refresh_sends_only_the_device_id_without_a_key(sent):
    assert muse_api.refresh_device_token("r", "homelink-abcdef") == (TOKENS, 200)
    assert sent == [("Bearer hatch_refresh:r", {"device_id": "homelink-abcdef"})]


def test_refresh_sends_the_sdk_token(sent):
    muse_api.refresh_device_token("r", "homelink-abcdef", sdk_token="mgst_token")
    assert sent == [("Bearer hatch_refresh:r", {"device_id": "homelink-abcdef", "sdk_token": "mgst_token"})]



def test_refresh_never_presents_the_access_token_or_doubles_the_prefix(sent):
    for stored in ("hatch_refresh:raw-r", "raw-r"):
        sent.clear()
        assert muse_api.refresh_device_token(stored, "homelink-abcdef") == (TOKENS, 200)
        assert [header for header, _ in sent] == ["Bearer hatch_refresh:raw-r"]


def test_a_rejected_refresh_reports_401(monkeypatch):
    def urlopen(req, timeout):
        raise muse_api.urllib.error.HTTPError(req.full_url, 401, "", {}, io.BytesIO(b""))

    monkeypatch.setattr(muse_api, "_open", urlopen)
    assert muse_api.refresh_device_token("r", "homelink-abcdef") == (None, 401)


def test_api_root_only_trusts_muse_hosts():
    assert muse_api.api_root() == "https://api.muse.ai"
    assert muse_api.api_root("https://api.muse.ai") == "https://api.muse.ai"
    assert muse_api.api_root("https://edge.muse.ai/v2") == "https://edge.muse.ai/v2"
    for untrusted in ("https://evil.example", "https://api.muse.ai@evil.example",
                      "https://api.muse.ai:8443", "http://api.muse.ai",
                      "https://muse.ai.evil.example", "https://api.muse.ai?x=1",
                      "https://api.muse.ai/#fragment", "not a url"):
        assert muse_api.api_root(untrusted) == "https://api.muse.ai", untrusted


def test_noise_host_only_trusts_metaaivm_hosts():
    assert muse_api.trusted_noise_host("") == "hatch.metaaivm.com"
    assert muse_api.trusted_noise_host("hatch.metaaivm.com") == "hatch.metaaivm.com"
    for untrusted in ("evil.example", "hatch.metaaivm.com@evil.example",
                      "hatch.metaaivm.com/evil", "https://hatch.metaaivm.com", None,
                      ".metaaivm.com", "-.metaaivm.com", "a..metaaivm.com",
                      "hatch.metaaivm.com."):
        assert muse_api.trusted_noise_host(untrusted) == "hatch.metaaivm.com", untrusted


def test_api_root_rejects_malformed_muse_hosts():
    for untrusted in ("https://a..muse.ai", "https://-bad.muse.ai",
                      "https://bad-.muse.ai", "https://api.muse.ai."):
        assert muse_api.api_root(untrusted) == "https://api.muse.ai", untrusted


def test_redirects_never_carry_credentials_to_another_host():
    handler = muse_api._SameHostRedirectHandler()
    req = muse_api.urllib.request.Request("https://api.muse.ai/fetch_vms")
    assert handler.redirect_request(req, None, 302, "", {}, "https://evil.example/x") is None
    same = handler.redirect_request(req, None, 302, "", {}, "https://api.muse.ai/other")
    assert same is not None and same.full_url == "https://api.muse.ai/other"
    explicit = handler.redirect_request(req, None, 302, "", {}, "https://api.muse.ai:443/other")
    assert explicit is not None and explicit.full_url == "https://api.muse.ai:443/other"


def test_redirects_never_downgrade_scheme_or_move_port():
    handler = muse_api._SameHostRedirectHandler()
    req = muse_api.urllib.request.Request("https://api.muse.ai/fetch_vms")
    req.add_header("Authorization", "Bearer SECRET")
    for newurl in ("http://api.muse.ai/x", "https://api.muse.ai:8443/x",
                   "https://api.muse.ai:notaport/x", "https://user@api.muse.ai/x"):
        assert handler.redirect_request(req, None, 302, "", {}, newurl) is None, newurl
