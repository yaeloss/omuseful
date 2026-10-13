import QtQuick
import QtQuick.Shapes
import qs.Commons as Theme

// Original Omuseful mark: a rounded chat bubble with a four-point spark.
// Deliberately not a character or mascot, and not derived from Muse or
// Jollybot artwork. No downloaded artwork, fonts, or network requests.
Item {
  id: root
  property color foreground: Theme.Color.foreground
  implicitWidth: 32
  implicitHeight: implicitWidth

  Shape {
    width: 64
    height: 64
    scale: Math.min(root.width, root.height) / 64
    transformOrigin: Item.TopLeft
    antialiasing: true
    preferredRendererType: Shape.CurveRenderer

    ShapePath {
      strokeColor: root.foreground
      strokeWidth: 3.6
      fillColor: "transparent"
      capStyle: ShapePath.RoundCap
      joinStyle: ShapePath.RoundJoin
      // Speech bubble with a lower-left tail: Omuseful is a chat interface.
      PathSvg { path: "M 20 10 H 44 A 12 12 0 0 1 56 22 V 32 A 12 12 0 0 1 44 44 H 30 L 17 54 L 20 44 A 12 12 0 0 1 8 32 V 22 A 12 12 0 0 1 20 10 Z" }
    }
    ShapePath {
      strokeColor: "transparent"
      fillColor: root.foreground
      PathSvg { path: "M 32 17.5 Q 33.6 25.4 41.5 27 Q 33.6 28.6 32 36.5 Q 30.4 28.6 22.5 27 Q 30.4 25.4 32 17.5 Z" }
    }
  }
}
