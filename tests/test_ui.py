# SPDX-License-Identifier: MIT
"""Static checks on the QML surfaces and package hygiene (no Quickshell needed)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
QML = {path.name: path.read_text() for path in ROOT.glob("*.qml")}
CODE = "".join(re.sub(r"^\s*//.*$", "", text, flags=re.M) for text in QML.values())
GENERATED = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def package_paths():
    for directory, directories, files in os.walk(ROOT):
        directories[:] = [name for name in directories if name not in GENERATED]
        yield from (Path(directory) / name for name in directories + files if name not in GENERATED)


PACKAGE = list(package_paths())
SOURCES = [path for path in PACKAGE
           if path.is_file()
           and path.suffix in {".qml", ".py", ".json", ".md", ".toml", ".yml"}]


def test_package_is_only_the_chat_interface():
    assert set(QML) == {"BarWidget.qml", "ChatState.qml", "LinkSafety.qml", "OmusefulMark.qml", "Panel.qml",
                        "Service.qml", "SetupGuide.qml"}
    assert [path.name for path in (ROOT / "helpers").glob("*.py")] == ["muse_bridge.py"]


def test_manifest_has_every_marketplace_field():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for field in ("schemaVersion", "id", "name", "version", "author", "description", "kinds", "entryPoints"):
        assert manifest.get(field), field
    assert manifest["schemaVersion"] == 1 and not manifest["id"].startswith("omarchy.")
    assert "omarchy" not in manifest  # no development-only clonedFrom
    for entry in manifest["entryPoints"].values():
        assert (ROOT / entry).is_file() and not entry.startswith("/") and ".." not in entry


def test_no_symlinks_or_invisible_characters():
    assert not [path for path in PACKAGE if path.is_symlink()]
    hidden = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
    assert not [str(path) for path in SOURCES if hidden.search(path.read_text(encoding="utf-8"))]


def test_qml_runs_only_fixed_local_programs():
    service = QML["Service.qml"]
    programs = set(re.findall(r'\["([^"]+)"', CODE))
    assert programs <= {"/usr/bin/python3", "wl-copy", "omarchy", "omarchy-launch-floating-terminal-with-presentation",
                        "watch", "send"}  # the last two are the helper's own actions
    assert '"-I", "-B"' in service
    # The installer terminal only ever runs a command the setup guide offers.
    assert "return step.run && step.run === command" in service and "if (!offered) return" in service
    for forbidden in ("sudo", "pkexec", '"-c"', "bash", "sh -c", "XMLHttpRequest", "Image {"):
        assert forbidden not in CODE, forbidden


def test_nothing_in_the_ui_approves_or_runs_commands():
    for word in ("decide(", "removeRule", "requests", "PanelWindow"):
        assert word not in CODE, word


def test_text_is_plain_and_links_are_fixed():
    panel, guide, safety = QML["Panel.qml"], QML["SetupGuide.qml"], QML["LinkSafety.qml"]
    # Rich text only for Muse's replies, and only the helper's escaped HTML.
    assert "textFormat: rich ? TextEdit.RichText : TextEdit.PlainText" in panel
    assert 'readonly property bool rich: chatRow.fromMuse && chatRow.messageHtml !== ""' in panel
    assert CODE.count("RichText") == 1 and "StyledText" not in CODE
    assert "onLinkActivated: function(link) { root.openLink(link) }" in panel
    assert "if (linkSafety.isSafeLink(link)) Qt.openUrlExternally(String(link))" in panel
    assert "root.styleLinks(chatRow.messageHtml)" in panel
    # LinkSafety applies the helper's safe_url rules a second time: printable
    # ASCII, a real host, no backslash, no user@ part. Behavior is pinned by
    # tests/link_vectors.json, shared with the Python suite.
    assert 'url.indexOf("\\\\")' in safety and "charCodeAt" in safety
    assert "[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?" in safety
    # Same bounds as safe_url: length cap, host cap, port range, valid IPv6.
    assert "url.length > 2048" in safety and "parseInt(parts[2], 10) > 65535" in safety
    assert 'inner.indexOf(":::")' in safety and "groups.length !== 8" in safety
    assert "parts[1].length > 253" in safety
    assert "TextField" not in guide and "password" not in guide.lower()
    assert guide.count("Qt.openUrlExternally(") == guide.count('startsWith("https://gadgets.muse.ai/")')


def test_chat_view_and_setup_view():
    panel = QML["Panel.qml"]
    assert 'property string currentView: "chat"' in panel
    assert '{ view: "chat", icon: 0xF0365, label: "Chat" }' in panel  # bottom nav, like the Muse app
    assert '{ view: "setup", icon: 0xF08BB, label: "Setup" }' in panel
    assert "anchors.bottom: navBar.top" in panel
    assert "messageField.paste()" not in panel and "Repeater {\n                  model: [\"" not in panel
    assert "it can't run anything on this computer" in panel


def test_bar_uses_the_original_mark():
    assert "OmusefulMark {" in QML["BarWidget.qml"] and "OmusefulMark {" in QML["Panel.qml"]
    mark = QML["OmusefulMark.qml"]
    assert "PathSvg" in mark and "https://" not in mark


def test_a_pending_reply_can_be_stopped():
    # The panel timeout must outlast the companion's 60s accept + 300s reply.
    assert "interval: 420000" in QML["Service.qml"]
    assert "function stopChat()" in QML["Service.qml"] and "chatProcess.running = false" in QML["Service.qml"]
    assert "root.sending ? root.service.stopChat() : root.submitMessage()" in QML["Panel.qml"]


def test_link_cards_open_only_through_the_checked_opener():
    panel = QML["Panel.qml"]
    assert 'objectName: "link-card"' in panel
    assert "onClicked: root.openLink(card.modelData.url)" in panel
    assert CODE.count("Qt.openUrlExternally(") == 2  # openLink + setup's fixed Muse pages
    assert 'visible: chatRow.messageText !== ""' in panel  # a card-only reply has no empty bubble


def test_card_domains_keep_their_real_end_visible():
    assert "elide: Text.ElideLeft" in QML["Panel.qml"]


def test_project_identity_is_omuseful():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["id"] == "omuseful" and manifest["name"] == "Omuseful"
    assert 'moduleName: "omuseful"' in QML["BarWidget.qml"] and 'moduleName: "omuseful"' in QML["Panel.qml"]


def test_notifications_are_opt_in_fixed_text_and_use_only_the_chat_open_action():
    service = QML["Service.qml"]
    notification = re.search(r'id: notificationProcess.*?command:\s*(\[[^\]]+\])', service, re.S)
    assert json.loads(notification[1]) == [
        "omarchy", "notification", "send", "--app-name", "Omuseful", "-u", "normal",
        "-t", "8000", "Muse replied", "Open Omuseful to read the reply.",
        "--exec", "omarchy-shell", "shell", "summon", "omuseful",
    ]
    assert "property bool notificationsEnabled: false" in service
    assert "root.notificationsEnabled && !root.shuttingDown" in service
    assert 'root.setting("notifyReplies", false) === true' in QML["BarWidget.qml"]
