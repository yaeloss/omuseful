import QtQuick

// Second link check, mirroring the helper's safe_url: at most 2048 chars of
// printable ASCII, a real host (or a valid bracketed IPv6 literal), a port
// within range, no backslash, and no user@ part hiding the host. Behavior is
// pinned by tests/link_vectors.json, shared with the Python suite.
QtObject {
  function isSafeLink(link) {
    var url = String(link)
    if (url.length > 2048 || url.indexOf("\\") >= 0) return false
    for (var i = 0; i < url.length; i++) {
      var code = url.charCodeAt(i)
      if (code < 0x21 || code > 0x7E) return false
    }
    // Case-insensitive scheme; an empty port means the default port, as in a
    // browser; overlong digit runs still fail the range check below.
    var parts = /^https?:\/\/(\[[^\]]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::([0-9]*))?(?:[\/?#]\S*)?$/i.exec(url)
    if (!parts) return false
    if (parts[2] !== undefined && parseInt(parts[2], 10) > 65535) return false
    if (parts[1].charAt(0) !== '[' && parts[1].length > 253) return false
    if (parts[1].charAt(0) === '[') {
      // Bracketed IPv6: "::" at most once, no ":::", 1-4 hex digits a group,
      // 8 groups without compression, fewer with it.
      var inner = parts[1].substring(1, parts[1].length - 1)
      if (!/^[0-9A-Fa-f:]+$/.test(inner) || inner.indexOf(":::") >= 0) return false
      var halves = inner.split("::")
      if (halves.length > 2) return false
      var groups = inner.split(":").filter(function(group) { return group.length > 0 })
      for (var g = 0; g < groups.length; g++) if (groups[g].length > 4) return false
      if (halves.length === 1 ? groups.length !== 8 : groups.length >= 8) return false
    }
    return true
  }
}
