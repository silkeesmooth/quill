import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quill  # noqa: E402
import quill_blanks as pb  # noqa: E402

GRAY = (0.7, 0.7, 0.7)   # forms often print lines in light gray


def draw_form(page: pymupdf.Page) -> None:
    page.insert_text((72, 60), "BOX OF DOOM ORDER FORM", fontname="hebo", fontsize=18)
    page.insert_text((72, 90), "Please fill in every part of this form in ink.", fontsize=10)
    # open write-on line with a label to its left
    page.insert_text((72, 130), "Name:", fontsize=10)
    page.draw_line((110, 132), (360, 132), color=GRAY, width=0.6)
    # a second "Name" line, to check names stay unique
    page.insert_text((72, 160), "Name:", fontsize=10)
    page.draw_line((110, 162), (360, 162), color=(0, 0, 0), width=0.6)
    # closed box with its label at the top
    page.draw_rect((72, 180, 300, 214), color=(0, 0, 0), width=0.8)
    page.insert_text((75, 188), "City", fontsize=6)
    # a table row of three cells with headers above
    for i, head in enumerate(["First", "Middle", "Last"]):
        page.insert_text((72 + i * 100, 236), head, fontsize=7)
    page.draw_rect((72, 240, 372, 262), color=(0, 0, 0), width=0.8)
    for x in (172, 272):
        page.draw_line((x, 240), (x, 262), color=(0, 0, 0), width=0.8)
    # checkboxes with labels to the right
    for i, text in enumerate(["Yes", "No"]):
        x = 72 + i * 80
        page.draw_rect((x, 290, x + 10, 300), color=(0, 0, 0), width=0.7)
        page.insert_text((x + 14, 299), text, fontsize=10)
    # a big comments box
    page.insert_text((72, 330), "Comments", fontsize=10)
    page.draw_rect((72, 336, 500, 440), color=(0, 0, 0), width=0.8)


@pytest.fixture
def flat(tmp_path):
    doc = pymupdf.open()
    draw_form(doc.new_page())
    p = tmp_path / "flat.pdf"
    doc.save(p)
    return p


def scanned(src: Path, dest: Path, angle: float = 0) -> Path:
    s = pymupdf.open(src)
    out = pymupdf.open()
    for page in s:
        if angle:
            tmp = pymupdf.open()
            t = tmp.new_page(width=page.rect.width, height=page.rect.height)
            t.show_pdf_page(t.rect, s, page.number, rotate=angle)
            pix = t.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        else:
            pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        np_ = out.new_page(width=page.rect.width, height=page.rect.height)
        np_.insert_image(np_.rect, pixmap=pix)
    out.save(dest)
    return dest


def widgets(path):
    doc = pymupdf.open(path)
    return [(w.field_type_string, w.field_name, w.field_label, w.rect * p.rotation_matrix,
             w.field_flags) for p in doc for w in p.widgets()]


def run(*args):
    return quill.main([str(a) for a in args])


def test_finds_every_blank(flat, capsys):
    assert run("make-fillable", flat) == 0
    ws = widgets(flat.parent / "flat-fillable.pdf")
    kinds = [w[0] for w in ws]
    assert kinds.count("CheckBox") == 2
    assert kinds.count("Text") == 7  # 2 lines, City box, 3 cells, comments
    names = {w[1] for w in ws}
    assert {"Name", "Name 2", "City", "Yes", "No"} <= names
    assert {"First", "Middle", "Last"} <= names
    comments = [w for w in ws if w[4] & pymupdf.PDF_TX_FIELD_IS_MULTILINE]
    assert len(comments) == 1 and comments[0][3].height > 60
    # nothing on the bold heading or the paragraph
    assert all(w[3].y0 > 100 for w in ws)


def test_fields_sit_on_their_lines(flat):
    assert run("make-fillable", flat) == 0
    ws = {w[1]: w[3] for w in widgets(flat.parent / "flat-fillable.pdf")}
    name = ws["Name"]
    assert 108 <= name.x0 <= 115 and 355 <= name.x1 <= 362
    assert 128 <= name.y1 <= 133 and name.height >= 8
    yes = ws["Yes"]
    assert abs(yes.x0 - 72) < 2 and abs(yes.y0 - 290) < 2


def test_scanned_copy_matches(flat, tmp_path):
    scan = scanned(flat, tmp_path / "scan.pdf")
    assert run("make-fillable", scan) == 0
    ws = widgets(tmp_path / "scan-fillable.pdf")
    assert [w[0] for w in ws].count("CheckBox") == 2
    assert [w[0] for w in ws].count("Text") == 7
    assert any(w[4] & pymupdf.PDF_TX_FIELD_IS_MULTILINE for w in ws)


def test_slightly_tilted_scan(flat, tmp_path):
    scan = scanned(flat, tmp_path / "tilt.pdf", angle=0.7)
    assert run("make-fillable", scan) == 0
    ws = widgets(tmp_path / "tilt-fillable.pdf")
    assert [w[0] for w in ws].count("CheckBox") == 2
    assert [w[0] for w in ws].count("Text") >= 6


def upright_rotated_copy(src: Path, dest: Path) -> Path:
    """The form stored sideways with /Rotate set so viewers show it upright,
    the way many scanners save pages."""
    s = pymupdf.open(src)
    for content_turn, page_rot in ((90, 90), (-90, 90), (90, 270), (-90, 270)):
        doc = pymupdf.open()
        page = doc.new_page(width=s[0].rect.height, height=s[0].rect.width)
        page.show_pdf_page(page.rect, s, 0, rotate=content_turn)
        page.set_rotation(page_rot)
        words = pb.page_words(page)
        hello = next(w for w in words if w[1] == "Name:")
        if hello[0].width > hello[0].height and abs(hello[0].x0 - 72) < 2:
            doc.save(dest)
            return dest
    raise AssertionError("could not build an upright rotated page")


def test_rotated_page(flat, tmp_path):
    src = upright_rotated_copy(flat, tmp_path / "rot.pdf")
    assert pymupdf.open(src)[0].rotation in (90, 270)
    assert run("make-fillable", src) == 0
    page = pymupdf.open(tmp_path / "rot-fillable.pdf")[0]
    ws = {w.field_name: w.rect * page.rotation_matrix for w in page.widgets()}
    assert "Name" in ws and "Yes" in ws
    # visible position matches the upright form: just above the Name line
    name = ws["Name"]
    assert 108 <= name.x0 <= 115 and 355 <= name.x1 <= 362 and 128 <= name.y1 <= 133


def test_result_can_be_filled(flat, tmp_path, monkeypatch):
    assert run("make-fillable", flat) == 0
    made = flat.parent / "flat-fillable.pdf"
    assert run("fill", made, "--set", "Name=Ada", "--set", "Yes=yes", "--set",
               "Comments=Line one", "-o", tmp_path / "done.pdf") == 0
    text = pymupdf.open(tmp_path / "done.pdf")[0].get_text()
    assert "Ada" in text and "Line one" in text


def test_refuses_fillable_pdf_without_anyway(flat, tmp_path, capsys):
    assert run("make-fillable", flat) == 0
    made = flat.parent / "flat-fillable.pdf"
    assert run("make-fillable", made, "-o", tmp_path / "x.pdf") == 2
    assert "already has" in capsys.readouterr().err
    assert run("make-fillable", made, "--anyway", "-o", tmp_path / "x.pdf") in (0, 2)


def test_blank_page_says_nothing_found(tmp_path, capsys):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Just a letter, no blanks.")
    p = tmp_path / "letter.pdf"
    doc.save(p)
    assert run("make-fillable", p) == 2
    assert "No blanks found" in capsys.readouterr().err
    assert not (tmp_path / "letter-fillable.pdf").exists()


def test_guided_mode_points_to_make_fillable(tmp_path, monkeypatch, capsys):
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "flat.pdf"
    doc.save(p)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert quill.main([str(p)]) == 2
    assert "make-fillable" in capsys.readouterr().err


def test_labels_with_brackets_make_valid_names(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_line((72, 200), (300, 200), color=(0, 0, 0), width=0.7)
    p.insert_text((72, 212), "Form SS-5 (08-2011) ef (0", fontsize=8)
    p.draw_line((72, 260), (300, 260), color=(0, 0, 0), width=0.7)
    p.insert_text((72, 272), "Name [legal] <full>/a.b", fontsize=8)
    src = tmp_path / "br.pdf"
    doc.save(src)
    assert run("make-fillable", src) == 0
    ws = widgets(tmp_path / "br-fillable.pdf")
    names = {w[1] for w in ws}
    assert len(ws) == 2 and all(not set(n) & set(".()[]<>/") for n in names), names
    assert any("(08-2011)" in (w[2] or "") for w in ws), "the readable label keeps them"
