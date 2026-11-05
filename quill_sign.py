"""Signatures: save yours once, then put it on the signature line of any form.

This places a picture of a signature (from a photo, or your name in a
handwriting font). It is not a certificate-based digital signature.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

SIG_WORDS = re.compile(r"\b(signature|sign here|signed by)\b", re.I)
DATE_WORDS = re.compile(r"\b(date|today'?s?)\b", re.I)
TODAY = re.compile(r"\btoday", re.I)
OFFICE = re.compile(r"office\s+use|official\s+use|do not write", re.I)
NOT_TODAY = re.compile(r"\b(birth|dob|expir|issue[d]?)\b", re.I)
INK = (0.05, 0.10, 0.40)     # dark blue-black, like a pen
MIN_SIG_HEIGHT = 22          # points; signatures look cramped smaller than this
MAX_SIG_HEIGHT = 32          # and oversized taller than this

FONTS = {   # handwriting fonts that ship with Windows
    "script": "segoesc.ttf",     # Segoe Script
    "print": "segoepr.ttf",      # Segoe Print
    "inkfree": "Inkfree.ttf",    # Ink Free
}


class SignError(Exception):
    pass


# ---------------------------------------------------------------- saved signature


def home() -> Path:
    """Where your signature and details live: ~/.quill on Windows,
    ~/.config/quill elsewhere.

    Not %APPDATA%: Microsoft Store Python quietly redirects AppData writes into
    its own package folder, which Explorer and other apps can't see."""
    if os.environ.get("QUILL_HOME"):
        return Path(os.environ["QUILL_HOME"])
    if os.name == "nt":
        new = Path.home() / ".quill"
        old = [Path.home() / ".pdffill"]  # this tool's old name
        if os.environ.get("APPDATA"):
            old.append(Path(os.environ["APPDATA"]) / "pdffill")  # oldest location
    else:
        new = Path.home() / ".config" / "quill"
        old = [Path.home() / ".config" / "pdffill"]
    for o in old:
        _move_old_data(o, new)
    return new


def _move_old_data(old: Path, new: Path) -> None:
    """One-time move of saved details and signature from an older location."""
    if new.exists() or not old.is_dir():
        return
    new.mkdir(parents=True)
    for f in old.iterdir():
        if f.is_file():
            (new / f.name).write_bytes(f.read_bytes())
    for f in list(old.iterdir()):
        if f.is_file() and (new / f.name).is_file():
            f.unlink()
    try:
        old.rmdir()
    except OSError:
        pass


def load_saved() -> dict | None:
    try:
        data = json.loads((home() / "signature.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("kind") in ("image", "vector") and not (home() / data.get("file", "")).is_file():
        return None
    return data


def describe(sig: dict) -> str:
    if sig["kind"] == "image":
        return f"your signature image ({home() / sig['file']})"
    if sig["kind"] == "vector":
        return f"your signature artwork ({home() / sig['file']})"
    return f"your typed name \"{sig['name']}\" in {sig.get('font', 'script')} handwriting"


def font_file(font: str) -> str | None:
    """Path to a handwriting font, or None to fall back to italic Times."""
    if font and Path(font).is_file():
        return font
    name = FONTS.get((font or "script").lower())
    if not name:
        raise SignError(f"Unknown font '{font}'. Use one of: {', '.join(FONTS)} or a .ttf path.")
    windir = os.environ.get("WINDIR", r"C:\Windows")
    p = Path(windir) / "Fonts" / name
    return str(p) if p.is_file() else None


def save_name(name: str, font: str = "script") -> dict:
    name = name.strip()
    if not name:
        raise SignError("The name is empty.")
    font_file(font)  # validates the choice
    data = {"kind": "name", "name": name, "font": font}
    _write(data)
    return data


def save_image(photo: str | Path, rotate: int = 0) -> dict:
    home().mkdir(parents=True, exist_ok=True)
    out = home() / "signature.png"
    clean_photo(photo, out, rotate)
    data = {"kind": "image", "file": out.name}
    _write(data)
    return data


def save_vector(art: str | Path) -> tuple[dict, int]:
    """Use vector artwork (PDF, Illustrator .ai or SVG) of a signature.
    Returns the saved entry and how many small shapes were left out."""
    home().mkdir(parents=True, exist_ok=True)
    out = home() / "signature.pdf"
    dropped = extract_vector(art, out)
    data = {"kind": "vector", "file": out.name}
    _write(data)
    return data, dropped


SAVED_FILES = ("signature.json", "signature.png", "signature.pdf", "signature-preview.png")


def remove_saved() -> bool:
    had = load_saved() is not None
    for n in SAVED_FILES:
        try:
            (home() / n).unlink()
        except FileNotFoundError:
            pass
    return had


def _write(data: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    keep = {"image": "signature.png", "vector": "signature.pdf"}.get(data["kind"])
    for n in ("signature.png", "signature.pdf"):  # drop the other kind's file
        if n != keep:
            try:
                (home() / n).unlink()
            except FileNotFoundError:
                pass
    (home() / "signature.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- vector artwork

VECTOR_TYPES = (".pdf", ".ai", ".svg")


def is_vector_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VECTOR_TYPES


def extract_vector(art: str | Path, out: Path) -> int:
    """Copy the signature shapes from artwork into a small PDF, redrawn in
    pen ink. Leaves out a full-page background and small shapes such as a
    tagline under the signature. Returns how many shapes were left out."""
    p = Path(art)
    if not p.is_file():
        raise SignError(f"File not found: {art}")
    try:
        src = pymupdf.open(p)
        page = src[0]
        paths = page.get_drawings()
    except Exception as e:  # noqa: BLE001
        raise SignError(f"Could not read {art}: {e}") from e
    area = page.rect.width * page.rect.height
    shapes = [d for d in paths if d["rect"].width * d["rect"].height < 0.8 * area]
    if not shapes:
        raise SignError(f"{p.name} has no drawn shapes to use. If it's a picture, "
                        "save it as PNG or JPG and use that instead.")
    tallest = max(d["rect"].height for d in shapes)
    keep = [d for d in shapes if d["rect"].height >= 0.2 * tallest]
    box = pymupdf.Rect(keep[0]["rect"])
    for d in keep[1:]:
        box |= d["rect"]
    pad = box.height * 0.04
    box = box + (-pad, -pad, pad, pad)

    doc = pymupdf.open()
    dst = doc.new_page(width=box.width, height=box.height)
    shape = dst.new_shape()
    shift = pymupdf.Matrix(1, 0, 0, 1, -box.x0, -box.y0)
    for d in keep:
        for item in d["items"]:
            kind, args = item[0], [a * shift if hasattr(a, "x") or hasattr(a, "x0") or
                                   hasattr(a, "ul") else a for a in item[1:]]
            if kind == "l":
                shape.draw_line(args[0], args[1])
            elif kind == "c":
                shape.draw_bezier(args[0], args[1], args[2], args[3])
            elif kind == "re":
                shape.draw_rect(args[0])
            elif kind == "qu":
                shape.draw_quad(args[0])
        filled = d.get("fill") is not None
        stroked = d.get("color") is not None and "s" in d["type"]
        shape.finish(fill=INK if filled else None, color=INK if stroked else None,
                     width=d.get("width") or 1, even_odd=bool(d.get("even_odd")),
                     closePath=bool(d.get("closePath")))
    shape.commit()
    doc.save(out, garbage=3, deflate=True)
    return len(shapes) - len(keep)


def preview_png(sig_pdf: Path) -> Path:
    out = sig_pdf.with_name("signature-preview.png")
    page = pymupdf.open(sig_pdf)[0]
    page.get_pixmap(matrix=pymupdf.Matrix(600 / page.rect.width, 600 / page.rect.width)).save(str(out))
    return out


# ---------------------------------------------------------------- photo cleanup


def _load_rgb(photo: str | Path, rotate: int) -> pymupdf.Pixmap:
    p = Path(photo)
    if not p.is_file():
        raise SignError(f"File not found: {photo}")
    try:
        # Opening the photo as a document applies the phone's rotation tag.
        doc = pymupdf.open(p)
        page = doc[0]
        if rotate:
            page.set_rotation((page.rotation + rotate) % 360)
        # work at the photo's own resolution, up to 1600 px on the long side
        raw = pymupdf.Pixmap(str(p))
        native = max(raw.width, raw.height) or 1600
        del raw
        scale = min(native, 1600) / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB,
                              alpha=False)
    except Exception as e:  # noqa: BLE001
        raise SignError(f"Could not read {photo} as an image: {e}") from e
    return pix


def clean_photo(photo: str | Path, out: Path, rotate: int = 0) -> Path:
    """Turn a photo of a signature on paper into a cropped PNG with a
    transparent background, keeping the pen's color."""
    pix = _load_rgb(photo, rotate)
    w, h = pix.width, pix.height
    rgb = pix.samples
    st = pix.stride
    gray = pymupdf.Pixmap(pymupdf.csGRAY, pix).samples

    # Paper brightness varies across a phone photo, so estimate it per tile
    # (a high percentile, so ink doesn't count) and interpolate between tiles.
    T = 48
    tw, th = (w + T - 1) // T, (h + T - 1) // T
    bg = [[0.0] * tw for _ in range(th)]
    for ty in range(th):
        for tx in range(tw):
            vals = [gray[y * w + x] for y in range(ty * T, min(h, ty * T + T), 3)
                    for x in range(tx * T, min(w, tx * T + T), 3)]
            vals.sort()
            bg[ty][tx] = max(60, vals[int(len(vals) * 0.9)] if vals else 255)

    def paper(x: int, y: int) -> float:
        fx = min(max((x - T / 2) / T, 0), tw - 1)
        fy = min(max((y - T / 2) / T, 0), th - 1)
        x0, y0 = int(fx), int(fy)
        x1, y1 = min(x0 + 1, tw - 1), min(y0 + 1, th - 1)
        ax, ay = fx - x0, fy - y0
        top = bg[y0][x0] * (1 - ax) + bg[y0][x1] * ax
        bot = bg[y1][x0] * (1 - ax) + bg[y1][x1] * ax
        return top * (1 - ay) + bot * ay

    alpha = bytearray(w * h)
    cols = [0] * w   # strong-ink counts, for cropping
    rows = [0] * h
    for y in range(h):
        row = y * w
        p = 255.0
        for x in range(w):
            if x % 4 == 0:  # paper level changes slowly; refresh every 4 px
                p = paper(x, y)
            v = gray[row + x]
            s = (p - v) / p  # how much darker than the paper, 0..1
            if s > 0.18:
                a = min(255, int((s - 0.18) / 0.22 * 255))
                alpha[row + x] = a
                if a > 140:
                    cols[x] += 1
                    rows[y] += 1

    # crop to the ink, ignoring rows/columns with only a speck or two
    xs = [x for x in range(w) if cols[x] >= 2]
    ys = [y for y in range(h) if rows[y] >= 2]
    if not xs or not ys:
        raise SignError("No signature found in the photo. Sign in dark ink on plain white "
                        "paper and take the photo in good light.")
    pad = max(4, (max(xs) - min(xs)) // 40)
    cx0, cx1 = max(0, min(xs) - pad), min(w, max(xs) + pad + 1)
    cy0, cy1 = max(0, min(ys) - pad), min(h, max(ys) + pad + 1)
    ink_share = sum(rows) / (w * h)
    if ink_share > 0.25:
        raise SignError("That photo looks mostly dark, not like a signature on white paper.")

    cw, ch = cx1 - cx0, cy1 - cy0
    outbuf = bytearray(cw * ch * 4)
    i = 0
    for y in range(cy0, cy1):
        for x in range(cx0, cx1):
            a = alpha[y * w + x]
            if a:
                p = paper(x, y)
                af = max(a / 255, 0.05)
                o = y * st + x * 3
                for c in range(3):
                    # un-mix the paper to recover the pen color
                    v = (rgb[o + c] - p * (1 - af)) / af
                    outbuf[i + c] = int(min(255, max(0, v)))
                outbuf[i + 3] = a
            i += 4
    res = pymupdf.Pixmap(pymupdf.csRGB, cw, ch, bytes(outbuf), True)
    res.save(str(out))
    return out


# ---------------------------------------------------------------- where to sign


@dataclass
class Spot:
    page: int                  # 0-based
    rect: pymupdf.Rect         # visible coordinates
    label: str
    source: str                # "field", "text-field", "line" or "label"
    xref: int = 0              # widget xref for fields


def _rot(page):
    return page.rotation_matrix


def find_spots(doc: pymupdf.Document) -> list[Spot]:
    spots: list[Spot] = []
    for page in doc:
        for w in page.widgets():
            text = f"{w.field_name} {w.field_label or ''}"
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                spots.append(Spot(page.number, w.rect * _rot(page),
                                  w.field_label or w.field_name, "field", w.xref))
            elif w.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT and SIG_WORDS.search(text):
                spots.append(Spot(page.number, w.rect * _rot(page),
                                  w.field_label or w.field_name, "text-field", w.xref))
    if spots:
        return spots

    import quill_blanks as pb  # flat form: look at the page itself

    for page in doc:
        words = pb.page_words(page)
        sig_lines = [line for line in _text_lines(words).values()
                     if len(line) <= 6 and SIG_WORDS.search(" ".join(w[1] for w in line))]
        if not sig_lines:
            continue
        found = [Spot(page.number, b.rect, b.label, "line")
                 for b in pb.find_blanks(page) if b.kind != "checkbox" and SIG_WORDS.search(b.label)]
        r = pb.Raster(page)
        for line in sig_lines:
            text = " ".join(w[1] for w in line)
            box = pymupdf.Rect(line[0][0]).include_rect(line[-1][0])
            right = _space_right(r, box)
            if right is None:
                continue
            # A label can yield both a line spot (the line it captions) and
            # the room beside it. The line wins when it has comfortable room;
            # otherwise take whichever has more height to sign in.
            twins = [s for s in found if s.source == "line" and abs(
                (s.rect.y0 + s.rect.y1) / 2 - (box.y0 + box.y1) / 2) < 40
                and (text.lower() in s.label.lower() or s.label.lower() in text.lower())]
            best = max((t.rect.height for t in twins), default=0)
            if twins and (best >= 14 or best >= right.height):
                continue
            found = [s for s in found if s not in twins]
            found.append(Spot(page.number, right, text, "label"))
        spots.extend(sorted(found, key=lambda s: (round(s.rect.y0 / 6), s.rect.x0)))
    return spots


def _text_lines(words):
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault((w[2], w[3]), []).append(w)
    for v in lines.values():
        v.sort(key=lambda w: w[0].x0)
    return lines


def _space_right(r, box: pymupdf.Rect) -> pymupdf.Rect | None:
    """Empty room to the right of a "Signature" label on the same line."""
    import quill_blanks as pb

    # how far right the label's own line stays empty...
    y0, y1 = max(0, pb.px(box.y0)), min(r.h, pb.px(box.y1))
    x = pb.px(box.x1 + 6)
    start = x
    while x < r.w and sum(1 for y in range(y0, y1) if r.rows[y][x] == pb.ONE) <= 1:
        x += 1
    stop = x - pb.px(4)
    if (stop - start) / pb.ZOOM < 54:  # under three quarters of an inch: no room
        return None
    # ...then grow up and down while that stretch stays empty, so the
    # signature gets as much height as the form leaves free
    for _ in range(pb.px(12)):
        if y0 <= 0 or r.dark(y0 - 1, start, stop) > 1:
            break
        y0 -= 1
    for _ in range(pb.px(10)):
        if y1 >= r.h or r.dark(y1, start, stop) > 1:
            break
        y1 += 1
    return pymupdf.Rect(start, y0 + 2, stop, y1 - 2) / pb.ZOOM


def spot_matching(spots: list[Spot], doc, at: str) -> Spot:
    """--at: a field name/label (substring), or X,Y[,PAGE] in points."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*(?:,\s*(\d+))?\s*$", at)
    if m:
        x, y, p = float(m[1]), float(m[2]), int(m[3] or 1)
        if not 1 <= p <= doc.page_count:
            raise SignError(f"Page {p} doesn't exist (the PDF has {doc.page_count}).")
        # x,y is where the signature's bottom-left sits (on the line)
        return Spot(p - 1, pymupdf.Rect(x, y - 30, x + 200, y), f"{at}", "position")
    hits = [s for s in spots if at.lower() in s.label.lower()]
    if not hits:
        names = ", ".join(f'"{s.label}"' for s in spots) or "none found"
        raise SignError(f"No signature spot matches '{at}'. Found: {names}")
    return hits[0]


# ---------------------------------------------------------------- placing


def _room_above(page: pymupdf.Page, rect: pymupdf.Rect, own_xref: int) -> float:
    """How high a signature in this field may rise: up through empty page,
    to at most MAX_SIG_HEIGHT tall. Returns the new top (visible coords)."""
    import quill_blanks as pb

    # look at the page without the field itself (its look may include a tag)
    hidden = []
    for w in page.widgets():
        if w.xref == own_xref:
            hidden.append((w.xref, page.parent.xref_get_key(w.xref, "F")))
            page.parent.xref_set_key(w.xref, "F", "2")
    try:
        r = pb.Raster(page)
    finally:
        for xref, (kind, val) in hidden:
            page.parent.xref_set_key(xref, "F", val if kind != "null" else "null")
    x0, x1 = pb.px(rect.x0) + 2, pb.px(rect.x1) - 2
    y = pb.px(rect.y0) - 1
    limit = pb.px(rect.y1 - MAX_SIG_HEIGHT)
    while y > max(0, limit) and r.dark(y, x0, x1) <= 1:
        y -= 1
    return min(rect.y0, (y + 3) / pb.ZOOM)


def place(doc: pymupdf.Document, spot: Spot, sig: dict) -> pymupdf.Rect:
    """Put the signature in the spot. Returns where it went (visible coords)."""
    page = doc[spot.page]
    target = pymupdf.Rect(spot.rect)
    if spot.source in ("field", "text-field") and target.height < MAX_SIG_HEIGHT:
        # Form fields are often drawn short. Let the signature rise into the
        # empty space above (detected spots already end where text begins).
        target.y0 = _room_above(page, target, spot.xref)
    if target.height < MIN_SIG_HEIGHT and spot.source in ("field", "text-field", "position"):
        target.y0 = target.y1 - MIN_SIG_HEIGHT
    if spot.source == "field" and spot.xref:
        # hide the empty signature field: its "sign here" look would cover ours
        doc.xref_set_key(spot.xref, "F", "2")
    if spot.source == "text-field" and spot.xref:
        # a text box meant for the signature would sit on top of it; remove it
        w = page.load_widget(spot.xref)
        if w is not None:
            page.delete_widget(w)
    inner = pymupdf.Rect(target.x0 + 3, target.y0 + 1, target.x1 - 3, target.y1 - 1)

    if sig["kind"] == "vector":
        art = pymupdf.open(sig.get("path") or str(home() / sig["file"]))
        aspect = art[0].rect.width / art[0].rect.height
        h = inner.height
        w = h * aspect
        if w > inner.width:
            w, h = inner.width, inner.width / aspect
        box = pymupdf.Rect(inner.x0, inner.y1 - h, inner.x0 + w, inner.y1)
        page.show_pdf_page(box * page.derotation_matrix, art, 0,
                           rotate=page.rotation, overlay=True)
        return box

    if sig["kind"] == "image":
        png = str(home() / sig["file"]) if "path" not in sig else sig["path"]
        img = pymupdf.Pixmap(png)
        aspect = img.width / img.height
        h = inner.height
        w = h * aspect
        if w > inner.width:
            w, h = inner.width, inner.width / aspect
        box = pymupdf.Rect(inner.x0, inner.y1 - h, inner.x0 + w, inner.y1)
        page.insert_image(box * page.derotation_matrix, filename=png,
                          rotate=page.rotation, keep_proportion=False, overlay=True)
        return box

    font_path = font_file(sig.get("font", "script"))
    name = sig["name"]
    font = pymupdf.Font(fontfile=font_path) if font_path else pymupdf.Font("tiit")
    size = min(inner.height * 0.75, 28)
    while size > 6 and font.text_length(name, fontsize=size) > inner.width:
        size -= 0.5
    base = inner.y1 - size * 0.28  # leave room for the font's descenders
    pt = pymupdf.Point(inner.x0, base) * page.derotation_matrix
    kw = {"fontfile": font_path, "fontname": "quillsig"} if font_path else {"fontname": "tiit"}
    page.insert_text(pt, name, fontsize=size, color=INK, rotate=page.rotation, **kw)
    return pymupdf.Rect(inner.x0, base - size, inner.x0 + font.text_length(name, fontsize=size),
                        base + size * 0.3)


def today_text(fmt: str = "%m/%d/%Y") -> str:
    return datetime.date.today().strftime(fmt)


def fill_date(doc: pymupdf.Document, spot: Spot, fmt: str = "%m/%d/%Y") -> str | None:
    """Fill the date box nearest the signature. Returns its label, or None."""
    page = doc[spot.page]
    rot = page.rotation_matrix
    near = spot.rect

    import quill_blanks as pb

    # Boxes under "For office use" / "Do not write below this line" aren't ours.
    office_y = min((w[0].y0 for w in pb.page_words(page)
                    if OFFICE.search(" ".join(x[1] for x in pb.page_words(page)
                                              if x[2] == w[2] and x[3] == w[3]))
                    and w[0].y0 > near.y1), default=float("inf"))

    def score(r: pymupdf.Rect, label: str) -> float:
        same_row = r.y1 > near.y0 - 10 and r.y0 < near.y1 + 10
        dx = max(0, r.x0 - near.x1, near.x0 - r.x1)
        dy = max(0, r.y0 - near.y1, near.y0 - r.y1)
        bonus = -400 if TODAY.search(label) else 0  # "Today's date" is the one
        return (0 if same_row else 1000) + dx + dy * 2 + bonus

    def usable(label: str, r: pymupdf.Rect) -> bool:
        return (DATE_WORDS.search(label) and not NOT_TODAY.search(label)
                and not OFFICE.search(label) and r.y0 < office_y)

    cands = []
    for w in page.widgets():
        text = f"{w.field_name} {w.field_label or ''}"
        r = w.rect * rot
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT and usable(text, r) and not w.field_value:
            cands.append((score(r, text), "field", w.xref, w.field_label or w.field_name, None))
    if not cands:
        for b in pb.find_blanks(page):
            if b.kind == "text" and usable(b.label, b.rect):
                cands.append((score(b.rect, b.label), "blank", 0, b.label, b.rect))
    cands = [c for c in cands if c[0] < 1300]
    if not cands:
        return None
    _, kind, xref, label, rect = min(cands, key=lambda c: c[0])
    text = today_text(fmt)
    if kind == "field":
        w = page.load_widget(xref)
        w.field_value = text
        w.update()
    else:
        size = max(7, min(11, rect.height * 0.7))
        pt = pymupdf.Point(rect.x0 + 2, rect.y1 - 3) * page.derotation_matrix
        page.insert_text(pt, text, fontname="helv", fontsize=size, color=(0, 0, 0),
                         rotate=page.rotation)
    return label

