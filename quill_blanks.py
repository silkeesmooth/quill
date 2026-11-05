"""Find the blanks on a flat or scanned form so they can become form fields.

The page is rendered to a grayscale image and searched for:
  - lines with empty space above them (write-on lines, boxes, table cells)
  - small empty squares (checkboxes)
Working from pixels means the same code handles digital and scanned pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

ZOOM = 2.0                 # render at 144 dpi; pixels per point
ONE = 49

MIN_LINE_PT = 36           # shortest open underline that counts as a blank
MIN_CELL_PT = 10           # narrowest closed cell (between two vertical lines)
MIN_H_PT = 8               # least empty room above a line for a text field
MAX_H_PT = 20              # tallest single-line field
MAX_BOX_PT = 300           # tallest box that becomes a multi-line field
MAX_THICK_PT = 3           # thicker "lines" are bars or shading, not blanks
BOX_MIN_PT, BOX_MAX_PT = 6, 18   # checkbox side length


@dataclass
class Blank:
    kind: str              # "text", "multiline" or "checkbox"
    rect: pymupdf.Rect     # visible page coordinates, points, top-left origin
    label: str = ""
    closed: bool = False   # inside a box/cell (vs. an open write-on line)


def px(pt: float) -> int:
    return int(round(pt * ZOOM))


def ink_table(samples: bytes) -> bytes:
    """Lookup table marking "ink" pixels. Forms often print lines in light gray,
    so anything clearly darker than the paper counts; the paper shade comes from
    the most common bright value (white for digital pages, grayer for scans)."""
    sample = samples[::97]
    counts = [0] * 256
    for v in sample:
        counts[v] += 1
    paper = max(range(128, 256), key=lambda v: counts[v])
    cut = max(150, min(225, paper - 30))
    return bytes(49 if v < cut else 48 for v in range(256))


class Raster:
    def __init__(self, page: pymupdf.Page):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM),
                              colorspace=pymupdf.csGRAY, alpha=False)
        self.w, self.h = pix.width, pix.height
        s, st = pix.samples, pix.stride
        table = ink_table(s)
        self.rows = [s[y * st:y * st + self.w].translate(table) for y in range(self.h)]

    def dark(self, y: int, x0: int, x1: int) -> int:
        return self.rows[y].count(b"1", max(0, x0), min(self.w, x1))

    def inked_near(self, x: int, y: int, reach: int = 1) -> bool:
        return any(self.rows[yy][xx] == ONE
                   for yy in range(max(0, y - reach), min(self.h, y + reach + 1))
                   for xx in range(max(0, x - reach), min(self.w, x + reach + 1)))

    def column_solid(self, x: int, y0: int, y1: int, frac: float = 1.0) -> bool:
        """Is column x dark in at least `frac` of rows y0..y1-1?"""
        if not (0 <= x < self.w) or y0 < 0 or y1 > self.h or y1 <= y0:
            return False
        need = frac * (y1 - y0)
        n = sum(1 for y in range(y0, y1) if self.rows[y][x] == ONE)
        return n >= need


# ---------------------------------------------------------------- lines


def find_lines(r: Raster) -> list[list[int]]:
    """Horizontal strokes as [x0, x1, y0, y1] (x1 exclusive, y1 inclusive)."""
    min_px = px(BOX_MIN_PT)
    pat = re.compile(b"1{%d,}" % min_px)
    groups: list[list[int]] = []
    prev: list[list[int]] = []
    for y, row in enumerate(r.rows):
        cur = []
        for m in pat.finditer(row):
            x0, x1 = m.start(), m.end()
            g = next((p for p in prev if abs(p[0] - x0) <= 3 and abs(p[1] - x1) <= 3), None)
            if g is None:
                g = [x0, x1, y, y]
                groups.append(g)
            else:
                g[0], g[1], g[3] = min(g[0], x0), max(g[1], x1), y
            cur.append(g)
        prev = cur
    thin = [g for g in groups if g[3] - g[2] + 1 <= px(MAX_THICK_PT)]
    return merge_collinear(thin)


def merge_collinear(lines: list[list[int]]) -> list[list[int]]:
    """Join pieces of one line that a slight scan skew broke apart."""
    # Walk pieces left to right; a piece continues a line only if it starts
    # at that line's right end, at about the same height as that end, so
    # text near a tilted line isn't glued onto it.
    out: list[list[int]] = []   # [x0, x1, y0, y1, end_x0, end_x1, end_ymid]
    for g in sorted(lines, key=lambda g: (g[0], g[2])):
        gy = (g[2] + g[3]) / 2
        best = None
        for o in out:
            if o[4] <= g[0] <= o[5] + 6 and g[1] > o[5] and abs(gy - o[6]) <= 3:
                if best is None or abs(gy - o[6]) < abs(gy - best[6]):
                    best = o
        if best is not None and (best[1] - best[0] > px(MIN_LINE_PT) // 2
                                 or g[1] - g[0] > px(MIN_LINE_PT) // 2):
            best[1] = g[1]
            best[2], best[3] = min(best[2], g[2]), max(best[3], g[3])
            best[4], best[5], best[6] = g[0], g[1], gy
        else:
            out.append([g[0], g[1], g[2], g[3], g[0], g[1], gy])
    return [o[:4] for o in out]


# ---------------------------------------------------------------- checkboxes


def dark_run(row: bytes, start: int, step: int, stop: int) -> int:
    """From `start`, skip light pixels, then count dark ones, moving by `step`."""
    x, n = start, 0
    inside = lambda v: (v < stop) if step > 0 else (v > stop)  # noqa: E731
    while inside(x) and 0 <= x < len(row) and row[x] != ONE:
        x += step
    while inside(x) and 0 <= x < len(row) and row[x] == ONE:
        n += 1
        x += step
    return n


def find_checkboxes(r: Raster, lines) -> list[pymupdf.Rect]:
    lo, hi = px(BOX_MIN_PT), px(BOX_MAX_PT)
    short = [g for g in lines if lo <= g[1] - g[0] <= hi]
    by_x: dict[int, list] = {}
    for g in short:
        by_x.setdefault(g[0] // 4, []).append(g)
    boxes = []
    for a in short:
        w = a[1] - a[0]
        for key in (a[0] // 4 - 1, a[0] // 4, a[0] // 4 + 1):
            for b in by_x.get(key, ()):
                if b is a or b[2] <= a[3] or abs(a[0] - b[0]) > 3 or abs(a[1] - b[1]) > 3:
                    continue
                side = b[3] - a[2]
                if not 0.7 * w <= side <= 1.4 * w:
                    continue
                y0, y1 = a[3] + 1, b[2]
                if y1 - y0 < 3:
                    continue
                left = any(r.column_solid(x, y0, y1, 0.85) for x in range(a[0] - 1, a[0] + 3))
                right = any(r.column_solid(x, y0, y1, 0.85) for x in range(a[1] - 3, a[1] + 1))
                if not (left and right):
                    continue
                inner = sum(r.dark(y, a[0] + 3, a[1] - 3) for y in range(y0 + 2, y1 - 2))
                area = max(1, (w - 6) * (y1 - y0 - 4))
                if inner > 0.1 * area:
                    continue
                # Bold letters (O, D, B...) also look like boxes, but their side
                # strokes are thick, and thicker than their top and bottom.
                # Printed boxes have thin edges of about equal weight.
                mid = r.rows[(a[2] + b[3]) // 2]
                sides = max(dark_run(mid, a[0] - 2, 1, a[1]), dark_run(mid, a[1] + 1, -1, a[0]))
                flat = max(a[3] - a[2] + 1, b[3] - b[2] + 1)
                if sides > 0.2 * w or sides - flat >= 2:
                    continue
                boxes.append(pymupdf.Rect(a[0], a[2], a[1], b[3] + 1) / ZOOM)
    return dedupe(boxes)


# ---------------------------------------------------------------- text blanks


def separators(r: Raster, x0: int, x1: int, y_top: int, rows: int, up: bool) -> list[int]:
    """Columns in x0..x1 that are solid for `rows` rows above (or below) y_top."""
    ys = (y_top - rows, y_top) if up else (y_top + 1, y_top + 1 + rows)
    if ys[0] < 0 or ys[1] > r.h:
        return []
    probe = r.rows[ys[1] - 1] if up else r.rows[ys[0]]
    cols = [x for x in range(max(0, x0), min(r.w, x1)) if probe[x] == ONE]
    return [x for x in cols if r.column_solid(x, ys[0], ys[1])]


def split_runs(xs: list[int]) -> list[tuple[int, int]]:
    """Group sorted columns into (start, end) runs."""
    runs: list[tuple[int, int]] = []
    for x in xs:
        if runs and x <= runs[-1][1] + 2:
            runs[-1] = (runs[-1][0], x)
        else:
            runs.append((x, x))
    return runs


def find_text_blanks(r: Raster, lines) -> list[Blank]:
    minh, maxh, maxbox = px(MIN_H_PT), px(MAX_H_PT), px(MAX_BOX_PT)
    out: list[Blank] = []
    for x0, x1, y0, y1 in lines:
        if x1 - x0 < px(MIN_CELL_PT) or y0 - minh < 0:
            continue
        up = split_runs(separators(r, x0 - 3, x1 + 3, y0, minh, up=True))
        down = split_runs(separators(r, x0 - 3, x1 + 3, y1, minh, up=False))
        at_ends = lambda runs, x: any(a - 4 <= x <= b + 4 for a, b in runs)  # noqa: E731
        up_both = at_ends(up, x0) and at_ends(up, x1 - 1)
        down_both = at_ends(down, x0) and at_ends(down, x1 - 1)
        if down_both and not up_both:
            continue  # top edge of a box: the space above is outside it

        # cut the line into cells at the vertical lines standing on it
        cuts = [(a, b) for a, b in up if a > x0 + 4 and b < x1 - 5]
        edges = [x0] + [c for run in cuts for c in run] + [x1]
        cells = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2)]
        closed = up_both or bool(cuts)
        min_w = px(MIN_CELL_PT) if closed else px(MIN_LINE_PT)
        for sx0, sx1 in cells:
            ix0, ix1 = sx0 + 3, sx1 - 3
            if ix1 - ix0 < min_w - 6:
                continue
            noise = max(2, (ix1 - ix0) // 100)
            y = y0 - 3  # the two rows touching the line are its anti-aliased fringe
            while y >= 0 and (y0 - 1 - y) < maxbox and r.dark(y, ix0, ix1) <= noise:
                y -= 1
            gap = (y0 - 1) - y
            if gap < minh:
                continue
            kind = "text"
            if gap > maxh:
                closed_above = y >= 0 and r.dark(y, ix0, ix1) >= 0.9 * (ix1 - ix0)
                if closed and closed_above and gap > 2 * maxh:
                    top = y + 3
                    kind = "multiline"
                else:
                    top = y0 - maxh
            else:
                top = y + 3
            rect = pymupdf.Rect(ix0, top, ix1, y0 - 1) / ZOOM
            if rect.height >= MIN_H_PT - 2:
                out.append(Blank(kind, rect, closed=closed))
    return out


# ---------------------------------------------------------------- labels


def overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """Share of the smaller rect covered by the other."""
    i = pymupdf.Rect(a) & b
    if i.is_empty:
        return 0.0
    small = min(a.width * a.height, b.width * b.height) or 1
    return (i.width * i.height) / small


def dedupe(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    out: list[pymupdf.Rect] = []
    for r in rects:
        if all(overlap(r, o) < 0.3 for o in out):
            out.append(r)
    return out


BOX_GLYPHS = set("\u25a1\u2610\u2611\u2612\u274f\u2750\u2751\u2752\u25a2\u25fb\u25fd")


def page_words(page: pymupdf.Page):
    """Words as (visible rect, text, block, line)."""
    rot = page.rotation_matrix
    return [(pymupdf.Rect(w[:4]) * rot, w[4], w[5], w[6]) for w in page.get_text("words")]


def on_text(box: pymupdf.Rect, words) -> bool:
    """Does a "checkbox" actually sit on printed text (e.g. a bold O)?"""
    for w in words:
        if overlap(box, w[0]) > 0.25 and not (set(w[1]) & BOX_GLYPHS):
            return True
    return False


def clean_label(words: list[str]) -> str:
    s = " ".join(words)
    s = re.sub(r"_{2,}", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" :_.-\u2013\u2014()")
    return s[:60]


def wall_between(r: Raster, x_from: float, x_to: float, rect: pymupdf.Rect) -> bool:
    """Is there a vertical line between x_from and x_to beside the field?"""
    y0, y1 = px(rect.y0) + 1, px(rect.y1) - 1
    if y1 - y0 < 3:
        return False
    return any(r.column_solid(x, y0, y1, 0.85) for x in range(px(x_from) + 1, px(x_to)))


def same_line_run(words, anchor, keep, forward: bool):
    """Words on anchor's text line that pass `keep`, stopping at big gaps."""
    run = sorted((w for w in words if w[2] == anchor[2] and w[3] == anchor[3] and keep(w)),
                 key=lambda w: w[0].x0, reverse=not forward)
    picked = []
    for w in run:
        if picked:
            gap = (w[0].x0 - picked[-1][0].x1) if forward else (picked[-1][0].x0 - w[0].x1)
            if gap > 15:
                break
        picked.append(w)
        if len(picked) >= 8:
            break
    return picked if forward else picked[::-1]


def beside(w, rect) -> bool:
    """Is word w on the same text line as the field (sitting on its line)?"""
    wy = (w[0].y0 + w[0].y1) / 2
    return rect.y0 - 2 <= wy <= rect.y1 + 3


def label_left(r, rect, words, max_gap=90):
    near = [w for w in words if w[0].x1 <= rect.x0 + 2 and rect.x0 - w[0].x1 < max_gap
            and beside(w, rect)]
    near = [w for w in near if not wall_between(r, w[0].x1, rect.x0, rect)]
    if not near:
        return ""
    anchor = max(near, key=lambda w: w[0].x1)
    picked = same_line_run(words, anchor, lambda w: w[0].x1 <= rect.x0 + 2, forward=False)
    return clean_label([w[1] for w in picked])


def label_right(r, rect, words, max_gap=12):
    near = [w for w in words if w[0].x0 >= rect.x1 - 2 and w[0].x0 - rect.x1 < max_gap
            and w[0].y0 - 2 <= (rect.y0 + rect.y1) / 2 <= w[0].y1 + 2]
    if not near:
        return ""
    anchor = min(near, key=lambda w: w[0].x0)
    picked = same_line_run(words, anchor, lambda w: w[0].x0 >= rect.x1 - 2, forward=True)
    return clean_label([w[1] for w in picked])


def label_band(rect, words, y0, y1):
    """Words in a horizontal band overlapping the field's x-span."""
    hits = [w for w in words if (w[0].y0 + w[0].y1) / 2 >= y0 and (w[0].y0 + w[0].y1) / 2 <= y1
            and w[0].x1 > rect.x0 - 2 and w[0].x0 < rect.x1 + 2]
    if not hits:
        return ""
    first_line = min(hits, key=lambda w: w[0].y0)
    row = [w for w in hits if abs(w[0].y0 - first_line[0].y0) < 3]
    row.sort(key=lambda w: w[0].x0)
    return clean_label([w[1] for w in row[:8]])


def row_header(r, rect, words) -> str:
    """Text in the cell to the left (behind a vertical line), for context."""
    near = [w for w in words if w[0].x1 <= rect.x0 and beside(w, rect)
            and wall_between(r, w[0].x1, rect.x0, rect)]
    if not near:
        return ""
    anchor = max(near, key=lambda w: w[0].x1)
    picked = same_line_run(words, anchor, lambda w: w[0].x1 <= rect.x0, forward=False)
    return clean_label([w[1] for w in picked])


def find_label(r: Raster, b: Blank, words) -> str:
    rect = b.rect
    if b.kind == "checkbox":
        return label_right(r, rect, words) or label_left(r, rect, words)
    own = label_left(r, rect, words)
    if not own:
        above = label_band(rect, words, rect.y0 - 12, rect.y0 + 2)
        below = label_band(rect, words, rect.y1, rect.y1 + 11)
        # an open write-on line is usually captioned underneath; a box or
        # cell carries its label at the top, inside
        own = (above or below) if b.closed else (below or above)
    head = row_header(r, rect, words) if b.closed else ""
    if b.closed and len(own.split()) <= 3:
        if head and head.lower() != own.lower():
            own = f"{head}: {own}" if own else head
    # Keep context that says the field is about someone else (or office use):
    # "EMERGENCY CONTACT" above a PHONE line, "MOTHER'S NAME" left of a row.
    for ctx in [head] + headings_above(rect, words):
        if ctx and OTHER_PERSON.search(ctx) and ctx.lower() not in own.lower():
            own = f"{ctx}: {own}" if own else ctx
            break
    return own[:90]


OTHER_PERSON = re.compile(
    r"\b(mother'?s?|father'?s?|parents?'?|spouse'?s?|husband'?s?|wife'?s?|partner'?s?|emergency|"
    r"child(ren)?'?s?|dependents?|beneficiar\w*|guardian'?s?|witness|supervisor'?s?|"
    r"preparer'?s?|office|official)\b", re.I)


def headings_above(rect, words, reach=80):
    """Short text lines up to `reach` points above the field, over its span."""
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault((w[2], w[3]), []).append(w)
    found = []
    for line in lines.values():
        box = pymupdf.Rect(line[0][0])
        for w in line[1:]:
            box |= w[0]
        if len(line) <= 8 and rect.y0 - reach <= box.y1 <= rect.y0 + 1 \
                and box.x1 > rect.x0 and box.x0 < rect.x1:
            found.append((box.y1, clean_label([w[1] for w in sorted(line, key=lambda w: w[0].x0)])))
    return [t for _, t in sorted(found, reverse=True)]


# ---------------------------------------------------------------- entry point


def find_blanks(page: pymupdf.Page, avoid: list[pymupdf.Rect] | None = None) -> list[Blank]:
    """All blanks on the page, in reading order, skipping areas in `avoid`
    (visible coordinates, e.g. existing fields)."""
    r = Raster(page)
    lines = find_lines(r)
    words = page_words(page)
    taken = list(avoid or [])
    blanks: list[Blank] = []
    for box in find_checkboxes(r, lines):
        if on_text(box, words):
            continue
        if all(overlap(box, t) < 0.3 for t in taken):
            blanks.append(Blank("checkbox", box, closed=True))
            taken.append(box)
    for b in find_text_blanks(r, lines):
        if all(overlap(b.rect, t) < 0.2 for t in taken):
            blanks.append(b)
            taken.append(b.rect)
    for b in blanks:
        b.label = find_label(r, b, words)
    blanks.sort(key=lambda b: (round(b.rect.y0 / 6), b.rect.x0))
    return blanks
