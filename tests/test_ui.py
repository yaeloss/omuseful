# SPDX-License-Identifier: MIT
"""Static checks on the QML surfaces and package hygiene (no Quickshell needed)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
QML = {path.name: path.read_text() for path in ROOT.glob("*.qml")}
CODE = "".join(re.sub(r"^\s*//.*$", "", text, flags=re.M) for text in QML.values())
SOURCES = [path for path in ROOT.rglob("*")
           if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
           and path.suffix in {".qml", ".py", ".json", ".md", ".toml", ".yml"}]


def test_package_is_only_the_chat_interface():
    assert set(QML) == {"BarWidget.qml", "OmusefulMark.qml", "Panel.qml", "Service.qml", "SetupGuide.qml"}
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
    assert not [path for path in ROOT.rglob("*") if path.is_symlink() and ".git" not in path.parts]
    hidden = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
    assert not [str(path) for path in SOURCES if hidden.search(path.read_text(encoding="utf-8"))]


def test_qml_runs_only_fixed_local_programs():
    service = QML["Service.qml"]
    programs = set(re.findall(r'\["([^"]+)"', CODE))
    assert programs <= {"/usr/bin/python3", "wl-copy", "omarchy-launch-floating-terminal-with-presentation",
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
    panel, guide = QML["Panel.qml"], QML["SetupGuide.qml"]
    # Rich text only for Muse's replies, and only the helper's escaped HTML.
    assert "textFormat: rich ? TextEdit.RichText : TextEdit.PlainText" in panel
    assert 'readonly property bool rich: chatRow.fromMuse && chatRow.messageHtml !== ""' in panel
    assert CODE.count("RichText") == 1 and "StyledText" not in CODE
    assert "onLinkActivated: function(link) { root.openLink(link) }" in panel
    assert "/^https?:\\/\\/[^\\s]+$/.test(url)" in panel
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
