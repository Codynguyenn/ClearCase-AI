import { describe, expect, it } from 'vitest'
import type { BBox, PageGeometry } from '../api/types'
import { bboxToCss, cssToBbox, renderedPageSize } from './geometry'

// A non-square page (400 x 500 points) so width/height mix-ups show up as failures.
const geometry: PageGeometry = { width: 400, height: 500, rotation: 0 }
const box: BBox = { x0: 10, y0: 20, x1: 50, y1: 40 }

describe('bboxToCss', () => {
  it('scales an unrotated box', () => {
    expect(bboxToCss(box, geometry, 0, 2)).toEqual({ left: 20, top: 40, width: 80, height: 40 })
  })

  // Hand-computed from the rotation rules: 90 = (h - y, x), 180 = (w - x, h - y), 270 = (y, w - x).
  it('rotates 90 degrees clockwise', () => {
    expect(bboxToCss(box, geometry, 90, 1)).toEqual({ left: 460, top: 10, width: 20, height: 40 })
  })

  it('rotates 180 degrees', () => {
    expect(bboxToCss(box, geometry, 180, 1)).toEqual({ left: 350, top: 460, width: 40, height: 20 })
  })

  it('rotates 270 degrees', () => {
    expect(bboxToCss(box, geometry, 270, 1)).toEqual({ left: 20, top: 350, width: 20, height: 40 })
  })

  it('treats 360 and negative rotations like their equivalents', () => {
    expect(bboxToCss(box, geometry, 360, 1)).toEqual(bboxToCss(box, geometry, 0, 1))
    expect(bboxToCss(box, geometry, -90, 1)).toEqual(bboxToCss(box, geometry, 270, 1))
  })

  it('always stays inside the rendered page', () => {
    const full: BBox = { x0: 0, y0: 0, x1: 400, y1: 500 }
    for (const rotation of [0, 90, 180, 270]) {
      for (const scale of [0.5, 1, 1.25, 3]) {
        const css = bboxToCss(full, geometry, rotation, scale)
        const size = renderedPageSize(geometry, rotation, scale)
        expect(css.left).toBeCloseTo(0)
        expect(css.top).toBeCloseTo(0)
        expect(css.width).toBeCloseTo(size.width)
        expect(css.height).toBeCloseTo(size.height)
      }
    }
  })
})

describe('renderedPageSize', () => {
  it('swaps width and height for quarter turns', () => {
    expect(renderedPageSize(geometry, 0, 1)).toEqual({ width: 400, height: 500 })
    expect(renderedPageSize(geometry, 90, 1)).toEqual({ width: 500, height: 400 })
    expect(renderedPageSize(geometry, 180, 2)).toEqual({ width: 800, height: 1000 })
    expect(renderedPageSize(geometry, 270, 0.5)).toEqual({ width: 250, height: 200 })
  })
})

describe('cssToBbox (inverse of bboxToCss)', () => {
  const rotations = [0, 90, 180, 270]
  const scales = [0.5, 0.75, 1, 1.25, 1.5, 2, 3]
  // Different page shapes, including one that models a cropped page (small, offset origin is
  // already removed by the backend, so geometry is simply the visible size).
  const geometries: PageGeometry[] = [
    { width: 400, height: 500, rotation: 0 },
    { width: 612, height: 792, rotation: 90 },
    { width: 350, height: 240, rotation: 0 },
  ]

  it('round-trips for every rotation, zoom and page shape', () => {
    for (const g of geometries) {
      for (const rotation of rotations) {
        for (const scale of scales) {
          const original: BBox = { x0: 12.5, y0: 33.25, x1: 120.75, y1: 61.5 }
          const css = bboxToCss(original, g, rotation, scale)
          const back = cssToBbox(css, g, rotation, scale)
          expect(back.x0).toBeCloseTo(original.x0, 6)
          expect(back.y0).toBeCloseTo(original.y0, 6)
          expect(back.x1).toBeCloseTo(original.x1, 6)
          expect(back.y1).toBeCloseTo(original.y1, 6)
        }
      }
    }
  })

  it('maps a drag on a rotated page to the right place in page space', () => {
    // The known 90-degree case from above, drawn at 2x zoom.
    const dragged = { left: 920, top: 20, width: 40, height: 80 }
    expect(cssToBbox(dragged, geometry, 90, 2)).toEqual({ x0: 10, y0: 20, x1: 50, y1: 40 })
  })

  it('normalises a drag made right-to-left / bottom-to-top', () => {
    const css = { left: 100, top: 100, width: -50, height: -20 }
    const b = cssToBbox(css, geometry, 0, 1)
    expect(b).toEqual({ x0: 50, y0: 80, x1: 100, y1: 100 })
  })

  it('clamps a drag that leaves the page', () => {
    const b = cssToBbox({ left: -30, top: 480, width: 600, height: 100 }, geometry, 0, 1)
    expect(b).toEqual({ x0: 0, y0: 480, x1: 400, y1: 500 })
  })
})
