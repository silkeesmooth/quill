import datetime
import json
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quill  # noqa: E402
import quill_profile as pp  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
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
    assert pp.rule_for(name, label, "text") == key


def plan_for(fields_spec, prof=PROF, maxlens=None):
    fields = [{"name": n, "label": lab, "type": t, "value": "", "options": o, "short": n}
              for n, lab, t, o in fields_spec]
    return {f["name"]: v for f, _, v in pp.plan(fields, prof, maxlens or {})}


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
    assert pp.load() == {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}


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


def values(path):
    return {w.field_name: w.field_value for p in pymupdf.open(path) for w in p.widgets()}


def test_autofill_command(tmp_path, profile, capsys):
    src = make_form(tmp_path / "f.pdf", [
        ("first_name", "", ""), ("last_name", "", ""), ("email", "", "keep@me.com"),
        ("mother_first_name", "Mother's first name", ""), ("Date", "", ""),
        ("Date of birth", "", ""), ("Favorite color", "", ""),
    ])
    assert quill.main(["autofill", str(src)]) == 0
    v = values(tmp_path / "f-filled.pdf")
    assert v["first_name"] == "Jane" and v["last_name"] == "Testperson"
    assert v["email"] == "keep@me.com", "never overwrite an answer"
    assert v["mother_first_name"] == ""
    assert v["Date"] == TODAY and v["Date of birth"] == "04/23/1990"
    assert "2 field(s) still empty" in capsys.readouterr().out  # mother, favorite color


def test_autofill_dry_run_and_sign(tmp_path, profile):
    import quill_sign as ps
    ps.save_name("Jane Testperson")
    src = make_form(tmp_path / "f.pdf", [("Full name", "", ""), ("Signature", "", "")])
    assert quill.main(["autofill", str(src), "--dry-run"]) == 0
    assert not (tmp_path / "f-filled.pdf").exists()
    assert quill.main(["autofill", str(src), "--sign"]) == 0
    page = pymupdf.open(tmp_path / "f-filled.pdf")[0]
    assert values(tmp_path / "f-filled.pdf") == {"Full name": "Jane Quinn Testperson"}
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
    assert values(tmp_path / "g-filled.pdf") == {"First name": "Jane", "City": "Austin",
                                                 "Favorite color": "teal"}


def test_guided_first_run_offers_setup(tmp_path, private_home, monkeypatch):
    (private_home / "profile.json").unlink()
    src = make_form(tmp_path / "g.pdf", [("First name", "", "")])
    answers = iter(["y", "Ada"] + [""] * len(pp.FIELDS) + ["n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert quill.main([str(src)]) == 0
    assert values(tmp_path / "g-filled.pdf") == {"First name": "Ada"}
    assert pp.load() == {"first_name": "Ada"}


def test_guided_first_run_no_is_remembered(tmp_path, private_home, monkeypatch):
    (private_home / "profile.json").unlink()
    src = make_form(tmp_path / "g.pdf", [("First name", "", "")])
    answers = iter(["n", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    quill.main([str(src)])
    assert pp.load() == {"_setup": "skipped"}


def test_real_ss5(tmp_path, profile):
    out = tmp_path / "ss5.pdf"
    assert quill.main(["autofill", str(FIXTURES / "ss-5.pdf"), "-o", str(out)]) == 0
    v = {k.rsplit(".", 1)[-1]: val for k, val in values(out).items()}
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
