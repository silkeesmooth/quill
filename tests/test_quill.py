"""Tests for Quill. Run: .venv\\Scripts\\python.exe -m pytest -q tests"""

import datetime
import json
import sys
from pathlib import Path

import pymupdf
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import quill  # noqa: E402

# ============================================================================
# Shared setup: never touch the real signature, details or Chrome
# ============================================================================

@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    """Never read or write the real saved signature and details."""
    home = tmp_path / "quill-home"
    home.mkdir()
    # existing guided-mode tests expect no "save your details?" question
    (home / "profile.json").write_text('{"_setup": "skipped"}', encoding="utf-8")
    monkeypatch.setenv("QUILL_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    """Tests must never open Chrome; results go to os.startfile, which tests stub."""
    monkeypatch.setattr(quill, "find_chrome", lambda: None)
    monkeypatch.setattr(quill.os, "startfile", lambda p: None, raising=False)


# ============================================================================
# Filling: fields, CSV, guided questions
# ============================================================================

def add(page, kind, name, rect, **kw):
    w = pymupdf.Widget()
    w.field_type = kind
    w.field_name = name
    w.rect = pymupdf.Rect(rect)
    for k, v in kw.items():
        setattr(w, k, v)
    page.add_widget(w)
    return w


@pytest.fixture
def form(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page()
    add(p, pymupdf.PDF_WIDGET_TYPE_TEXT, "first_name", (72, 72, 300, 92), field_label="First name")
    add(p, pymupdf.PDF_WIDGET_TYPE_TEXT, "address[0]", (72, 100, 300, 120))
    add(p, pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "agree", (72, 130, 86, 144))
    add(p, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "color", (72, 160, 86, 174), button_caption="red")
    add(p, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "color", (100, 160, 114, 174), button_caption="blue")
    add(p, pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "state", (72, 190, 200, 210),
        choice_values=["CA", "NY", "TX"])
    # PyMuPDF names every new radio's on-state "None"; real forms give each
    # button its own name, so rename them the way a real form would have them.
    # "dark#20blue" is how real forms escape a space in a state name
    radios = [w for w in p.widgets() if w.field_name == "color"]
    for w, state in zip(radios, ["red", "dark#20blue"]):
        off = doc.xref_get_key(w.xref, "AP/N/Off")[1]
        on = doc.xref_get_key(w.xref, "AP/N/None")[1]
        doc.xref_set_key(w.xref, "AP/N", f"<</Off {off} /{state} {on}>>")
        doc.xref_set_key(w.xref, "AS", "/Off")
    # a checkbox whose on-state holds an escaped space, like the SS-5's
    add(p, pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "citizen", (72, 220, 86, 234))
    cb = [w for w in p.widgets() if w.field_name == "citizen"][0]
    off = doc.xref_get_key(cb.xref, "AP/N/Off")[1]
    on = [k for k in cb.button_states()["normal"] if k != "Off"][0]
    on_ref = doc.xref_get_key(cb.xref, f"AP/N/{on}")[1]
    doc.xref_set_key(cb.xref, "AP/N", f"<</Off {off} /us#20citizen {on_ref}>>")
    doc.xref_set_key(cb.xref, "AS", "/Off")
    p2 = doc.new_page()
    p2.set_rotation(90)
    path = tmp_path / "form.pdf"
    doc.save(path)
    return path


def run(*args):
    return quill.main([str(a) for a in args])


def values(path):
    doc = pymupdf.open(path)
    out = {}
    for page in doc:
        for w in page.widgets():
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                if w.field_value not in (False, "Off", None, ""):
                    out[w.field_name] = quill.decode_name(w.on_state())
                out.setdefault(w.field_name, "")
            else:
                out[w.field_name] = w.field_value
    return out


def test_fields_lists_every_field(form, capsys):
    assert run("fields", form, "--json") == 0
    fields = {f["name"]: f for f in json.loads(capsys.readouterr().out)}
    assert set(fields) == {"first_name", "address[0]", "agree", "color", "state", "citizen"}
    assert fields["color"]["options"] == ["red", "dark blue"]
    assert fields["first_name"]["label"] == "First name"
    assert fields["state"]["options"] == ["CA", "NY", "TX"]
    assert fields["color"]["type"] == "radio"
    assert len(fields["color"]["options"]) == 2


def test_template_then_fill(form, tmp_path, capsys):
    tpl = tmp_path / "vals.json"
    assert run("template", form, "-o", tpl) == 0
    data = json.loads(tpl.read_text())
    assert "first_name" in data
    radio = quill.collect_fields(pymupdf.open(form))["color"]["options"]
    data.update({"first_name": "Ada", "address[0]": "1 Main St", "agree": "yes",
                 "color": radio[1], "state": "NY", "citizen": "yes"})
    tpl.write_text(json.dumps(data))
    out = tmp_path / "out.pdf"
    assert run("fill", form, "--data", tpl, "-o", out) == 0
    v = values(out)
    assert v["first_name"] == "Ada"
    assert v["address[0]"] == "1 Main St"
    assert v["agree"] not in ("Off", False, "")
    assert v["color"] == radio[1]
    on = [w for w in pymupdf.open(out)[0].widgets()
          if w.field_name == "color" and w.field_value not in (False, "Off", None, "")]
    assert len(on) == 1, "exactly one radio button should be on"
    d = pymupdf.open(out)
    cb = [w for w in d[0].widgets() if w.field_name == "citizen"][0]
    assert d.xref_get_key(cb.xref, "AS")[1] == "/us citizen", "escaped on-state must be set"
    assert v["state"] == "NY"
    # filled values must be visible in the page appearance, not just stored
    assert "Ada" in pymupdf.open(out)[0].get_text()


def test_set_overrides_data_and_unchecks(form, tmp_path):
    data = tmp_path / "d.json"
    data.write_text(json.dumps({"first_name": "A", "agree": True}))
    out = tmp_path / "o.pdf"
    assert run("fill", form, "--data", data, "--set", "first_name=B", "--set", "agree=no",
               "-o", out) == 0
    v = values(out)
    assert v["first_name"] == "B"
    assert v["agree"] in ("Off", False)


def test_unknown_field_suggests_name(form, tmp_path, capsys):
    assert run("fill", form, "--set", "firstname=x", "-o", tmp_path / "o.pdf") == 2
    err = capsys.readouterr().err
    assert "firstname" in err and "first_name" in err


def test_bad_option_rejected(form, tmp_path, capsys):
    assert run("fill", form, "--set", "color=green", "-o", tmp_path / "o.pdf") == 2
    assert "no option 'green'" in capsys.readouterr().err
    assert run("fill", form, "--set", "agree=maybe", "-o", tmp_path / "o.pdf") == 2


def test_text_placement_and_rotated_page(form, tmp_path):
    out = tmp_path / "t.pdf"
    assert run("fill", form, "--text", "100,400=Hello", "--text", "50,60,2=Turned",
               "-o", out) == 0
    doc = pymupdf.open(out)
    hit = doc[0].search_for("Hello")[0]
    assert abs(hit.x0 - 100) < 2 and abs(hit.y1 - 400) < 4
    # on the rotated page, search_for reports unrotated coords; map them back
    page = doc[1]
    hit = page.search_for("Turned")[0] * page.rotation_matrix
    assert abs(hit.x0 - 50) < 3 and abs(hit.y1 - 60) < 4


def test_position_keys_in_data_file(form, tmp_path):
    data = tmp_path / "d.json"
    data.write_text(json.dumps({"200,500": "From JSON", "first_name": "Z"}))
    out = tmp_path / "o.pdf"
    assert run("fill", form, "--data", data, "-o", out) == 0
    assert "From JSON" in pymupdf.open(out)[0].get_text()


def test_flatten_removes_fields(form, tmp_path):
    out = tmp_path / "f.pdf"
    assert run("fill", form, "--set", "first_name=Flat", "--flatten", "-o", out) == 0
    doc = pymupdf.open(out)
    assert not list(doc[0].widgets())
    assert "Flat" in doc[0].get_text()


def test_refuses_to_overwrite(form, tmp_path, capsys):
    assert run("fill", form, "--set", "first_name=x", "-o", form) == 2
    out = tmp_path / "o.pdf"
    out.write_bytes(b"keep")
    assert run("fill", form, "--set", "first_name=x", "-o", out) == 2
    assert out.read_bytes() == b"keep"
    assert run("fill", form, "--set", "first_name=x", "-o", out, "--force") == 0


def test_off_page_and_bad_page(form, tmp_path):
    assert run("fill", form, "--text", "9999,10=x", "-o", tmp_path / "o.pdf") == 2
    assert run("fill", form, "--text", "10,10,5=x", "-o", tmp_path / "o.pdf") == 2


def test_grid(form, tmp_path):
    out = tmp_path / "g.pdf"
    assert run("grid", form, "-o", out) == 0
    assert "100" in pymupdf.open(out)[0].get_text()


@pytest.mark.parametrize("indirect", [True, False])
def test_fill_removes_xfa(form, tmp_path, indirect):
    doc = pymupdf.open(form)
    cat = doc.pdf_catalog()
    acro = doc.xref_get_key(cat, "AcroForm")
    if indirect and acro[0] != "xref":
        x = doc.get_new_xref()
        doc.update_object(x, acro[1])
        doc.xref_set_key(cat, "AcroForm", f"{x} 0 R")
    if not indirect and acro[0] == "xref":
        doc.xref_set_key(cat, "AcroForm", doc.xref_object(int(acro[1].split()[0])))
    target = int(doc.xref_get_key(cat, "AcroForm")[1].split()[0]) if indirect else cat
    key = "XFA" if indirect else "AcroForm/XFA"
    doc.xref_set_key(target, key, "[(template) (x)]")
    hybrid = tmp_path / "hybrid.pdf"
    doc.save(hybrid)
    assert quill.has_xfa(pymupdf.open(hybrid))
    out = tmp_path / "o.pdf"
    assert run("fill", hybrid, "--set", "first_name=Q", "-o", out) == 0
    d = pymupdf.open(out)
    assert d.xref_get_key(d.pdf_catalog(), "AcroForm/XFA")[0] == "null"
    assert values(out)["first_name"] == "Q"


def test_flat_pdf_message(tmp_path, capsys):
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "flat.pdf"
    doc.save(p)
    assert run("fields", p) == 1
    assert "no fillable fields" in capsys.readouterr().err


# ---------------------------------------------------------------- 1.1 features


@pytest.fixture
def nested(tmp_path):
    """Field names like LiveCycle forms use: topmostSubform[0].Page1[0].x[0]."""
    doc = pymupdf.open()
    p = doc.new_page()
    names = ["topmostSubform[0].Page1[0].firstname[0]",
             "topmostSubform[0].Page1[0].citizenship[0]",
             "topmostSubform[0].Page1[0].citizenship[1]",
             "topmostSubform[0].Page1[0].city[0]",
             "topmostSubform[0].Page2[0].city[0]"]
    for k, n in enumerate(names):
        kind = pymupdf.PDF_WIDGET_TYPE_CHECKBOX if "citizenship" in n else pymupdf.PDF_WIDGET_TYPE_TEXT
        add(p, kind, n, (72, 72 + 30 * k, 200, 92 + 30 * k))
    path = tmp_path / "nested.pdf"
    doc.save(path)
    return path


def test_short_names(nested, tmp_path, capsys):
    fields = quill.collect_fields(pymupdf.open(nested))
    short = {f["short"] for f in fields.values()}
    assert "firstname" in short
    assert {"citizenship[0]", "citizenship[1]"} <= short  # siblings keep their index
    # same last part on two pages is ambiguous, so both keep full names
    assert "topmostSubform[0].Page1[0].city[0]" in short
    assert "topmostSubform[0].Page2[0].city[0]" in short
    out = tmp_path / "o.pdf"
    assert run("fill", nested, "--set", "firstname=Ann", "--set", "citizenship[1]=yes",
               "-o", out) == 0
    v = values(out)
    assert v["topmostSubform[0].Page1[0].firstname[0]"] == "Ann"
    assert v["topmostSubform[0].Page1[0].citizenship[1]"] not in ("Off", False)
    assert v["topmostSubform[0].Page1[0].citizenship[0]"] in ("Off", False)
    # full names still work
    assert run("fill", nested, "--set", "topmostSubform[0].Page1[0].firstname[0]=B",
               "-o", out, "--force") == 0


def test_default_output_names(form, capsys):
    assert run("fill", form, "--set", "first_name=a") == 0
    assert run("fill", form, "--set", "first_name=b") == 0
    assert (form.parent / "form-filled.pdf").exists()
    assert values(form.parent / "form-filled-2.pdf")["first_name"] == "b"
    assert run("grid", form) == 0
    assert (form.parent / "form-grid.pdf").exists()
    assert run("template", form) == 0
    assert (form.parent / "form-template.json").exists()


def test_csv_batch(form, tmp_path, capsys):
    assert run("template", form, "--csv", "-o", tmp_path / "t.csv") == 0
    header = (tmp_path / "t.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("filename,") and "first_name" in header
    rows = tmp_path / "rows.csv"
    rows.write_text("filename,first_name,agree,state,\"300,700\"\n"
                    "ada,Ada,yes,NY,note one\n"
                    ",Bob,no,,\n", encoding="utf-8")
    outdir = tmp_path / "batch"
    assert run("fill", form, "--csv", rows, "-o", outdir) == 0
    assert values(outdir / "ada.pdf")["first_name"] == "Ada"
    assert values(outdir / "ada.pdf")["state"] == "NY"
    assert "note one" in pymupdf.open(outdir / "ada.pdf")[0].get_text()
    assert values(outdir / "form-002.pdf")["first_name"] == "Bob"


def test_csv_bad_row_writes_nothing(form, tmp_path, capsys):
    rows = tmp_path / "rows.csv"
    rows.write_text("first_name,agree\nAda,yes\nBob,maybe\n", encoding="utf-8")
    outdir = tmp_path / "batch"
    assert run("fill", form, "--csv", rows, "-o", outdir) == 2
    assert "Row 2" in capsys.readouterr().err
    assert not outdir.exists()
    rows.write_text("filename,first_name\nsame,A\nsame,B\n", encoding="utf-8")
    assert run("fill", form, "--csv", rows, "-o", outdir) == 2
    assert not outdir.exists()


def test_safe_filename():
    assert quill.safe_filename(r"..\..\evil") == "evil.pdf"
    assert quill.safe_filename("../x/..") == ""
    assert quill.safe_filename("a:b?.pdf") == "a_b_.pdf"
    assert quill.safe_filename("  ") == ""


def scripted(monkeypatch, answers):
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr("builtins.input", fake_input)
    opened = []
    monkeypatch.setattr(quill.os, "startfile", opened.append, raising=False)
    return opened


def test_guided_mode(form, monkeypatch, capsys):
    # order: first_name, address[0], agree, color, state, citizen
    opened = scripted(monkeypatch, [
        "Ada",        # first_name
        "<",          # back to first_name
        "Grace",      # first_name again
        "",           # skip address
        "maybe",      # bad checkbox answer, asked again
        "y",          # agree
        "2",          # color: option 2 = dark blue
        "TX",         # state
        "",           # citizen skipped
        "n",          # don't open
    ])
    assert quill.main([str(form)]) == 0
    out = form.parent / "form-filled.pdf"
    v = values(out)
    assert v["first_name"] == "Grace"
    assert v["agree"] not in ("Off", False)
    assert v["color"] == "dark blue"
    assert v["state"] == "TX"
    assert opened == []
    assert "yes or no" in capsys.readouterr().out


def test_guided_asks_for_path_and_pauses(form, monkeypatch, capsys):
    opened = scripted(monkeypatch, [f'"{form}"', "Zed", "", "", "", "", "", "", ""])
    # no args at all = double-clicked: asks for the file, then waits before closing
    assert quill.main([]) == 0
    assert values(form.parent / "form-filled.pdf")["first_name"] == "Zed"
    assert opened == [form.parent / "form-filled.pdf"]
    assert "Press Enter to close" not in capsys.readouterr().out  # prompt goes via input()


def test_guided_ctrl_c_saves_nothing(form, monkeypatch):
    def boom(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", boom)
    assert quill.main([str(form)]) == 1
    assert not (form.parent / "form-filled.pdf").exists()


def test_guided_flat_pdf(tmp_path, monkeypatch, capsys):
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "flat.pdf"
    doc.save(p)
    scripted(monkeypatch, ["y"])  # yes, look for blanks (a blank page has none)
    assert quill.main([str(p), "--pause"]) == 2
    assert "no fillable fields" in capsys.readouterr().err


def test_guided_strips_bom(form, monkeypatch):
    scripted(monkeypatch, ["\ufeffAda", "", "", "", "", "", "n"])
    assert quill.main([str(form)]) == 0
    assert values(form.parent / "form-filled.pdf")["first_name"] == "Ada"


def test_guided_open_flag_skips_question(form, monkeypatch):
    opened = scripted(monkeypatch, ["Ada", "", "", "", "", ""])  # no "open it?" answer needed
    assert quill.main([str(form), "--open"]) == 0
    assert opened == [form.parent / "form-filled.pdf"]


def test_open_result_prefers_chrome(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quill, "find_chrome", lambda: r"C:\chrome.exe")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args: calls.append(args))
    quill.open_result(tmp_path / "x.pdf")
    assert calls == [[r"C:\chrome.exe", str((tmp_path / "x.pdf").resolve())]]


# ============================================================================
# Make fillable: finding blanks on flat and scanned forms
# ============================================================================

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
        words = quill.page_words(page)
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


# ============================================================================
# Signatures
# ============================================================================

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
    out = quill.clean_photo(photo, tmp_path / "clean.png")
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
    with pytest.raises(quill.SignError, match="No signature found"):
        quill.clean_photo(p, tmp_path / "x.png")


def test_save_show_remove(tmp_path, capsys, sig_home):
    assert run("signature") == 0
    assert "No saved signature" in capsys.readouterr().out
    assert run("signature", fake_photo(tmp_path / "s.jpg"), "--no-open") == 0
    assert (sig_home / "signature.png").is_file()
    assert run("signature") == 0
    assert "signature image" in capsys.readouterr().out
    assert run("signature", "--name", "Ada Lovelace") == 0
    assert not (sig_home / "signature.png").exists()
    assert quill.load_saved()["name"] == "Ada Lovelace"
    assert run("signature", "--remove") == 0
    assert quill.load_saved() is None


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
    today = quill.today_text()
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
    if quill.font_file("script"):
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
    assert names["Date signed"] == quill.today_text()
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
    assert quill.load_saved()["name"] == "Grace Hopper"
    page = pymupdf.open(tmp_path / "f-signed.pdf")[0]
    assert page.search_for("Grace Hopper") and page.search_for(quill.today_text())


def test_guided_mode_offers_signing(tmp_path, monkeypatch):
    quill.save_name("Ada Lovelace")
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
    sig = quill.load_saved()
    assert sig["kind"] == "vector"
    art = pymupdf.open(sig_home / "signature.pdf")[0]
    # cropped to the script (390 x 140 plus a little padding), tagline left out
    assert 390 <= art.rect.width < 430 and 140 <= art.rect.height < 160
    fills = {tuple(round(c, 2) for c in d["fill"]) for d in art.get_drawings() if d.get("fill")}
    assert fills == {tuple(round(c, 2) for c in quill.INK)}, "white shapes become pen ink"
    assert len(art.get_drawings()) == 2

    form = form_with_line(tmp_path / "f.pdf", with_date=False)
    assert run("sign", form) == 0
    page = pymupdf.open(tmp_path / "f-signed.pdf")[0]
    ink = [d for d in page.get_drawings() if d.get("fill") and
           tuple(round(c, 2) for c in d["fill"]) == tuple(round(c, 2) for c in quill.INK)]
    assert ink, "signature drawn as vector shapes"
    box = ink[0]["rect"]
    for d in ink[1:]:
        box |= d["rect"]
    assert 72 <= box.x0 < 80 and 290 <= box.y1 <= 300.5 and box.width > box.height * 2


def rotated_form(tmp_path: Path, rot: int = 90) -> Path:
    """A signature form stored sideways with /Rotate, shown upright."""
    flat = form_with_line(tmp_path / "upright.pdf", with_date=False)
    s = pymupdf.open(flat)
    for turn in (90, -90):
        doc = pymupdf.open()
        page = doc.new_page(width=s[0].rect.height, height=s[0].rect.width)
        page.show_pdf_page(page.rect, s, 0, rotate=turn)
        page.set_rotation(rot)
        w = next(w for w in quill.page_words(page) if w[1] == "Signature")
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
    monkeypatch.setattr(quill.os, "name", "nt")
    monkeypatch.setattr(quill.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    old = tmp_path / "appdata" / "pdffill"
    old.mkdir(parents=True)
    (old / "signature.json").write_text('{"kind": "name", "name": "Ada"}', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert quill.home() == tmp_path / "user" / ".quill"
    assert quill.load_saved()["name"] == "Ada"
    assert not old.exists()


def test_moves_data_from_old_name(tmp_path, monkeypatch):
    """Saved under the old name (~/.pdffill): moves to ~/.quill once."""
    monkeypatch.delenv("QUILL_HOME")
    monkeypatch.setattr(quill.os, "name", "nt")
    monkeypatch.setattr(quill.Path, "home", classmethod(lambda cls: tmp_path / "user"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    old = tmp_path / "user" / ".pdffill"
    old.mkdir(parents=True)
    (old / "signature.json").write_text('{"kind": "name", "name": "Ada"}', encoding="utf-8")
    (old / "profile.json").write_text('{"first_name": "Ada"}', encoding="utf-8")
    assert quill.home() == tmp_path / "user" / ".quill"
    assert sorted(p.name for p in quill.home().iterdir()) == ["profile.json", "signature.json"]
    assert not old.exists()


# ============================================================================
# Your saved details and auto-fill
# ============================================================================

PROF = {"first_name": "Jane", "middle_name": "Quinn", "last_name": "Testperson",
        "email": "jane@example.com", "phone": "(512) 555-0142", "street": "100 Test Ave",
        "address2": "Apt 2", "city": "Austin", "state": "TX", "zip": "78701",
        "birth_date": "04/23/1990", "birth_city": "Springfield", "birth_state": "Illinois"}
TODAY = datetime.date.today().strftime("%m/%d/%Y")


@pytest.fixture
def profile(private_home):
    (private_home / "profile.json").write_text(json.dumps(PROF), encoding="utf-8")
    return PROF


@pytest.mark.parametrize("name,label,key", [
    ("Name_First", "FIRST", "first_name"),
    ("topmostSubform[0].Page5[0].firstname[0]", "First Name to be shown on card", "first_name"),
    ("TO BE SHOWN ON CARD: First", "TO BE SHOWN ON CARD: First", "first_name"),
    ("lname", "", "last_name"),
    ("Surname", "", "last_name"),
    ("MI", "", "middle_initial"),
    ("Full Name", "", "full_name"),
    ("applicant_name", "Name of applicant", "full_name"),
    ("emailAddress", "", "email"),
    ("Personal email", "", "email"),
    ("home_email", "", "email"),
    ("workEmail", "", "work_email"),
    ("Email", "Business email address", "work_email"),
    ("Office email", "", "work_email"),
    ("Company e-mail", "", "work_email"),
    ("Emergency contact email", "", None),
    ("Date", "For office use only: date received", None),
    ("DriversLicenseNumber", "", "drivers_license"),
    ("driverslicense", "", "drivers_license"),
    ("dl_no", "", "drivers_license"),
    ("Text12", "Driver's License #", "drivers_license"),
    ("lic", "License number (driver)", "drivers_license"),
    ("Text13", "Driver's license state", "license_detail"),
    ("Text14", "Driver license expiration date", "license_detail"),
    ("Text15", "Class of license", "license_detail"),
    ("Text16", "Professional license number", None),
    ("Text17", "Spouse's driver's license number", None),
    ("cityofbirth", "City of Birth", "birth_city"),
    ("dob", "", "birth_date"),
    ("Birthdate", "MONTH/DAY/YEAR", "birth_date"),
    ("DateTimeField2[1]", "Today's Date MM/DD/YYYY", "today"),
    ("Date", "", "today"),
    ("date_signed", "", "today"),
    ("mailingcity", "Mailing Address - City", "city"),
    ("streetaddress", "Mailing Address - Street Address, Apartment Number", "street"),
    ("Address_2", "", "address2"),
    ("zipcode", "", "zip"),
    ("Telephone_Home", "HOME", "phone"),
    ("areacode", "Daytime Phone Number Area Code", "area_code"),
    ("csz", "City, State, ZIP", "city_state_zip"),
    ("Name_Prefix", "PREFIx", "prefix"),
    ("company_name", "Company name", "employer"),
    # someone else, or a different value: leave alone
    ("mothersfirstname", "Mother's First Name at her Birth", None),
    ("firstdiffname", "First Name at birth (if different than above)", None),
    ("Emergency_Contact", "NAME", None),
    ("Emergency_Phone", "PHONE", None),
    ("Telephone_Work", "WORK", None),
    ("spouse_name", "Spouse's full name", None),
    ("DateTimeField2[0]", "Enter any different date of birth", None),
    ("exp", "Expiration date", None),
    ("start_date", "Start date", None),
    ("SSN", "Social security no.", None),
    ("oldssnXXX", "First three digits of SSN previously assigned", None),
    ("Othername", "Other names used", None),
])
def test_rules(name, label, key):
    assert quill.rule_for(name, label, "text") == key


def plan_for(fields_spec, prof=PROF, maxlens=None):
    fields = [{"name": n, "label": lab, "type": t, "value": "", "options": o, "short": n}
              for n, lab, t, o in fields_spec]
    return {f["name"]: v for f, _, v in quill.autofill_plan(fields, prof, maxlens or {})}


def test_values_and_formats():
    got = plan_for([
        ("state", "State", "text", []),
        ("st2", "State code", "text", []),
        ("state_full", "State (do not abbreviate)", "text", []),
        ("stdrop", "Home state", "dropdown", ["Tennessee", "Texas", "Utah"]),
        ("areacode", "Area code", "text", []),
        ("phonenumber", "Phone number", "text", []),
        ("street", "Street address", "text", []),
        ("iso", "Date (YYYY-MM-DD)", "text", []),
        ("age", "Age", "text", []),
        ("mi", "MI", "text", []),
    ], maxlens={"st2": 2})
    assert got["state"] == "TX" and got["st2"] == "TX"
    assert got["state_full"] == "Texas" and got["stdrop"] == "Texas"
    assert got["areacode"] == "512" and got["phonenumber"] == "555-0142"
    assert got["street"] == "100 Test Ave, Apt 2"  # no separate apartment box
    assert got["iso"] == datetime.date.today().isoformat()
    born = datetime.date(1990, 4, 23)
    t = datetime.date.today()
    assert got["age"] == str(t.year - born.year - ((t.month, t.day) < (born.month, born.day)))
    assert got["mi"] == "Q"
    # with an apartment box present, the street stays on its own
    got = plan_for([("street", "Street", "text", []), ("apt", "Apt", "text", [])])
    assert got == {"street": "100 Test Ave", "apt": "Apt 2"}
    # phone without an area code box is written in full
    assert plan_for([("phone", "Phone", "text", [])]) == {"phone": "512-555-0142"}
    # too long for the box: left empty rather than cut off
    assert plan_for([("city", "City", "text", [])], maxlens={"city": 3}) == {}


def test_custom_keys():
    prof = dict(PROF, **{"badge number": "B-77", "_setup": "skipped"})
    got = plan_for([("badge_no", "Employee badge number", "text", []),
                    ("setup", "Setup", "text", [])], prof)
    assert got == {"badge_no": "B-77"}


def test_profile_command(capsys, private_home):
    assert quill.main(["profile", "--set", "first name=Ada", "--set", "birth_date=1815-12-10",
                         "--set", "badge number=7"]) == 0
    saved = json.loads((private_home / "profile.json").read_text(encoding="utf-8"))
    assert saved["first_name"] == "Ada" and saved["birth_date"] == "12/10/1815"
    assert saved["badge number"] == "7"
    assert quill.main(["profile", "--set", "birth_date=someday"]) == 2
    assert "MM/DD/YYYY" in capsys.readouterr().err
    assert quill.main(["profile", "--show"]) == 0
    out = capsys.readouterr().out
    assert "First name: Ada" in out and "_setup" not in out
    assert quill.main(["profile", "--clear"]) == 0
    assert not (private_home / "profile.json").exists()


def test_profile_editor(monkeypatch, private_home):
    (private_home / "profile.json").unlink()
    answers = iter(["Ada", "", "Lovelace", "", "not-an-email", "ada@example.com"] + [""] * 30)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main(["profile"]) == 0
    assert quill.load_details() == {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}


def make_form(path: Path, specs) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, (name, label, value) in enumerate(specs):
        w = pymupdf.Widget()
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        if label:
            w.field_label = label
        w.rect = pymupdf.Rect(72, 60 + i * 30, 300, 80 + i * 30)
        if value:
            w.field_value = value
        page.add_widget(w)
    doc.save(path)
    return path


def field_values(path):
    return {w.field_name: w.field_value for p in pymupdf.open(path) for w in p.widgets()}


def test_autofill_command(tmp_path, profile, capsys):
    src = make_form(tmp_path / "f.pdf", [
        ("first_name", "", ""), ("last_name", "", ""), ("email", "", "keep@me.com"),
        ("mother_first_name", "Mother's first name", ""), ("Date", "", ""),
        ("Date of birth", "", ""), ("Favorite color", "", ""),
    ])
    assert quill.main(["autofill", str(src)]) == 0
    v = field_values(tmp_path / "f-filled.pdf")
    assert v["first_name"] == "Jane" and v["last_name"] == "Testperson"
    assert v["email"] == "keep@me.com", "never overwrite an answer"
    assert v["mother_first_name"] == ""
    assert v["Date"] == TODAY and v["Date of birth"] == "04/23/1990"
    assert "2 field(s) still empty" in capsys.readouterr().out  # mother, favorite color


def test_autofill_dry_run_and_sign(tmp_path, profile):
    quill.save_name("Jane Testperson")
    src = make_form(tmp_path / "f.pdf", [("Full name", "", ""), ("Signature", "", "")])
    assert quill.main(["autofill", str(src), "--dry-run"]) == 0
    assert not (tmp_path / "f-filled.pdf").exists()
    assert quill.main(["autofill", str(src), "--sign"]) == 0
    page = pymupdf.open(tmp_path / "f-filled.pdf")[0]
    assert field_values(tmp_path / "f-filled.pdf") == {"Full name": "Jane Quinn Testperson"}
    assert len(page.search_for("Jane Testperson")) == 1  # the signature


def test_autofill_flat_form(tmp_path, profile):
    doc = pymupdf.open()
    p = doc.new_page()
    for y, label in ((120, "First name:"), (160, "Last name:"), (200, "City:"), (240, "Date:")):
        p.insert_text((72, y), label, fontsize=10)
        p.draw_line((140, y + 2), (360, y + 2), color=(0, 0, 0), width=0.6)
    src = tmp_path / "flat.pdf"
    doc.save(src)
    assert quill.main(["autofill", str(src)]) == 0
    text = pymupdf.open(tmp_path / "flat-filled.pdf")[0].get_text()
    for want in ("Jane", "Testperson", "Austin", TODAY):
        assert want in text


def test_guided_fills_known_and_asks_rest(tmp_path, profile, monkeypatch, capsys):
    src = make_form(tmp_path / "g.pdf", [("First name", "", ""), ("City", "", ""),
                                         ("Favorite color", "", "")])
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        return "teal" if len(asked) == 1 else "n"
    monkeypatch.setattr("builtins.input", fake_input)
    assert quill.main([str(src)]) == 0
    out = capsys.readouterr().out
    assert "Filled 2 answer(s)" in out and "1 questions left" in out
    assert field_values(tmp_path / "g-filled.pdf") == {"First name": "Jane", "City": "Austin",
                                                 "Favorite color": "teal"}


def test_guided_first_run_offers_setup(tmp_path, private_home, monkeypatch):
    (private_home / "profile.json").unlink()
    src = make_form(tmp_path / "g.pdf", [("First name", "", "")])
    answers = iter(["y", "Ada"] + [""] * len(quill.DETAIL_FIELDS) + ["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main([str(src)]) == 0
    assert field_values(tmp_path / "g-filled.pdf") == {"First name": "Ada"}
    assert quill.load_details() == {"first_name": "Ada"}


def test_guided_first_run_no_is_remembered(tmp_path, private_home, monkeypatch):
    (private_home / "profile.json").unlink()
    src = make_form(tmp_path / "g.pdf", [("First name", "", "")])
    answers = iter(["n", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    quill.main([str(src)])
    assert quill.load_details() == {"_setup": "skipped"}


def test_real_ss5(tmp_path, profile):
    out = tmp_path / "ss5.pdf"
    assert quill.main(["autofill", str(HERE / "ss-5.pdf"), "-o", str(out)]) == 0
    v = {k.rsplit(".", 1)[-1]: val for k, val in field_values(out).items()}
    expect = {
        "firstname[0]": "Jane", "Middlename[0]": "Quinn", "LastName[0]": "Testperson",
        "cityofbirth[0]": "Springfield", "stateatbirth[0]": "Illinois",
        "DateTimeField1[0]": "04/23/1990", "DateTimeField2[1]": TODAY,
        "areacode[0]": "512", "phonenumber[0]": "555-0142",
        "streetaddress[0]": "100 Test Ave, Apt 2", "mailingcity[0]": "Austin",
        "state[0]": "Texas", "zipcode[0]": "78701",
    }
    for k, want in expect.items():
        assert v[k] == want, k
    filled = {k for k, val in v.items() if val and val not in ("Off", False)}
    assert filled == set(expect), filled - set(expect)


def test_flat_form_context_keeps_other_people_out(tmp_path, profile):
    """Flat form: an emergency-contact section and a parent's row stay empty."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 100), "YOUR INFORMATION", fontname="hebo", fontsize=11)
    p.draw_line((72, 140), (300, 140), color=(0, 0, 0), width=0.6)
    p.insert_text((72, 150), "NAME", fontsize=7)
    p.insert_text((72, 200), "EMERGENCY CONTACT INFORMATION", fontname="hebo", fontsize=11)
    p.draw_line((72, 240), (300, 240), color=(0, 0, 0), width=0.6)
    p.insert_text((72, 250), "PHONE", fontsize=7)
    p.draw_line((72, 280), (300, 280), color=(0, 0, 0), width=0.6)
    p.insert_text((72, 290), "NAME", fontsize=7)
    # a boxed row about a parent, with a row header behind a vertical line
    p.draw_rect((60, 320, 540, 350), color=(0, 0, 0), width=0.8)
    p.draw_line((200, 320), (200, 350), color=(0, 0, 0), width=0.8)
    p.insert_text((64, 338), "MOTHER'S NAME", fontname="hebo", fontsize=10)
    p.insert_text((204, 327), "First", fontsize=6)
    # below an office-use heading
    p.insert_text((72, 420), "FOR OFFICE USE ONLY", fontname="hebo", fontsize=9)
    p.draw_line((72, 460), (300, 460), color=(0, 0, 0), width=0.6)
    p.insert_text((72, 470), "Date", fontsize=7)
    src = tmp_path / "ctx.pdf"
    doc.save(src)
    assert quill.main(["autofill", str(src)]) == 0
    page = pymupdf.open(tmp_path / "ctx-filled.pdf")[0]
    filled = {w.field_label: w.field_value for w in page.widgets() if w.field_value}
    assert filled == {"NAME": "Jane Quinn Testperson"}, filled


def test_work_and_personal_email():
    prof = dict(PROF, work_email="jane@work.example")
    got = plan_for([("email", "Email", "text", []), ("work", "Work email", "text", [])], prof)
    assert got == {"email": "jane@example.com", "work": "jane@work.example"}
    # no work email saved: the work box stays empty, never the personal one
    got = plan_for([("email", "Email", "text", []), ("work", "Work email", "text", [])])
    assert got == {"email": "jane@example.com"}


def test_drivers_license_values():
    prof = dict(PROF, drivers_license="D1234567")
    got = plan_for([("dl", "Driver's license number", "text", []),
                    ("dls", "Driver's license state", "text", []),
                    ("dlx", "Driver's license expiration date", "text", []),
                    ("pro", "License number", "text", [])], prof)
    assert got == {"dl": "D1234567"}, "only the number, never the state, date or other licenses"


# ============================================================================
# Chrome panel (native messaging) and skipping unrelated questions
# ============================================================================


def detail_form(path, specs):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, (name, label) in enumerate(specs):
        w = pymupdf.Widget()
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        if label:
            w.field_label = label
        w.rect = pymupdf.Rect(72, 60 + i * 30, 300, 80 + i * 30)
        page.add_widget(w)
    doc.save(path)
    return path


def test_local_pdf_check(tmp_path, monkeypatch):
    pdf = tmp_path / "My Form (1).pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert quill.check_local_pdf(str(pdf)) == pdf
    for bad in (str(tmp_path / "missing.pdf"), str(tmp_path / "a.txt"), "relative.pdf", ""):
        with pytest.raises(quill.FillError):
            quill.check_local_pdf(bad)
    touched = []
    monkeypatch.setattr(quill.Path, "is_file", lambda self: touched.append(self) or True)
    for unc in (r"\evil-server\share\x.pdf", "//evil-server/share/x.pdf", r"\?\UNC\evil\x.pdf"):
        with pytest.raises(quill.FillError, match="network"):
            quill.check_local_pdf(unc)
    assert touched == [], "a network path must be refused before Windows tries to open it"


def test_panel_inspect_and_fill(tmp_path, profile, monkeypatch):
    src = detail_form(tmp_path / "f.pdf", [("First name", ""), ("Favorite color", ""),
                                           ("Mother's first name", "")])
    info = quill.host_reply({"cmd": "inspect", "path": str(src)})
    assert "error" not in info, info
    assert [a["label"] for a in info["auto"]] == ["First name"]
    assert [q["name"] for q in info["questions"]] == ["Favorite color"]
    assert [q["name"] for q in info["unrelated"]] == ["Mother's first name"]
    r = quill.host_reply({"cmd": "fill", "path": str(src), "answers": {"Favorite color": "teal"}})
    assert "error" not in r, r
    assert field_values(Path(r["saved"])) == {"First name": "Jane", "Favorite color": "teal",
                                              "Mother's first name": ""}
    # a bad request comes back as a message for the panel, never a crash
    assert "error" in quill.host_reply({"cmd": "fill", "path": r"\server\x.pdf"})
    assert "error" in quill.host_reply({"cmd": "nope"})


def test_panel_sign_needs_signature(tmp_path, profile):
    src = detail_form(tmp_path / "f.pdf", [("First name", ""), ("Signature", "")])
    r = quill.host_reply({"cmd": "fill", "path": str(src), "sign": True})
    assert "no saved signature" in r["error"]
    quill.save_name("Jane Testperson")
    assert quill.host_reply({"cmd": "inspect", "path": str(src)})["canSign"] is True
    r = quill.host_reply({"cmd": "fill", "path": str(src), "sign": True})
    assert r["signed"] and pymupdf.open(r["saved"])[0].search_for("Jane Testperson")


def test_native_messaging_round_trip(tmp_path, profile, private_home):
    """The real host loop over stdin/stdout, framed the way Chrome frames it."""
    import struct
    import subprocess
    src = detail_form(tmp_path / "f.pdf", [("First name", "")])
    msgs = [{"cmd": "inspect", "path": str(src)}, {"cmd": "fill", "path": str(src)}]
    data = b"".join(struct.pack("<I", len(b)) + b for b in (json.dumps(m).encode() for m in msgs))
    env = dict(__import__("os").environ, QUILL_HOME=str(private_home), QUILL_NO_OPEN="1")
    proc = subprocess.run([sys.executable, "-c", "import quill, sys; sys.exit(quill.host_main())"],
                          input=data, capture_output=True, cwd=str(HERE.parent), env=env, timeout=120)
    out, replies = proc.stdout, []
    while out:
        n = struct.unpack("<I", out[:4])[0]
        replies.append(json.loads(out[4:4 + n]))
        out = out[4 + n:]
    assert len(replies) == 2, (proc.stdout, proc.stderr)
    assert replies[0]["auto"][0]["value"] == "Jane"
    assert Path(replies[1]["saved"]).is_file()


def test_guided_offers_to_skip_unrelated(tmp_path, profile, monkeypatch, capsys):
    src = detail_form(tmp_path / "g.pdf", [("Favorite color", ""), ("Mother's first name", ""),
                                           ("Spouse's name", "")])
    answers = iter(["teal", "y", "n"])  # color; yes skip the unrelated two; don't open
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))
    assert quill.main([str(src)]) == 0
    assert "The last 2 look unrelated" in capsys.readouterr().out
    assert any("The other 2 questions look like" in p for p in prompts)
    assert len(prompts) == 3, "the two unrelated questions were never asked"
    assert field_values(tmp_path / "g-filled.pdf")["Favorite color"] == "teal"


def test_guided_skip_rest_shortcut(tmp_path, profile, monkeypatch):
    src = detail_form(tmp_path / "g.pdf", [("Favorite color", ""), ("Favorite food", "")])
    answers = iter(["teal", ">>", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main([str(src)]) == 0
    assert field_values(tmp_path / "g-filled.pdf") == {"Favorite color": "teal", "Favorite food": ""}


def test_signature_spot_choice(tmp_path):
    """Your own signature field first; date boxes never; other people's lines last."""
    doc = pymupdf.open()
    page = doc.new_page()
    specs = [("Division Director's Signature Date", pymupdf.PDF_WIDGET_TYPE_TEXT),
             ("Sponsor Signature", pymupdf.PDF_WIDGET_TYPE_TEXT),
             ("Applicant signature", pymupdf.PDF_WIDGET_TYPE_TEXT)]
    for i, (name, kind) in enumerate(specs):
        w = pymupdf.Widget()
        w.field_type = kind
        w.field_name = name
        w.rect = pymupdf.Rect(72, 60 + i * 40, 300, 80 + i * 40)
        page.add_widget(w)
    spots = quill.find_spots(doc)
    assert [s.label for s in spots] == ["Applicant signature", "Sponsor Signature"]
    assert [s.other for s in spots] == [False, True]


def test_panel_signs_chosen_line(tmp_path, profile):
    quill.save_name("Jane Testperson")
    src = detail_form(tmp_path / "f.pdf", [("Your signature", ""), ("Witness signature", "")])
    info = quill.host_reply({"cmd": "inspect", "path": str(src)})
    assert [s["label"] for s in info["spots"]] == ["Your signature", "Witness signature"]
    r = quill.host_reply({"cmd": "fill", "path": str(src), "sign": True, "spot": 1, "signOnly": True})
    assert r["signed"] == "Witness signature" and r["filled"] == 0
    names = {w.field_name for w in pymupdf.open(r["saved"])[0].widgets()}
    assert names == {"Your signature"}, "the chosen line's box is replaced by the signature"


def test_underscore_signature_line(tmp_path):
    """'Signature: ______' signs on the underscores, and never over a signature already there."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 200), "Signature: ______________________________", fontsize=11)
    page.insert_text((72, 400), "Signature: ______________________________", fontsize=11)
    page.draw_bezier((140, 395), (170, 360), (200, 420), (250, 380), color=(0, 0, 0), width=3)  # signed
    spots = quill.find_spots(doc)
    assert len(spots) == 1, [s.rect for s in spots]
    s = spots[0]
    under = page.search_for("___")[0]
    assert s.rect.y1 <= under.y1 and s.rect.y1 > under.y1 - 6 and s.rect.x0 >= under.x0 - 1
