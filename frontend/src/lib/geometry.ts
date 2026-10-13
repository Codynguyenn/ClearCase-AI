import type { BBox, PageGeometry } from '../api/types'

/**
 * Convert a rectangle from PDF extraction space into CSS pixels on the rendered page.
 *
 * Extraction space (what the backend stores): PDF points, origin at the top-left
 * of the *unrotated* page, y growing downwards. This is PyMuPDF's convention.
 *
 * The viewer renders the page rotated by `totalRotation` degrees (the page's own
 * /Rotate value plus whatever the user added) and scaled by `scale`. We rotate the
 * two corners, re-normalise so x0<x1 and y0<y1, then scale. Rotation is
 * clockwise, matching PDF.js and PyMuPDF (90 = "rotated to the right").
 */
export function bboxToCss(
  bbox: BBox,
  geometry: PageGeometry,
  totalRotation: number,
  scale: number,
): { left: number; top: number; width: number; height: number } {
  const r = ((totalRotation % 360) + 360) % 360
  const { width: w, height: h } = geometry

  const rotatePoint = (x: number, y: number): [number, number] => {
    switch (r) {
      case 90:
        return [h - y, x]
      case 180:
        return [w - x, h - y]
      case 270:
        return [y, w - x]
      default:
        return [x, y]
    }
  }

  const [ax, ay] = rotatePoint(bbox.x0, bbox.y0)
  const [bx, by] = rotatePoint(bbox.x1, bbox.y1)
  const left = Math.min(ax, bx)
  const top = Math.min(ay, by)
  const right = Math.max(ax, bx)
  const bottom = Math.max(ay, by)

  return {
    left: left * scale,
    top: top * scale,
    width: (right - left) * scale,
    height: (bottom - top) * scale,
  }
}

/** Size in CSS pixels of a page rendered with the given rotation and scale. */
export function renderedPageSize(
  geometry: PageGeometry,
  totalRotation: number,
  scale: number,
): { width: number; height: number } {
  const r = ((totalRotation % 360) + 360) % 360
  const swap = r === 90 || r === 270
  return {
    width: (swap ? geometry.height : geometry.width) * scale,
    height: (swap ? geometry.width : geometry.height) * scale,
  }
}

/**
 * Inverse of {@link bboxToCss}: turn a rectangle dragged on the rendered page (CSS
 * pixels, origin at the page's top-left corner as displayed) back into extraction
 * space, which is what the backend stores.
 *
 * Steps: undo the zoom, undo the rotation (using the same clockwise convention),
 * then normalise and clamp to the page so a drag that leaves the page edge still
 * yields a valid rectangle.
 */
export function cssToBbox(
  rect: { left: number; top: number; width: number; height: number },
  geometry: PageGeometry,
  totalRotation: number,
  scale: number,
): BBox {
  const r = ((totalRotation % 360) + 360) % 360
  const { width: w, height: h } = geometry

  const unrotatePoint = (X: number, Y: number): [number, number] => {
    switch (r) {
      case 90:
        return [Y, h - X]
      case 180:
        return [w - X, h - Y]
      case 270:
        return [w - Y, X]
      default:
        return [X, Y]
    }
  }

  const [ax, ay] = unrotatePoint(rect.left / scale, rect.top / scale)
  const [bx, by] = unrotatePoint((rect.left + rect.width) / scale, (rect.top + rect.height) / scale)
  const clamp = (v: number, max: number) => Math.min(Math.max(v, 0), max)
  return {
    x0: clamp(Math.min(ax, bx), w),
    y0: clamp(Math.min(ay, by), h),
    x1: clamp(Math.max(ax, bx), w),
    y1: clamp(Math.max(ay, by), h),
  }
}
