import QtQuick
import QtTest
import "../.." as Omuseful

TestCase {
  id: testCase
  name: "LinkSafety"
  property var safety: null
  property var shared: []

  Component { id: safetyComponent; Omuseful.LinkSafety { } }

  function init() {
    safety = createTemporaryObject(safetyComponent, testCase)
    verify(safety !== null)
  }

  function initTestCase() {
    var xhr = new XMLHttpRequest()
    xhr.open("GET", Qt.resolvedUrl("../link_vectors.json"))
    var finished = false
    xhr.onreadystatechange = function() {
      if (xhr.readyState === XMLHttpRequest.DONE) {
        testCase.shared = JSON.parse(xhr.responseText)
        finished = true
      }
    }
    xhr.send()
    tryVerify(function() { return finished }, 5000)
    verify(shared.length > 0)
  }

  function test_shared_vectors() {
    for (var i = 0; i < shared.length; i++) {
      compare(safety.isSafeLink(shared[i].url), shared[i].valid,
        "vector " + i + ": " + shared[i].url)
    }
  }

  function test_length_boundaries() {
    // Mirrored in test_bridge.py; overlong input is never a link.
    verify(safety.isSafeLink("https://example.com/" + "a".repeat(2048 - 20)))
    verify(!safety.isSafeLink("https://example.com/" + "a".repeat(2049 - 20)))
    verify(safety.isSafeLink("https://" + "a".repeat(253) + "/"))
    verify(!safety.isSafeLink("https://" + "a".repeat(254) + "/"))
  }
}
