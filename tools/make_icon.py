"""Draw Quill's icon and write quill.ico (16-256 px) plus a preview PNG.

A white form page with a folded corner and form lines, and a teal check
mark. Small sizes get a simpler drawing so they stay crisp in menus.

Run:  .venv\\Scripts\\python.exe tools\\make_icon.py
"""

import struct
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
TEAL = (0.06, 0.55, 0.53)       # check mark
TEAL_DARK = (0.03, 0.38, 0.37)
INK = (0.20, 0.23, 0.27)        # page outline
LINE = (0.70, 0.74, 0.78)       # form lines
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def draw(size: int) -> pymupdf.Pixmap:
    """Draw on a 100x100 canvas, then render at `size` pixels."""
    doc = pymupdf.open()
    page = doc.new_page(width=100, height=100)
    small = size <= 24
    sh = page.new_shape()

    # page with a folded top-right corner
    x0, y0, x1, y1 = (14, 6, 80, 94) if not small else (12, 4, 82, 96)
    fold = 20 if not small else 22
    outline = [(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)]
    sh.draw_polyline(outline + [outline[0]])
    sh.finish(color=INK, fill=(1, 1, 1), width=4.5 if not small else 7, closePath=True,
              lineJoin=1)
    sh.draw_polyline([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)])
    sh.finish(color=INK, fill=(0.88, 0.91, 0.93), width=4.5 if not small else 7, lineJoin=1,
              closePath=True)

    # form lines (fewer and thicker when tiny)
    rows = [30, 44, 58] if not small else [34, 52]
    for i, y in enumerate(rows):
        end = x1 - 12 if i else x1 - fold - 6
        sh.draw_line((x0 + 11, y), (end, y))
        sh.finish(color=LINE, width=5 if not small else 8, lineCap=1)
    sh.commit()

    # teal check mark over the lower right, with a dark edge for contrast
    # stays inside the 100x100 frame: endpoints are inset by half the stroke
    check = [(42, 70), (57, 85), (88, 52)] if not small else [(36, 68), (53, 85), (86, 50)]
    for color, width in ((TEAL_DARK, 18 if not small else 23), (TEAL, 11 if not small else 15)):
        s2 = page.new_shape()
        s2.draw_polyline(check)
        s2.finish(color=color, width=width, lineCap=1, lineJoin=1, closePath=False)
        s2.commit()

    zoom = size / 100
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)


def write_ico(pngs: list[tuple[int, bytes]], out: Path) -> None:
    """ICO file with PNG images inside (Windows Vista and later)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs = b"", b""
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        dim = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    out.write_bytes(header + entries + blobs)


def main() -> int:
    pngs = [(s, draw(s).tobytes("png")) for s in SIZES]
    ico = ROOT / "quill.ico"
    write_ico(pngs, ico)
    # preview: every size side by side on light and dark backgrounds
    doc = pymupdf.open()
    width = sum(SIZES) + 12 * len(SIZES) + 12
    page = doc.new_page(width=width, height=2 * 280)
    for row, bg in enumerate(((1, 1, 1), (0.13, 0.13, 0.15))):
        page.draw_rect(pymupdf.Rect(0, row * 280, width, row * 280 + 280), color=None, fill=bg)
        x = 12
        for s, data in pngs:
            page.insert_image(pymupdf.Rect(x, row * 280 + 12, x + s, row * 280 + 12 + s), stream=data)
            x += s + 12
    page.get_pixmap(dpi=72).save(str(ROOT / "chrome-check" / "icon-preview.png"))
    print(f"Wrote {ico} ({ico.stat().st_size} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
