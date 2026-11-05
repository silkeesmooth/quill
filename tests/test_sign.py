import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quill  # noqa: E402
import quill_sign as ps  # noqa: E402

PEN = (0.1, 0.2, 0.7)


@pytest.fixture(autouse=True)
def sig_home(private_home, monkeypatch):
    monkeypatch.setattr(quill.os, "startfile", lambda p: None, raising=False)
    return private_home


def fake_photo(path: Path) -> Path:
    """A phone-style photo: unevenly lit gray paper with a blue scribble."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    for i in range(40):  # lighting falls off from left to right
        g = 0.93 - i * 0.006
        page.draw_rect(pymupdf.Rect(i * 10, 0, i * 10 + 10, 300), color=None, fill=(g, g, g * 0.97))
    pts = [(120, 170), (150, 110), (170, 190), (200, 120), (230, 185), (260, 130), (290, 175)]
    for a, b in zip(pts, pts[1:]):
        page.draw_bezier(a, (a[0] + 10, a[1] - 30), (b[0] - 10, b[1] + 30), b, color=PEN, width=3)
    page.draw_dot = None
    pix = page.get_pixmap(dpi=144)
    path.write_bytes(pix.tobytes("jpg"))
    return path


def form_with_line(path: Path, label="Signature", with_date=True) -> Path:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Agreement", fontsize=16)
    p.draw_line((72, 300), (300, 300), color=(0, 0, 0), width=0.7)
    p.insert_text((72, 312), label, fontsize=9)
    if with_date:
        p.draw_line((340, 300), (480, 300), color=(0, 0, 0), width=0.7)
        p.insert_text((340, 312), "Date", fontsize=9)
    p.insert_text((72, 400), "Date of birth", fontsize=9)
    p.draw_line((140, 400), (260, 400), color=(0, 0, 0), width=0.7)
    doc.save(path)
    return path


def run(*args):
    return quill.main([str(a) for a in args])


def ink_box(page: pymupdf.Page, rect: pymupdf.Rect):
    """Bounding box of non-white pixels inside rect (page coordinates)."""
    pix = page.get_pixmap(dpi=72, clip=rect, colorspace=pymupdf.csGRAY)
    xs, ys = [], []
    for y in range(pix.height):
        for x in range(pix.width):
            if pix.samples[y * pix.stride + x] < 200:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return pymupdf.Rect(rect.x0 + min(xs), rect.y0 + min(ys), rect.x0 + max(xs), rect.y0 + max(ys))


def test_photo_cleanup(tmp_path):
    photo = fake_photo(tmp_path / "sig.jpg")
    out = ps.clean_photo(photo, tmp_path / "clean.png")
    pix = pymupdf.Pixmap(str(out))
    assert pix.alpha
    # cropped close to the scribble (photo is 800x600 px)
    assert 300 < pix.width < 460 and 150 < pix.height < 260
    n = pix.n
    corners = [pix.samples[i] for i in (n - 1, (pix.width - 1) * n + n - 1)]
    assert all(a == 0 for a in corners), "paper must become transparent"
    strong = [pix.samples[i:i + n] for i in range(0, len(pix.samples), n)
              if pix.samples[i + n - 1] > 200]
    assert len(strong) > 500
    r, g, b = sum(s[0] for s in strong) / len(strong), sum(s[1] for s in strong) / len(strong), \
        sum(s[2] for s in strong) / len(strong)
    assert b > r + 40 and b > g + 30, "the pen's blue should survive"


def test_blank_photo_rejected(tmp_path):
    doc = pymupdf.open()
    doc.new_page(width=200, height=100)
    p = tmp_path / "blank.png"
    doc[0].get_pixmap().save(str(p))
    with pytest.raises(ps.SignError, match="No signature found"):
        ps.clean_photo(p, tmp_path / "x.png")


def test_save_show_remove(tmp_path, capsys, sig_home):
    assert run("signature") == 0
    assert "No saved signature" in capsys.readouterr().out
    assert run("signature", fake_photo(tmp_path / "s.jpg"), "--no-open") == 0
    assert (sig_home / "signature.png").is_file()
    assert run("signature") == 0
    assert "signature image" in capsys.readouterr().out
    assert run("signature", "--name", "Ada Lovelace") == 0
    assert not (sig_home / "signature.png").exists()
    assert ps.load_saved()["name"] == "Ada Lovelace"
    assert run("signature", "--remove") == 0
    assert ps.load_saved() is None


def test_sign_flat_form_with_photo(tmp_path):
    form = form_with_line(tmp_path / "f.pdf")
    run("signature", fake_photo(tmp_path / "s.jpg"), "--no-open")
    assert run("sign", form, "--date") == 0
    out = tmp_path / "f-signed.pdf"
    page = pymupdf.open(out)[0]
    imgs = page.get_images()
    assert len(imgs) == 1
    where = page.get_image_rects(imgs[0][0])[0]
    # sits on the signature line: bottom at the line, left end at its start
    assert 290 <= where.y1 <= 300.5 and 72 <= where.x0 <= 80 and where.height >= 15
    # dated in the Date box beside it, not in "Date of birth"
    text = page.get_text()
    today = ps.today_text()
    assert today in text
    hit = page.search_for(today)[0]
    assert hit.x0 > 335 and hit.y1 < 302


def test_sign_typed_name(tmp_path):
    form = form_with_line(tmp_path / "f.pdf", with_date=False)
    assert run("sign", form, "--name", "Ada Lovelace") == 0
    page = pymupdf.open(tmp_path / "f-signed.pdf")[0]
    hit = page.search_for("Ada Lovelace")
    assert hit and 270 < hit[0].y1 < 303 and hit[0].x0 >= 72
    fonts = {f[3] for f in page.get_fonts()}
    if ps.font_file("script"):
        assert any("Segoe" in f or "Script" in f for f in fonts), fonts


def test_sign_signature_field(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_name = "Employee Signature"
    w.rect = pymupdf.Rect(100, 500, 350, 520)
    p.add_widget(w)
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_name = "Date signed"
    w.rect = pymupdf.Rect(400, 500, 500, 520)
    p.add_widget(w)
    src = tmp_path / "fill.pdf"
    doc.save(src)
    assert run("sign", src, "--name", "Ada", "--date") == 0
    page = pymupdf.open(tmp_path / "fill-signed.pdf")[0]
    names = {w.field_name: w.field_value for w in page.widgets()}
    assert "Employee Signature" not in names, "the signature text box is replaced"
    assert names["Date signed"] == ps.today_text()
    ink = ink_box(page, pymupdf.Rect(80, 460, 380, 540))
    assert ink is not None and 100 <= ink.x0 < 120
    assert 490 <= ink.y0 and ink.y1 <= 523  # inside the box, rising a little above it


def test_several_spots_and_at(tmp_path, capsys):
    doc = pymupdf.open()
    p = doc.new_page()
    for y, who in ((300, "Employee signature"), (400, "Supervisor signature")):
        p.draw_line((72, y), (300, y), color=(0, 0, 0), width=0.7)
        p.insert_text((72, y + 12), who, fontsize=9)
    src = tmp_path / "two.pdf"
    doc.save(src)
    assert run("sign", src, "--name", "Ada") == 0
    assert "Also found" in capsys.readouterr().out
    page = pymupdf.open(tmp_path / "two-signed.pdf")[0]
    assert page.search_for("Ada")[0].y1 < 305
    assert run("sign", src, "--name", "Ada", "--at", "supervisor", "-o", tmp_path / "b.pdf") == 0
    assert 395 < pymupdf.open(tmp_path / "b.pdf")[0].search_for("Ada")[0].y1 < 405
    assert run("sign", src, "--name", "Ada", "--at", "nobody", "-o", tmp_path / "c.pdf") == 2
    assert run("sign", src, "--name", "Ada", "--at", "100,600", "-o", tmp_path / "d.pdf") == 0
    ink = ink_box(pymupdf.open(tmp_path / "d.pdf")[0], pymupdf.Rect(80, 540, 360, 640))
    assert ink is not None and 99 <= ink.x0 <= 108 and 585 < ink.y1 <= 603


def test_label_beside_space(tmp_path):
    """SS-5 style: 'YOUR SIGNATURE' label with empty room to its right."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_rect((60, 600, 400, 640), color=(0, 0, 0), width=0.8)
    p.insert_text((66, 622), "YOUR SIGNATURE", fontname="hebo", fontsize=11)
    src = tmp_path / "box.pdf"
    doc.save(src)
    assert run("sign", src, "--name", "Ada Lovelace") == 0
    page = pymupdf.open(tmp_path / "box-signed.pdf")[0]
    label = page.search_for("YOUR SIGNATURE")[0]
    ink = ink_box(page, pymupdf.Rect(62, 602, 398, 638))  # inside the box's border
    assert ink is not None
    # signed inside the box, beside or under the label, never over it
    sig = page.search_for("Ada Lovelace")[0]
    glyphs = pymupdf.Rect(sig.x0, sig.y0 + sig.height * 0.3, sig.x1, sig.y1 - sig.height * 0.25)
    assert not glyphs.intersects(label), (sig, label)


def test_no_spot_and_no_signature(tmp_path, capsys):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A plain letter with nothing to fill in.")
    src = tmp_path / "plain.pdf"
    doc.save(src)
    assert run("sign", src) == 2
    assert "No saved signature" in capsys.readouterr().err
    assert run("sign", src, "--name", "Ada") == 2
    assert "Couldn't find where to sign" in capsys.readouterr().err


def test_send_to_prompts_for_signature(tmp_path, monkeypatch, sig_home):
    form = form_with_line(tmp_path / "f.pdf")
    answers = iter(["Grace Hopper", "y", ""])  # name, date yes, close window
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main(["sign", str(form), "--pause"]) == 0
    assert ps.load_saved()["name"] == "Grace Hopper"
    page = pymupdf.open(tmp_path / "f-signed.pdf")[0]
    assert page.search_for("Grace Hopper") and page.search_for(ps.today_text())


def test_guided_mode_offers_signing(tmp_path, monkeypatch):
    ps.save_name("Ada Lovelace")
    doc = pymupdf.open()
    p = doc.new_page()
    for name, y in (("Full name", 100), ("Signature", 200)):
        w = pymupdf.Widget()
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        w.rect = pymupdf.Rect(72, y, 300, y + 20)
        p.add_widget(w)
    src = tmp_path / "g.pdf"
    doc.save(src)
    # only "Full name" is asked; then: sign? yes; date? no; open? no
    answers = iter(["Ada L", "", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main([str(src)]) == 0
    page = pymupdf.open(tmp_path / "g-filled.pdf")[0]
    assert page.search_for("Ada Lovelace")
    assert {w.field_name for w in page.widgets()} == {"Full name"}


# ---------------------------------------------------------------- vector artwork, rotation


def fake_logo(path: Path) -> Path:
    """Like a logo file: dark background, white script shapes, small tagline."""
    doc = pymupdf.open()
    p = doc.new_page(width=500, height=300)
    p.draw_rect(p.rect, color=None, fill=(0.13, 0.12, 0.12))
    # a tall, heavy loop at the left and a long thin swash to the right
    p.draw_oval(pymupdf.Rect(60, 60, 140, 200), color=None, fill=(1, 1, 1))
    p.draw_polyline([(140, 150), (450, 120), (450, 126), (140, 160)], color=None,
                    fill=(1, 1, 1), closePath=True)
    for i in range(8):  # "PHOTOGRAPHY"-style small letters underneath
        p.draw_rect(pymupdf.Rect(150 + i * 25, 230, 165 + i * 25, 245), color=None, fill=(1, 1, 1))
    doc.save(path)
    return path


def lopsided_photo(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.draw_circle((80, 100), 35, color=None, fill=PEN)
    page.draw_line((115, 100), (360, 100), color=PEN, width=3)
    path.write_bytes(page.get_pixmap(dpi=144).tobytes("jpg"))
    return path


def test_vector_signature_from_logo(tmp_path, sig_home):
    assert run("signature", fake_logo(tmp_path / "logo.ai.pdf"), "--no-open") == 0
    sig = ps.load_saved()
    assert sig["kind"] == "vector"
    art = pymupdf.open(sig_home / "signature.pdf")[0]
    # cropped to the script (390 x 140 plus a little padding), tagline left out
    assert 390 <= art.rect.width < 430 and 140 <= art.rect.height < 160
    fills = {tuple(round(c, 2) for c in d["fill"]) for d in art.get_drawings() if d.get("fill")}
    assert fills == {tuple(round(c, 2) for c in ps.INK)}, "white shapes become pen ink"
    assert len(art.get_drawings()) == 2

    form = form_with_line(tmp_path / "f.pdf", with_date=False)
    assert run("sign", form) == 0
    page = pymupdf.open(tmp_path / "f-signed.pdf")[0]
    ink = [d for d in page.get_drawings() if d.get("fill") and
           tuple(round(c, 2) for c in d["fill"]) == tuple(round(c, 2) for c in ps.INK)]
    assert ink, "signature drawn as vector shapes"
    box = ink[0]["rect"]
    for d in ink[1:]:
        box |= d["rect"]
    assert 72 <= box.x0 < 80 and 290 <= box.y1 <= 300.5 and box.width > box.height * 2


def rotated_form(tmp_path: Path, rot: int = 90) -> Path:
    """A signature form stored sideways with /Rotate, shown upright."""
    import quill_blanks as pb
    flat = form_with_line(tmp_path / "upright.pdf", with_date=False)
    s = pymupdf.open(flat)
    for turn in (90, -90):
        doc = pymupdf.open()
        page = doc.new_page(width=s[0].rect.height, height=s[0].rect.width)
        page.show_pdf_page(page.rect, s, 0, rotate=turn)
        page.set_rotation(rot)
        w = next(w for w in pb.page_words(page) if w[1] == "Signature")
        if w[0].width > w[0].height and abs(w[0].x0 - 72) < 2:
            out = tmp_path / "rotated.pdf"
            doc.save(out)
            return out
    raise AssertionError("could not build a rotated form")


def visible_ink(page: pymupdf.Page, rect: pymupdf.Rect):
    """Ink pixels (x, y) in visible page coordinates inside rect."""
    pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)  # renders as seen
    pts = []
    for y in range(int(rect.y0), int(rect.y1)):
        for x in range(int(rect.x0), int(rect.x1)):
            if pix.samples[y * pix.stride + x] < 180:
                pts.append((x, y))
    return pts


@pytest.mark.parametrize("rot", [90, 270])
@pytest.mark.parametrize("kind", ["image", "vector"])
def test_rotated_page_signature_is_upright(tmp_path, kind, rot):
    form = rotated_form(tmp_path, rot)
    if kind == "image":
        art = lopsided_photo(tmp_path / "sig.jpg")
    else:
        doc = pymupdf.open()
        p = doc.new_page(width=400, height=200)
        p.draw_circle((80, 100), 35, color=None, fill=(0, 0, 0))
        # a thin rising swash (slanted, so it isn't mistaken for a small tagline)
        p.draw_polyline([(110, 110), (360, 60), (360, 66), (110, 116)], color=None,
                        fill=(0, 0, 0), closePath=True)
        art = tmp_path / "sig.pdf"
        doc.save(art)
    assert run("sign", form, "--image", art, "-o", tmp_path / "out.pdf") == 0
    page = pymupdf.open(tmp_path / "out.pdf")[0]
    assert page.rotation == rot
    pts = visible_ink(page, pymupdf.Rect(70, 260, 310, 298))  # above the line, as seen
    assert len(pts) > 50
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    assert max(xs) - min(xs) > 2 * (max(ys) - min(ys)), "lying flat, not on its side"
    mid = (min(xs) + max(xs)) / 2
    left = sum(1 for x in xs if x < mid)
    assert left > 0.6 * len(xs), "heavy end on the left: not upside down or mirrored"


@pytest.mark.parametrize("rot", [90, 270])
def test_rotated_page_typed_name_is_upright(tmp_path, rot):
    form = rotated_form(tmp_path, rot)
    assert run("sign", form, "--name", "Ada Lovelace", "-o", tmp_path / "out.pdf") == 0
    page = pymupdf.open(tmp_path / "out.pdf")[0]
    rot = page.rotation_matrix
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if "Ada" in "".join(sp["text"] for sp in line["spans"]):
                d = pymupdf.Point(line["dir"]) * pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
                assert abs(d.x - 1) < 0.01 and abs(d.y) < 0.01, d
                vis = pymupdf.Rect(line["bbox"]) * rot
                assert 70 <= vis.x0 < 80 and vis.y1 < 305
                return
    raise AssertionError("signature text not found")


def test_moves_data_out_of_appdata(tmp_path, monkeypatch):
    """Store Python hides %APPDATA% writes; data moves to ~/.quill once."""
    monkeypatch.delenv("QUILL_HOME")
    monkeypatch.setattr(ps.os, "name", "nt")
    monkeypatch.setattr(ps.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    old = tmp_path / "appdata" / "pdffill"
    old.mkdir(parents=True)
    (old / "signature.json").write_text('{"kind": "name", "name": "Ada"}', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert ps.home() == tmp_path / "user" / ".quill"
    assert ps.load_saved()["name"] == "Ada"
    assert not old.exists()


def test_moves_data_from_old_name(tmp_path, monkeypatch):
    """Saved under the old name (~/.pdffill): moves to ~/.quill once."""
    monkeypatch.delenv("QUILL_HOME")
    monkeypatch.setattr(ps.os, "name", "nt")
    monkeypatch.setattr(ps.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    old = tmp_path / "user" / ".pdffill"
    old.mkdir(parents=True)
    (old / "signature.json").write_text('{"kind": "name", "name": "Ada"}', encoding="utf-8")
    (old / "profile.json").write_text('{"first_name": "Ada"}', encoding="utf-8")
    assert ps.home() == tmp_path / "user" / ".quill"
    assert sorted(p.name for p in ps.home().iterdir()) == ["profile.json", "signature.json"]
    assert not old.exists()
