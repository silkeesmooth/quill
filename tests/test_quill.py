import json
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quill  # noqa: E402


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
