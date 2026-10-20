# Changelog

## 0.1.4 (unreleased)

- Fix first-time pairing: restore the SDK token in the encrypted
  `pairing_confirmed` record, only after local terminal confirmation. Remove the
  ineffective startup refresh that attempted to register it over HTTPS.
- Record a root-only token hint and offer explicit re-pairing when a replacement
  token no longer matches. No token or hint enters the plugin's public status.
- Add per-message delivery states, editable recovery for confirmed failures,
  conservative handling of uncertain delivery, and offline drafting.
- Show working/unread state in the bar and add opt-in, fixed-text reply
  notifications using Omarchy's DND-aware notification service. Notification
  clicks summon Chat rather than toggling an already-open panel closed.
- Add offline pairing-contract regression tests and real Qt 6 chat-state tests,
  including partial streams and multi-monitor read tracking.

### Updating the companion

Updating the plugin alone does not replace the installed companion. Use
**Setup → Run setup** and review the changed-source confirmation. Retry pairing
in the Muse app if setup previously failed with “this gadget doesn't have an
SDK token.” If already paired and replacing a token, run
`sudo musegadget pair --force` in a terminal and confirm there.

The pairing security description has changed: the token is sent to the phone
inside the encrypted pairing confirmation, as required by the upstream SDK.
Local confirmation remains mandatory, but community pairing does not protect
against an active nearby man-in-the-middle. See [SECURITY.md](SECURITY.md#pairing).
