# SPDX-License-Identifier: MIT
from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("muse_bridge", Path(__file__).parents[1] / "helpers" / "muse_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)



def write_status(path: Path, **fields) -> Path:
    data = {"schema_version": 1, "backend": "omuseful", "chat_only": True,
            "paired": True, "connected": True, "updated_at": time.time(),
            "ble_name": "MuseGadgetABCDEF", "sdk_token_configured": True}
    data.update(fields)
    path.write_text(json.dumps(data))
    return path


@pytest.fixture(autouse=True)
def trust_test_files(monkeypatch):
    """Tests create files and sockets as the current user, not root."""
    monkeypatch.setattr(bridge, "TRUSTED_UID", os.getuid())


@pytest.fixture
def status(tmp_path):
    return write_status(tmp_path / "status.json")


def serve_once(events, received):
    """A synthetic companion socket that records the request and replays events."""
    directory = tempfile.TemporaryDirectory(prefix="omu-")  # short AF_UNIX path
    path = Path(directory.name) / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def run():
        connection, _ = server.accept()
        with connection, connection.makefile("rb") as stream:
            received.append(json.loads(stream.readline()))
            for event in events:
                connection.sendall(json.dumps(event).encode() + b"\n")
        server.close()

    worker = threading.Thread(target=run)
    worker.start()
    return path, worker, directory


# -- Status --------------------------------------------------------------------

def test_missing_companion_is_setup_required(tmp_path):
    assert bridge.backend_status(tmp_path / "missing.json")["state"] == "setup_required"


@pytest.mark.parametrize("fields,state", [
    ({"chat_only": False}, "incompatible"),
    ({"chat_only": "true"}, "incompatible"),
    ({"approval_enabled": True, "chat_only": None}, "incompatible"),
    ({"backend": "musegadget"}, "incompatible"),
    ({"schema_version": 2}, "incompatible"),
    ({"updated_at": time.time() - 60}, "offline"),
])
def test_only_a_fresh_chat_only_companion_is_available(tmp_path, fields, state):
    result = bridge.backend_status(write_status(tmp_path / "status.json", **fields))
    assert result["available"] is False and result["state"] == state


def test_status_projects_only_known_non_secret_fields(tmp_path):
    path = write_status(tmp_path / "status.json", sdk_token="PRIVATE-SYNTHETIC",
                        sdk_token_hint="PRIVATE-HINT", sdk_token_needs_pairing="PRIVATE-SYNTHETIC",
                        account_home="/var/lib/muse", sdk_token_configured="PRIVATE-SYNTHETIC")
    result = bridge.backend_status(path)
    assert result["available"] and result["label"] == "Connected · MuseGadgetABCDEF"
    assert result["sdk_token_configured"] is False
    assert result["sdk_token_needs_pairing"] is False
    assert "PRIVATE" not in json.dumps(result) and "account_home" not in result


def test_device_name_cannot_inject_controls(tmp_path):
    path = write_status(tmp_path / "status.json", ble_name="Muse\u202egnp.exe\x1b")
    assert bridge.backend_status(path)["ble_name"] == "Muse\\u202egnp.exe\\u001b"


def test_device_name_cannot_inject_markup_into_the_shared_bar_tooltip(tmp_path):
    path = write_status(tmp_path / "status.json", ble_name='<img src="https://tracker.example/pixel">')
    status = bridge.backend_status(path)
    assert "<" not in status["label"] and ">" not in status["label"]
    assert "‹img" in status["label"]


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"value":NaN}', "[]"])
def test_noncanonical_json_is_rejected(tmp_path, raw):
    path = tmp_path / "status.json"
    path.write_text(raw)
    assert bridge.backend_status(path)["available"] is False


def test_symlinked_status_is_not_followed(tmp_path, status):
    link = tmp_path / "link.json"
    link.symlink_to(status)
    assert bridge.backend_status(link)["available"] is False


def test_visible_text_keeps_emoji_but_exposes_bidi_and_controls():
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    assert bridge.visible_text(family) == family
    assert bridge.visible_text("a\u202eb\x1b\n") == "a\\u202eb\\u001b\n"


# -- Setup guide ---------------------------------------------------------------

@pytest.mark.parametrize("state", ["setup_required", "offline", "incompatible"])
def test_without_a_companion_there_is_one_setup_step(state):
    guide = bridge.setup_guide({"available": False, "state": state}, False)
    assert [step["id"] for step in guide["steps"]] == ["setup"]
    assert guide["complete"] is False


def test_setup_command_does_everything_and_says_chat_only():
    step = bridge.setup_guide({"available": False, "state": "setup_required"}, False)["steps"][0]
    assert step["command"].startswith("bash ") and step["command"].endswith("/companion/omuseful-setup.sh")
    assert (bridge.COMPANION / "omuseful-setup.sh").is_file()  # the bundled companion exists
    assert "SDK token" in step["body"] and "can't run anything" in step["body"]


@pytest.mark.parametrize("status,current,token_done,pair_done,complete", [
    ({"available": True}, 0, False, False, False),
    ({"available": True, "sdk_token_configured": True}, 1, True, False, False),
    ({"available": True, "sdk_token_configured": True, "paired": True}, 1, True, True, False),
    ({"available": True, "sdk_token_configured": True, "paired": True, "connected": True}, 1, True, True, True),
])
def test_installed_flow_is_token_then_pair(status, current, token_done, pair_done, complete):
    guide = bridge.setup_guide(status, True)
    token, pair = guide["steps"]
    assert (token["id"], pair["id"]) == ("token", "pair")
    assert guide["current_step"] == current and guide["complete"] is complete
    assert token["done"] is token_done and pair["done"] is pair_done


@pytest.mark.parametrize("status", [
    {"available": True},
    {"available": True, "sdk_token_configured": True, "pairing": True},
    {"available": True, "sdk_token_configured": True, "paired": True},
])
def test_pair_command_is_only_offered_when_ready(status):
    assert bridge.setup_guide(status, True)["steps"][1]["command_enabled"] is False


def test_guide_links_and_commands_are_fixed():
    token, pair = bridge.setup_guide({"available": True}, True)["steps"]
    assert token["url"] == "https://gadgets.muse.ai/settings/sdk-tokens"
    assert token["secondary_url"] == "https://gadgets.muse.ai/sdk-terms"
    assert pair["command"] == "sudo musegadget pair"


def test_guide_never_mentions_running_commands_for_muse():
    for status in ({"available": False}, {"available": True}):
        text = json.dumps(bridge.setup_guide(status, True)).lower()
        for word in ("approv", "execution", "run as"):
            assert word not in text


def test_snapshot_reports_missing_chat_access(status, tmp_path):
    snap = bridge.snapshot(status, tmp_path / "no-socket")
    assert snap["error"].startswith("Chat access missing")
    assert snap["setup"]["complete"] is False
    assert set(snap) == {"status", "error", "setup"}


# -- Chat ----------------------------------------------------------------------

def test_stream_projects_only_safe_events(status):
    received, events = [], []
    path, worker, directory = serve_once([
        {"event": "accepted", "token": "PRIVATE"},
        {"event": "progress", "phase": "working", "thoughts": "PRIVATE"},
        {"event": "reply", "id": "r1", "text": "Hello", "done": False},
        {"event": "reply", "id": "r1", "text": "Hello world", "done": True},
        {"ok": True, "reply": "Hello world"},
    ], received)
    with directory:
        result = bridge.send("synthetic hello", events.append, status, path)
        worker.join()
    assert result == {"ok": True, "delivery": "complete", "reply": "Hello world", "html": "Hello world", "links": []}
    assert received == [{"message": "synthetic hello", "stream": True}]
    assert [event["event"] for event in events] == ["accepted", "progress", "reply", "reply"]
    assert "PRIVATE" not in json.dumps(events)


def test_final_only_reply_is_accepted(status):
    received, events = [], []
    path, worker, directory = serve_once([{"ok": True, "reply": "Only final"}], received)
    with directory:
        assert bridge.send("hi", events.append, status, path) == {
            "ok": True, "delivery": "complete", "reply": "Only final", "html": "Only final", "links": []}
        worker.join()
    assert events == []


def test_unknown_failure_is_not_misrepresented_as_a_safe_retry(status):
    path, worker, directory = serve_once([{"ok": False, "error": "PRIVATE detail"}], [])
    with directory:
        result = bridge.send("hi", lambda event: None, status, path)
        assert result["delivery"] == "uncertain" and result["ok"] is False
        assert "PRIVATE" not in result["error"] and "before resending" in result["error"]
        worker.join()


@pytest.mark.parametrize("message", ["", "   ", None, 5])
def test_empty_or_invalid_message_never_opens_socket(status, message):
    with pytest.raises(ValueError):
        bridge.send(message, lambda event: None, status, Path("/nonexistent"))


def test_disconnected_companion_never_opens_socket(tmp_path):
    path = write_status(tmp_path / "status.json", connected=False)
    with pytest.raises(ValueError, match="not connected"):
        bridge.send("hi", lambda event: None, path, Path("/nonexistent"))


def test_oversized_message_is_rejected(status):
    with pytest.raises(ValueError, match="too long"):
        bridge.send("x" * (bridge.MAX_MESSAGE + 1), lambda event: None, status, Path("/nonexistent"))


@pytest.mark.parametrize("reply,delivery", [
    ({"ok": False, "delivery": "failed"}, "failed"),
    ({"ok": False, "status": 401}, "failed"),
    ({"ok": False, "status": 429}, "failed"),
    ({"ok": False, "status": 408}, "uncertain"),
    ({"ok": False, "status": 503}, "uncertain"),
    ({"ok": False, "status": "401"}, "uncertain"),
    ({"ok": True}, "accepted"),
    ({"ok": True, "reply": "Partial", "warning": "Reply timed out"}, "accepted"),
])
def test_delivery_is_classified_conservatively(status, reply, delivery):
    path, worker, directory = serve_once([reply], [])
    with directory:
        assert bridge.send("hi", lambda event: None, status, path)["delivery"] == delivery
        worker.join()


@pytest.mark.parametrize("events,delivery", [
    ([], "uncertain"),
    ([{"event": "accepted"}], "accepted"),
    ([{"event": "reply", "id": "r", "text": "Partial", "done": False}], "accepted"),
])
def test_socket_hangup_preserves_what_is_known_about_delivery(status, events, delivery):
    path, worker, directory = serve_once(events, [])
    with directory:
        with pytest.raises(bridge.DeliveryError) as error:
            bridge.send("hi", lambda event: None, status, path)
        worker.join()
    assert error.value.delivery == delivery


def test_connection_failure_before_sending_is_safe_to_retry(status, tmp_path):
    with pytest.raises(bridge.DeliveryError) as error:
        bridge.send("hi", lambda event: None, status, tmp_path / "absent")
    assert error.value.delivery == "failed"


def test_a_failure_after_acceptance_cannot_become_a_safe_retry(status):
    path, worker, directory = serve_once([
        {"event": "accepted"}, {"ok": False, "delivery": "failed", "error": "PRIVATE"},
    ], [])
    with directory:
        result = bridge.send("hi", lambda event: None, status, path)
        worker.join()
    assert result["delivery"] == "accepted" and "PRIVATE" not in result["error"]


def test_a_final_card_only_reply_is_complete(status):
    path, worker, directory = serve_once([
        {"ok": True, "links": [{"url": "https://shop.example/item", "title": "Listing"}]},
    ], [])
    with directory:
        result = bridge.send("hi", lambda event: None, status, path)
        worker.join()
    assert result["delivery"] == "complete" and result["links"][0]["domain"] == "shop.example"


def test_changed_token_offers_explicit_repair_instead_of_claiming_setup_is_done():
    status = {"available": True, "paired": True, "connected": True,
              "sdk_token_configured": True, "sdk_token_needs_pairing": True}
    guide = bridge.setup_guide(status, True)
    pair = guide["steps"][1]
    assert not guide["complete"] and not pair["done"]
    assert pair["run"] == "sudo musegadget pair --force"
    assert pair["command_enabled"] is True


def test_old_companion_is_offered_the_bundled_update_before_pairing():
    guide = bridge.setup_guide({"available": True, "version": "0.1.3"}, True)
    assert not guide["complete"]
    assert guide["steps"][0]["id"] == "setup" and "update" in guide["steps"][0]["body"]


# -- Rich replies ----------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "<img src=https://tracker.example/x.png>", "<script>alert(1)</script>",
    '<a href="javascript:alert(1)">x</a>', "<style>*{display:none}</style>",
    "[click](javascript:alert(1))", "[x](data:text/html,hi)",
])
def test_reply_markup_is_escaped_not_rendered(hostile):
    out = bridge.render_html(hostile)
    assert "<img" not in out and "<script" not in out and "<style" not in out
    assert "javascript:" not in out.split('href="')[-1] if 'href="' in out else True
    assert 'href="javascript' not in out and 'href="data' not in out


def test_markdown_subset_and_links():
    out = bridge.render_html("**Bold** _it_ `code`\n- one\n# Title\nsee https://a.example/p?x=1&y=2.")
    assert "<b>Bold</b>" in out and "<i>it</i>" in out and "<code>code</code>" in out
    assert "• one" in out and "<b>Title</b>" in out
    assert '<a href="https://a.example/p?x=1&amp;y=2">' in out and out.endswith("</a>.")


def test_labelled_link_shows_its_real_domain():
    out = bridge.render_html("[Your bank](https://evil.example/login)")
    assert ">Your bank <small>(evil.example)</small></a>" in out


def test_links_outside_the_text_become_cards():
    cards = bridge.link_cards([
        {"url": "https://www.shop.example/item/123", "title": "Open the listing"},
        {"url": "https://already.example/in-text", "title": "dup"},
        {"url": "javascript:alert(1)", "title": "bad"}, "junk",
        {"url": "https://untitled.example/x"},
    ], "see https://already.example/in-text")
    assert cards == [
        {"url": "https://www.shop.example/item/123", "title": "Open the listing", "domain": "shop.example"},
        {"url": "https://untitled.example/x", "title": "untitled.example", "domain": "untitled.example"},
    ]


def test_streamed_reply_carries_html(status):
    received, events = [], []
    path, worker, directory = serve_once([
        {"event": "reply", "id": "r1", "text": "See **this**", "done": True,
         "links": [{"url": "https://x.example", "title": "X"}]},
        {"ok": True, "reply": "See **this**"},
    ], received)
    with directory:
        result = bridge.send("hi", events.append, status, path)
        worker.join()
    assert "<b>this</b>" in events[0]["html"]
    assert events[0]["links"] == [{"url": "https://x.example", "title": "X", "domain": "x.example"}]
    assert "<b>this</b>" in result["html"]


# -- Audit regressions ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://p\u0430ypal.com/",                  # Cyrillic a: lookalike domain
    "https://bank.example@evil.example/",        # user@ hides the real host
    "https://a.example/x\x1b[201~echo pwned",    # terminal escape via the clipboard
    "https://a.example/\u202emoc.lapyap",        # bidi override in a card
    "https://a.example:99999/",                  # invalid port
    "file:///etc/passwd", "javascript:alert(1)", "https:///nohost",
])
def test_unsafe_urls_are_never_links(url):
    assert bridge.safe_url(url) == ""
    assert bridge.link_cards([{"url": url, "title": "x"}]) == []
    assert "<a " not in bridge.render_html(url)


def test_status_file_not_owned_by_the_companion_is_ignored(status, monkeypatch):
    monkeypatch.setattr(bridge, "TRUSTED_UID", os.getuid() + 1)
    assert bridge.backend_status(status)["available"] is False


def test_socket_not_owned_by_the_companion_is_refused(status, monkeypatch):
    connected = bridge.backend_status(status)
    monkeypatch.setattr(bridge, "backend_status", lambda path=None: connected)
    monkeypatch.setattr(bridge, "TRUSTED_UID", os.getuid() + 1)
    with tempfile.TemporaryDirectory(prefix="omu-") as directory:
        path = Path(directory) / "s"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as impostor:
            impostor.bind(str(path))
            impostor.listen(1)
            with pytest.raises(ValueError, match="not owned"):
                bridge.send("hi", lambda event: None, status, path)


def test_deeply_nested_json_is_rejected_cleanly(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("[" * 70000)
    assert bridge.backend_status(path)["available"] is False


def test_long_replies_are_bounded_after_escaping(status):
    path, worker, directory = serve_once([{"ok": True, "reply": "\x01" * 20000}], [])
    with directory:
        result = bridge.send("hi", lambda event: None, status, path)
        worker.join()
    assert len(result["reply"]) <= bridge.MAX_REPLY


def test_version_must_be_a_short_string(tmp_path):
    assert "version" not in bridge.backend_status(write_status(tmp_path / "s.json", version={"x": 1}))
    assert bridge.backend_status(write_status(tmp_path / "t.json", version="0.1.0"))["version"] == "0.1.0"


@pytest.mark.parametrize("url,ok", [
    ("https://ok.example/p", True),
    ("https://xn--pypal-4ve.com/", True),          # IDN shown as punycode
    ("https://[::1]:8080/x", True),
    ("https://evil.example\\accounts.google.com/login", False),  # backslash host spoof
    ("https://-bad.example/", False),
    ("https://a_b.example/", False),
    ("https://[zz::1]/", False),
])
def test_only_plain_hosts_become_links(url, ok):
    assert bool(bridge.safe_url(url)) is ok


def test_copied_commands_are_shell_quoted(monkeypatch, tmp_path):
    odd = Path("/srv/plug$(touch /tmp/pwn) \"x\"")
    monkeypatch.setattr(bridge, "COMPANION", odd / "companion")
    command = bridge.companion_command("omuseful-setup.sh")
    import shlex
    assert shlex.split(command) == ["bash", str(odd / "companion" / "omuseful-setup.sh")]



def test_setup_runs_only_fixed_bundled_commands():
    commands = set()
    for status in ({"available": False}, {"available": False, "state": "offline"},
                   {"available": True}, {"available": True, "sdk_token_configured": True}):
        for step in bridge.setup_guide(status, True)["steps"]:
            if step.get("run"):
                commands.add(step["run"])
    assert commands == {bridge.terminal_command(bridge.SETUP_COMMAND),
                        bridge.terminal_command(bridge.companion_command("omuseful-token.sh")),
                        bridge.PAIR_COMMAND}


def test_pair_is_only_offered_once_a_token_exists():
    token_missing = bridge.setup_guide({"available": True}, True)["steps"][1]
    assert token_missing["run"] == "" and token_missing["disabled_hint"]


def test_manifest_version_meets_bridge_floor():
    root = Path(__file__).parents[1]
    version = json.loads((root / "manifest.json").read_text())["version"]
    assert tuple(map(int, version.split("."))) >= bridge.MIN_COMPANION_VERSION


VECTORS = Path(__file__).parent / "link_vectors.json"


def test_shared_link_vectors():
    # Same file drives the QML suite (tst_LinkSafety.qml): the two link
    # checks must agree on every vector.
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert vectors
    for vector in vectors:
        assert bool(bridge.safe_url(vector["url"])) is vector["valid"], vector["url"]


def test_link_length_boundaries():
    # Mirrored in tst_LinkSafety.qml; overlong input is never a link.
    assert bridge.safe_url("https://example.com/" + "a" * (2048 - 20))
    assert bridge.safe_url("https://example.com/" + "a" * (2049 - 20)) == ""
    assert bridge.safe_url("https://" + "a" * 253 + "/")
    assert bridge.safe_url("https://" + "a" * 254 + "/") == ""


def test_reply_anchors_use_the_exact_markup_the_panel_restyles():
    # Panel.qml's styleLinks tints '<a href="' only; anything else would pass
    # through unstyled.
    bare = bridge._anchor("https://example.com/p")
    assert bare == '<a href="https://example.com/p">https://example.com/p</a>'
    labelled = bridge._anchor("https://example.com/p", "Example")
    assert labelled.startswith('<a href="https://example.com/p">')
    assert labelled.count("<a ") == 1 and "style=" not in labelled
