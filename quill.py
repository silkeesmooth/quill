"""Quill: fill in PDF forms.

  quill FORM.pdf                 fill a form: your details go in, then it asks the rest
  quill profile                  save your details once (name, address, phone...)
  quill autofill FORM.pdf        fill from your details with no questions (--sign too)
  quill make-fillable FORM.pdf   turn a flat or scanned form's blanks into fields
  quill signature PHOTO.jpg      save your signature once (or --name "Your Name")
  quill sign FORM.pdf            put it on the signature line
  quill fields   FORM.pdf        list the form's fields
  quill template FORM.pdf        write a JSON (or --csv) file of field names
  quill fill     FORM.pdf ...    fill from --set, --data, --csv or --text
  quill grid     FORM.pdf        add a grid to find x,y positions for --text

Run `quill <command> -h` for each command's options.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
from pathlib import Path

import pymupdf

__version__ = "1.4.0"

SUBCOMMANDS = ("fields", "template", "fill", "grid", "make-fillable", "signature", "sign",
               "profile", "autofill")

# "x,y" or "x,y,page" (numbers may have decimals)
POS_RE = re.compile(r"^\s*(?P<x>\d+(?:\.\d+)?)\s*,\s*(?P<y>\d+(?:\.\d+)?)\s*(?:,\s*(?P<page>\d+)\s*)?$")

TRUE_WORDS = {"1", "true", "yes", "y", "on", "x", "checked"}
FALSE_WORDS = {"0", "false", "no", "n", "off", "", "unchecked"}
FILENAME_COLUMN = "filename"

TYPE_NAMES = {
    pymupdf.PDF_WIDGET_TYPE_TEXT: "text",
    pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "dropdown",
    pymupdf.PDF_WIDGET_TYPE_LISTBOX: "list",
    pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    pymupdf.PDF_WIDGET_TYPE_BUTTON: "button",
}
UNFILLABLE = ("signature", "button", "unknown")


class FillError(Exception):
    """A problem the user can fix; printed without a traceback."""


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def find_chrome() -> str | None:
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if exe.is_file():
                return str(exe)
    return None


def open_result(path) -> None:
    """Show a finished PDF: in Chrome if it's installed, else the default app."""
    import subprocess  # nosec B404 - only used to start Chrome on our own output

    chrome = find_chrome()
    if chrome:
        # an argument list (no shell); the path is absolute, so it can't read as a flag
        subprocess.Popen([chrome, str(Path(path).resolve())])  # noqa: S603  # nosec B603
    elif hasattr(os, "startfile"):
        os.startfile(path)  # noqa: S606  # nosec B606 - opens the file we just wrote


# ---------------------------------------------------------------- opening


def open_pdf(path, password: str | None = None) -> pymupdf.Document:
    p = Path(path)
    if not p.is_file():
        raise FillError(f"File not found: {path}")
    try:
        doc = pymupdf.open(p)
    except Exception as e:  # noqa: BLE001 - pymupdf raises several types
        raise FillError(f"Could not open {path} as a PDF: {e}") from e
    if not doc.is_pdf:
        raise FillError(f"{path} is not a PDF.")
    if doc.needs_pass:
        if not password:
            raise FillError(f"{path} is password-protected. Pass --password.")
        if not doc.authenticate(password):
            raise FillError("Wrong password.")
    return doc


def has_xfa(doc: pymupdf.Document) -> bool:
    kind, _ = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")
    return kind != "null"


def remove_xfa(doc: pymupdf.Document) -> None:
    cat = doc.pdf_catalog()
    kind, val = doc.xref_get_key(cat, "AcroForm")
    if kind == "xref":
        # setting "AcroForm/XFA" on the catalog doesn't follow an indirect
        # AcroForm; it writes a placeholder string, so edit the object itself
        doc.xref_set_key(int(val.split()[0]), "XFA", "null")
    else:
        doc.xref_set_key(cat, "AcroForm/XFA", "null")
    doc.xref_set_key(cat, "NeedsRendering", "null")


def no_fields_message(doc, path) -> str:
    if has_xfa(doc):
        return (f"{path} is an XFA form (Adobe LiveCycle). Its fields can't be filled by "
                "this tool. Open it in Adobe Acrobat Reader instead, or place text with "
                "`quill fill --text`.")
    return (f"{path} has no fillable fields. It may be a scanned or flat form: run "
            "`quill make-fillable` to add fields you can type in.")


# ---------------------------------------------------------------- fields


def decode_name(s: str) -> str:
    """PDF names escape characters as #xx (e.g. "us#20citizen")."""
    return re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m[1], 16)), s)


def opt_value(choice) -> str:
    # choices are either "Text" or ["export", "Text"]
    if isinstance(choice, (list, tuple)):
        return str(choice[0])
    return str(choice)


def collect_fields(doc: pymupdf.Document) -> dict[str, dict]:
    """Group widgets by field name, in document order."""
    fields: dict[str, dict] = {}
    for page in doc:
        for w in page.widgets():
            name = w.field_name
            f = fields.get(name)
            if f is None:
                f = fields[name] = {
                    "name": name,
                    "short": name,
                    "type": TYPE_NAMES.get(w.field_type, "unknown"),
                    "label": w.field_label or "",
                    "value": w.field_value,
                    "pages": [],
                    "options": [],
                    "widgets": [],
                }
            f["widgets"].append((page.number, w.xref))
            if page.number + 1 not in f["pages"]:
                f["pages"].append(page.number + 1)
            if f["type"] in ("dropdown", "list") and not f["options"]:
                f["options"] = [opt_value(c) for c in (w.choice_values or [])]
            if f["type"] == "radio":
                state = decode_name(w.on_state() or "")
                if state and state not in f["options"]:
                    f["options"].append(state)
                if w.field_value not in (None, False, "Off", ""):
                    f["value"] = decode_name(str(w.field_value))
            if f["type"] == "checkbox":
                f["options"] = [decode_name(w.on_state() or "Yes")]
                if w.field_value not in (None, False, "Off", ""):
                    f["value"] = "yes"
    for f in fields.values():
        if f["value"] in (None, False, "Off"):
            f["value"] = ""
        elif not isinstance(f["value"], str):
            f["value"] = str(f["value"])
    add_short_names(fields)
    return fields


def add_short_names(fields: dict[str, dict]) -> None:
    """Give each field the shortest unambiguous name: the last part of
    "topmostSubform[0].Page5[0].firstname[0]" is "firstname"."""
    def last(name):
        return name.rsplit(".", 1)[-1]

    def base(name):
        return re.sub(r"\[\d+\]$", "", last(name))

    base_count: dict[str, int] = {}
    for name in fields:
        base_count[base(name)] = base_count.get(base(name), 0) + 1
    proposed = {n: (base(n) if base_count[base(n)] == 1 else last(n)) for n in fields}
    taken: dict[str, int] = {}
    for short in proposed.values():
        taken[short] = taken.get(short, 0) + 1
    for name, short in proposed.items():
        clash = taken[short] > 1 or (short in fields and short != name)
        fields[name]["short"] = name if clash or not short else short


class Form:
    def __init__(self, path, password: str | None = None):
        self.path = Path(path)
        self.doc = open_pdf(path, password)
        self.refresh()

    def refresh(self) -> None:
        """Re-read the fields (after adding some)."""
        self.fields = collect_fields(self.doc)
        self.by_short = {f["short"]: f for f in self.fields.values()}

    def fillable(self) -> list[dict]:
        return [f for f in self.fields.values() if f["type"] not in UNFILLABLE]

    def maxlens(self) -> dict[str, int]:
        out = {}
        for f in self.fields.values():
            pno, xref = f["widgets"][0]
            page = self.doc[pno]
            w = page.load_widget(xref)
            out[f["name"]] = (w.text_maxlen or 0) if w else 0
        return out

    def make_fillable_if_flat(self, ask_first: bool) -> bool:
        """For a flat form, add fields at its blanks. Returns True if it did."""
        if self.fields:
            return False
        if ask_first and not ask_yes("This form has no fields to fill. Find its blanks and "
                                     "make it fillable? [Y/n] ", True):
            return False
        counts = add_blank_fields(self.doc, verbose=False)
        self.refresh()
        if sum(counts.values()):
            print(f"Added {sum(counts.values())} fields at the form's blanks.")
        return bool(self.fields)

    def lookup(self, key: str) -> dict | None:
        return self.fields.get(key) or self.by_short.get(key)

    def resolve(self, keys) -> dict[str, dict]:
        """Map each key to its field, or fail listing every unknown key."""
        found, unknown = {}, []
        for k in keys:
            f = self.lookup(k)
            if f is None:
                unknown.append(k)
            else:
                found[k] = f
        if unknown:
            names = list(self.by_short) + list(self.fields)
            lines = []
            for k in unknown:
                near = list(dict.fromkeys(difflib.get_close_matches(k, names, n=3)))
                lines.append(f"  {k}" + (f"   (did you mean: {', '.join(near)}?)" if near else ""))
            raise FillError("No such field(s) in the form:\n" + "\n".join(lines)
                            + "\nRun `quill fields` to see the names.")
        return found


# ---------------------------------------------------------------- values


def as_bool(value, label: str) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise FillError(f"'{label}' is a checkbox: answer yes or no (got: {value}).")


def check_value(f: dict, value):
    """Validate a value for a field and return it in the form apply_value wants."""
    ftype, label = f["type"], f["short"]
    if ftype in UNFILLABLE:
        raise FillError(f"'{label}' is a {ftype} field and can't be filled.")
    if ftype == "checkbox":
        return as_bool(value, label)
    if ftype == "radio":
        if isinstance(value, bool) or str(value).strip().lower() in FALSE_WORDS:
            if value is True:
                raise FillError(f"'{label}' needs one of: {', '.join(f['options'])}")
            return None
        choice = decode_name(str(value))
        if choice not in f["options"]:
            raise FillError(f"'{label}' has no option '{value}'. "
                            f"Options: {', '.join(f['options'])}")
        return choice
    if ftype == "list" and str(value) not in f["options"]:
        raise FillError(f"'{label}' has no option '{value}'. "
                        f"Options: {', '.join(f['options'])}")
    if ftype == "dropdown" and f["options"] and str(value) not in f["options"]:
        warn(f"'{value}' is not one of the listed options for '{label}'; using it anyway.")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else ""
    return str(value)


def apply_value(doc, f: dict, value) -> None:
    for pno, xref in f["widgets"]:
        page = doc[pno]  # keep a reference: the widget dies with its page object
        w = page.load_widget(xref)
        # Use True/False for buttons: PyMuPDF picks the widget's own on-state.
        # Passing the state name back fails when it holds escapes like "#20".
        if f["type"] == "checkbox":
            w.field_value = value
        elif f["type"] == "radio":
            w.field_value = value is not None and decode_name(w.on_state() or "") == value
        else:
            w.field_value = value
        w.update()


def visible_to_page(page: pymupdf.Page, x: float, y: float) -> pymupdf.Point:
    """Convert a point measured on the page as you see it (top-left origin)
    into the page's own coordinate space, which differs when /Rotate is set."""
    return pymupdf.Point(x, y) * page.derotation_matrix


def check_position(doc, m: re.Match) -> tuple[float, float, int]:
    x, y, page_no = float(m["x"]), float(m["y"]), int(m["page"] or 1)
    if not 1 <= page_no <= doc.page_count:
        raise FillError(f"Page {page_no} doesn't exist (the PDF has {doc.page_count}).")
    rect = doc[page_no - 1].rect
    if x > rect.width or y > rect.height:
        raise FillError(f"Position {x:g},{y:g} is off page {page_no} "
                        f"(size {rect.width:.0f} x {rect.height:.0f} points).")
    return x, y, page_no


def place_text(doc, x: float, y: float, page_no: int, text: str, size: float) -> None:
    page = doc[page_no - 1]
    page.insert_text(visible_to_page(page, x, y), text, fontname="helv",
                     fontsize=size, rotate=page.rotation, color=(0, 0, 0))


def split_positions(values: dict) -> tuple[dict, list]:
    """Pull "x,y[,page]" keys out of a values dict as text placements."""
    fields, texts = {}, []
    for k, v in values.items():
        m = POS_RE.match(k)
        if m:
            texts.append((m, v))
        else:
            fields[k] = v
    return fields, texts


def fill_doc(form: Form, values: dict, texts: list, font_size: float) -> int:
    """Validate everything first, then change the document. Returns fields set."""
    resolved = form.resolve(values)
    if resolved and not form.fields:
        raise FillError(no_fields_message(form.doc, form.path))
    checked = [(resolved[k], check_value(resolved[k], v)) for k, v in values.items()]
    spots = [(check_position(form.doc, m), str(t)) for m, t in texts]
    for f, v in checked:
        apply_value(form.doc, f, v)
    if checked and has_xfa(form.doc):
        # Hybrid LiveCycle form: Acrobat would show the stale XFA copy instead of
        # the values just set, so drop it (pdftk does the same).
        remove_xfa(form.doc)
    for (x, y, p), t in spots:
        place_text(form.doc, x, y, p, t, font_size)
    return len(checked)


def save(form: Form, out: Path, flatten: bool) -> None:
    if flatten:
        form.doc.bake(annots=False, widgets=True)
    try:
        form.doc.save(out, garbage=3, deflate=True)
    except Exception as e:  # noqa: BLE001
        raise FillError(f"Could not save {out}: {e}. Is it open in another program?") from e


# ---------------------------------------------------------------- output names


def free_name(path: Path) -> Path:
    """path, or path with -2, -3 ... added if that file already exists."""
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not p.exists():
            return p
        i += 1


def default_output(src: Path, tag: str, ext: str = ".pdf") -> Path:
    return free_name(src.with_name(f"{src.stem}-{tag}{ext}"))


def check_output(out: Path, src: Path, force: bool) -> None:
    if out.resolve() == src.resolve():
        raise FillError("Output must be a different file from the input.")
    if out.exists() and not force:
        raise FillError(f"{out} already exists. Use --force to overwrite it.")


def pick_output(args, tag: str, ext: str = ".pdf") -> Path:
    src = Path(args.pdf)
    if args.output:
        out = Path(args.output)
        check_output(out, src, args.force)
        return out
    return default_output(src, tag, ext)


def safe_filename(name: str) -> str:
    # keep only the last path part (no folders, no "..") and drop characters
    # Windows forbids; Path().name would treat "a:" as a drive letter
    name = re.split(r"[\\/]", name.strip())[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return ""
    return name if name.lower().endswith(".pdf") else name + ".pdf"


# ---------------------------------------------------------------- commands


def cmd_fields(args) -> int:
    form = Form(args.pdf, args.password)
    if not form.fields:
        print(no_fields_message(form.doc, args.pdf), file=sys.stderr)
        return 1
    views = [{k: f[k] for k in ("name", "short", "type", "label", "value", "pages", "options")}
             for f in form.fields.values()]
    if args.json:
        print(json.dumps(views, indent=2, ensure_ascii=False))
        return 0
    for v in views:
        line = f"{v['short']}  [{v['type']}, page {','.join(map(str, v['pages']))}]"
        if v["label"] and v["label"] != v["short"]:
            line += f"  \"{v['label']}\""
        print(line)
        if v["options"] and v["type"] != "checkbox":
            print(f"    options: {', '.join(v['options'])}")
        if v["value"]:
            print(f"    current: {v['value']}")
    print(f"\n{len(views)} fields. Use these names with --set, in JSON or as CSV columns.")
    return 0


def cmd_template(args) -> int:
    form = Form(args.pdf, args.password)
    if not form.fields:
        print(no_fields_message(form.doc, args.pdf), file=sys.stderr)
        return 1
    fields = form.fillable()
    out = pick_output(args, "template", ".csv" if args.csv else ".json")
    if args.csv:
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow([FILENAME_COLUMN] + [f["short"] for f in fields])
            w.writerow([""] + [f["value"] for f in fields])
        print(f"Wrote {out} with {len(fields)} field columns.")
        print("Add one row per filled copy (the filename column is optional), then run:")
        print(f'  quill fill "{args.pdf}" --csv "{out}"')
        return 0
    data = {f["short"]: f["value"] for f in fields}
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(data)} fields to {out}")
    print("Fill in the values, then run:")
    print(f'  quill fill "{args.pdf}" --data "{out}"')
    return 0


def parse_pairs(items: list[str] | None, what: str) -> dict[str, str]:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise FillError(f"{what} needs NAME=VALUE, got: {item}")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def load_json(path: str) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise FillError(f"Could not read {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FillError(f"{path} is not valid JSON (line {e.lineno}: {e.msg}).") from e
    if not isinstance(data, dict):
        raise FillError(f"{path} must hold a JSON object of name: value pairs.")
    return data


def cmd_fill(args) -> int:
    if args.csv:
        if args.data or args.set or args.text:
            raise FillError("--csv can't be combined with --data, --set or --text.")
        return fill_csv(args)
    values: dict = {}
    if args.data:
        values.update(load_json(args.data))
    values.update(parse_pairs(args.set, "--set"))
    values, texts = split_positions(values)
    for key, v in parse_pairs(args.text, "--text").items():
        m = POS_RE.match(key)
        if not m:
            raise FillError(f"--text position must be X,Y or X,Y,PAGE, got: {key}")
        texts.append((m, v))
    if not values and not texts:
        raise FillError("Nothing to fill. Use --set, --data, --csv or --text "
                        "(or run `quill FORM.pdf` to be asked for each field).")
    out = pick_output(args, "filled")
    form = Form(args.pdf, args.password)
    n = fill_doc(form, values, texts, args.font_size)
    save(form, out, args.flatten)
    msg = f"Filled {n} field(s)"
    if texts:
        msg += f" and placed {len(texts)} text item(s)"
    print(f"{msg}. Saved {out}" + (" (flattened)" if args.flatten else ""))
    return 0


def fill_csv(args) -> int:
    src = Path(args.pdf)
    try:
        with open(args.csv, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as e:
        raise FillError(f"Could not read {args.csv}: {e}") from e
    except csv.Error as e:
        raise FillError(f"{args.csv} is not a valid CSV: {e}") from e
    if not rows:
        raise FillError(f"{args.csv} has no data rows under its header.")
    outdir = Path(args.output) if args.output else free_name(src.with_name(f"{src.stem}-filled"))

    # Check every row before writing anything.
    form = Form(src, args.password)
    jobs, names = [], set()
    for i, row in enumerate(rows, start=1):
        row = {(k or "").strip(): (v or "") for k, v in row.items() if k is not None}
        name = safe_filename(row.pop(FILENAME_COLUMN, "") or "") or f"{src.stem}-{i:03}.pdf"
        if name.lower() in names:
            raise FillError(f"Row {i}: two rows would both be saved as {name}.")
        names.add(name.lower())
        values, texts = split_positions({k: v for k, v in row.items() if v.strip() != ""})
        try:
            resolved = form.resolve(values)
            for k, v in values.items():
                check_value(resolved[k], v)
            for m, _ in texts:
                check_position(form.doc, m)
        except FillError as e:
            raise FillError(f"Row {i} of {args.csv}: {e}") from e
        out = outdir / name
        if out.exists() and not args.force:
            raise FillError(f"{out} already exists. Use --force to overwrite.")
        jobs.append((out, values, texts))

    outdir.mkdir(parents=True, exist_ok=True)
    for out, values, texts in jobs:
        f = Form(src, args.password)
        fill_doc(f, values, texts, args.font_size)
        save(f, out, args.flatten)
    print(f"Saved {len(jobs)} filled PDF(s) in {outdir}")
    return 0


def cmd_grid(args) -> int:
    out = pick_output(args, "grid")
    doc = open_pdf(args.pdf, args.password)
    step = args.step
    if step < 5:
        raise FillError("--step must be at least 5 points.")
    for page in doc:
        w, h = page.rect.width, page.rect.height
        shape = page.new_shape()
        x = 0.0
        while x <= w:
            major = round(x) % (step * 2) == 0
            shape.draw_line(visible_to_page(page, x, 0), visible_to_page(page, x, h))
            shape.finish(color=(1, 0, 0) if major else (1, 0.6, 0.6), width=0.4 if major else 0.2)
            x += step
        y = 0.0
        while y <= h:
            major = round(y) % (step * 2) == 0
            shape.draw_line(visible_to_page(page, 0, y), visible_to_page(page, w, y))
            shape.finish(color=(0, 0, 1) if major else (0.6, 0.6, 1), width=0.4 if major else 0.2)
            y += step
        shape.commit()
        x = step
        while x < w:
            page.insert_text(visible_to_page(page, x + 1, 7), f"{x:g}", fontsize=6,
                             color=(1, 0, 0), rotate=page.rotation)
            x += step
        y = step
        while y < h:
            page.insert_text(visible_to_page(page, 1, y - 1), f"{y:g}", fontsize=6,
                             color=(0, 0, 1), rotate=page.rotation)
            y += step
    doc.save(out, garbage=3, deflate=True)
    print(f"Saved {out}. Red numbers are x (across), blue are y (down from the top), "
          "in points (72 per inch).")
    return 0


def field_name_from(label: str, page_no: int, n: int, used: set[str]) -> str:
    # "." would make the name a hierarchy in the PDF, and PyMuPDF writes the
    # name unescaped, so brackets and the like would break the file. Viewers
    # also link fields that share a name, so every name must be unique.
    clean = re.sub(r"[.()\[\]{}<>/\\%#]", " ", label)
    base = re.sub(r"\s+", " ", clean).strip() or f"Page {page_no} blank {n}"
    name, k = base, 2
    while name.lower() in used:
        name = f"{base} {k}"
        k += 1
    used.add(name.lower())
    return name


def add_blank_fields(doc, verbose: bool = True) -> dict[str, int]:
    """Turn the blanks on every page into form fields. Returns counts by kind."""
    import quill_blanks as blanks

    used = {w.field_name.lower() for page in doc for w in page.widgets()}
    counts = {"text": 0, "multiline": 0, "checkbox": 0}
    for page in doc:
        rot = page.rotation_matrix
        avoid = [w.rect * rot for w in page.widgets()]
        found = blanks.find_blanks(page, avoid)
        for n, b in enumerate(found, start=1):
            w = pymupdf.Widget()
            w.field_name = field_name_from(b.label, page.number + 1, n, used)
            if b.label:
                w.field_label = b.label
            w.rect = b.rect * page.derotation_matrix
            w.border_width = 0
            if b.kind == "checkbox":
                w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            else:
                w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                w.text_font = "Helv"
                if b.kind == "multiline":
                    w.field_flags |= pymupdf.PDF_TX_FIELD_IS_MULTILINE
                    w.text_fontsize = 10
                else:
                    w.text_fontsize = max(6, min(11, round(b.rect.height * 0.7)))
            page.add_widget(w)
            counts[b.kind] += 1
        if found and verbose:
            print(f"  page {page.number + 1}: {len(found)} field(s)")
    return counts


def cmd_make_fillable(args) -> int:
    out = pick_output(args, "fillable")
    doc = open_pdf(args.pdf, args.password)
    existing = [w for page in doc for w in page.widgets()]
    if existing and not args.anyway:
        raise FillError(f"{args.pdf} already has {len(existing)} fillable fields, so you can "
                        "type in it as it is. Add --anyway to look for more blanks.")
    counts = add_blank_fields(doc)
    total = sum(counts.values())
    if not total:
        raise FillError("No blanks found. This works on forms with lines, boxes or "
                        "checkboxes to fill in.")
    if has_xfa(doc):
        remove_xfa(doc)
    try:
        doc.save(out, garbage=3, deflate=True)
    except Exception as e:  # noqa: BLE001
        raise FillError(f"Could not save {out}: {e}. Is it open in another program?") from e
    parts = [f"{counts['text'] + counts['multiline']} text"]
    if counts["checkbox"]:
        parts.append(f"{counts['checkbox']} checkbox")
    print(f"Added {total} fields ({', '.join(parts)}). Saved {out}")
    print("Open it in Chrome, Edge or Adobe Reader, click a box and type, then save "
          "(Ctrl+S). Check the result: a few blanks may be missed or misplaced.")
    if args.open:
        open_result(out)
    return 0


# ---------------------------------------------------------------- signatures


def choose_spot(spots, interactive: bool):
    if len(spots) == 1:
        return spots[0]
    if not interactive:
        others = "; ".join(f'"{s.label}" (page {s.page + 1})' for s in spots[1:])
        print(f"Signing \"{spots[0].label}\". Also found: {others}. Pick one with --at.")
        return spots[0]
    print("\nWhere should the signature go?")
    for k, s in enumerate(spots, start=1):
        print(f"    {k}. {s.label or 'Signature'} (page {s.page + 1})")
    while True:
        raw = ask("> ") or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(spots):
            return spots[int(raw) - 1]
        print(f"    Type a number from 1 to {len(spots)}.")


def offer_date(doc, spot, interactive: bool, wanted: bool) -> None:
    import quill_sign as ps

    if not wanted and not (interactive and ask_yes("Fill in today's date too? [Y/n] ", True)):
        return
    label = ps.fill_date(doc, spot)
    if label:
        print(f"Dated \"{label}\" {ps.today_text()}.")
    else:
        print("No empty date box found near the signature, so no date was added.")


def get_signature(args, interactive: bool) -> dict:
    import tempfile

    import quill_sign as ps

    if args.name:
        return {"kind": "name", "name": args.name, "font": args.font or "script"}
    if args.image:
        import atexit
        import shutil

        tmpdir = Path(tempfile.mkdtemp(prefix="quill-"))
        # the cleaned signature is only needed for this run: never leave it in %TEMP%
        atexit.register(shutil.rmtree, tmpdir, True)
        if ps.is_vector_file(args.image):
            ps.extract_vector(args.image, tmpdir / "signature.pdf")
            return {"kind": "vector", "path": str(tmpdir / "signature.pdf")}
        ps.clean_photo(args.image, tmpdir / "signature.png")
        return {"kind": "image", "path": str(tmpdir / "signature.png")}
    sig = ps.load_saved()
    if sig:
        return sig
    if not interactive:
        raise FillError("No saved signature yet. Set one up once:\n"
                        "  quill signature photo.jpg        (a photo of your signature)\n"
                        "  quill signature --name \"Your Name\"   (typed, in handwriting)")
    print("No saved signature yet. Either:")
    print("  - drag a photo of your signature (dark ink on white paper) into this window, or")
    print("  - type your name for a typed signature in handwriting.")
    raw = clean_path(ask("Then press Enter:\n> "))
    if not raw:
        raise FillError("No signature given.")
    if Path(raw).is_file():
        print("Cleaning up the photo (takes a few seconds)...")
        sig = ps.save_image(raw)
        print(f"Saved it for next time: {ps.home() / sig['file']}")
    else:
        sig = ps.save_name(raw)
        print(f"Saved it for next time: {ps.describe(sig)}.")
    return sig


def cmd_signature(args) -> int:
    import quill_sign as ps

    try:
        if args.remove:
            print("Removed your saved signature." if ps.remove_saved() else "No saved signature.")
            return 0
        if args.photo and args.name:
            raise FillError("Give a photo or --name, not both.")
        if args.name:
            sig = ps.save_name(args.name, args.font or "script")
            print(f"Saved {ps.describe(sig)}.")
            if not ps.font_file(sig["font"]):
                warn("that handwriting font isn't installed; italic Times will be used.")
            return 0
        if args.photo and ps.is_vector_file(args.photo):
            sig, dropped = ps.save_vector(args.photo)
            out = ps.home() / sig["file"]
            note = f" Left out {dropped} small shape(s), like a tagline." if dropped else ""
            print(f"Saved your signature artwork: {out}.{note}")
            if args.open and hasattr(os, "startfile"):
                os.startfile(ps.preview_png(out))  # noqa: S606  # nosec B606 - our preview
            return 0
        if args.photo:
            print("Cleaning up the photo (takes a few seconds)...")
            sig = ps.save_image(args.photo, args.rotate)
            out = ps.home() / sig["file"]
            print(f"Saved {out}")
            print("Have a look at it. If it's sideways, run this again with --rotate 90 "
                  "(or 180, 270).")
            if args.open and hasattr(os, "startfile"):
                os.startfile(out)  # noqa: S606  # nosec B606 - our cleaned image
            return 0
        sig = ps.load_saved()
        if sig:
            print(f"Saved signature: {ps.describe(sig)}")
        else:
            print("No saved signature yet. Set one up with:\n"
                  "  quill signature photo.jpg\n  quill signature --name \"Your Name\"")
        return 0
    except ps.SignError as e:
        raise FillError(str(e)) from e


def cmd_sign(args) -> int:
    import quill_sign as ps

    interactive = bool(args.pause)
    try:
        sig = get_signature(args, interactive)
        out = pick_output(args, "signed")
        doc = open_pdf(args.pdf, args.password)
        spots = ps.find_spots(doc)
        if args.at:
            spot = ps.spot_matching(spots, doc, args.at)
        elif not spots:
            scan = not any(page.get_text("words") for page in doc)
            why = ("This looks like a scan with no text layer, so the \"Signature\" label "
                   "can't be read." if scan else
                   "No signature field, and no \"Signature\" label with room next to it.")
            raise FillError(f"Couldn't find where to sign. {why} Use --at X,Y,PAGE for the "
                            "signature's bottom-left corner (`quill grid` shows positions).")
        else:
            spot = choose_spot(spots, interactive)
        ps.place(doc, spot, sig)
        offer_date(doc, spot, interactive and not args.date, wanted=args.date)
    except ps.SignError as e:
        raise FillError(str(e)) from e
    if has_xfa(doc):
        remove_xfa(doc)
    try:
        doc.save(out, garbage=3, deflate=True)
    except Exception as e:  # noqa: BLE001
        raise FillError(f"Could not save {out}: {e}. Is it open in another program?") from e
    print(f"Signed \"{spot.label or 'Signature'}\" on page {spot.page + 1}. Saved {out}")
    if args.open:
        open_result(out)
    return 0


# ---------------------------------------------------------------- your details


def edit_profile() -> dict:
    """Ask for each detail, showing what's saved. Returns the new profile."""
    import quill_profile as pp

    prof = pp.load()
    print("Your details fill in forms automatically. Press Enter to keep what's shown,")
    print("type - to clear an answer, or Ctrl+C to stop without saving.")
    print(f"They're saved as plain text in {pp.path()} (only your Windows account can open it).")
    for key, question, example in pp.FIELDS:
        now = prof.get(key, "")
        hint = f" [{now}]" if now else (f" (e.g. {example})" if example else "")
        while True:
            raw = ask(f"{question}{hint}: ")
            if raw == "":
                break
            if raw == "-":
                prof.pop(key, None)
                break
            try:
                prof[key] = pp.check_value(key, raw)
                break
            except ValueError as e:
                print(f"    {e}")
    prof.pop("_setup", None)
    pp.save(prof)
    print(f"\nSaved {len(prof)} details.")
    return prof


def cmd_profile(args) -> int:
    import quill_profile as pp

    if args.clear:
        try:
            pp.path().unlink()
            print("Deleted your saved details.")
        except FileNotFoundError:
            print("No saved details.")
        return 0
    if args.set:
        prof = pp.load()
        for k, v in parse_pairs(args.set, "--set").items():
            std = k.strip().lower().replace(" ", "_")
            key = std if std in pp.KEYS else k.strip()  # your own keys keep their wording
            if pp.SENSITIVE.search(key):
                warn(f"'{key}' is saved as plain text on this PC.")
            try:
                prof[key] = pp.check_value(key, v)
            except ValueError as e:
                raise FillError(f"{k}: {e}") from e
            if not prof[key]:
                prof.pop(key)
        pp.save(prof)
        print(f"Saved. You have {len(prof)} details.")
        return 0
    if args.show:
        prof = {k: v for k, v in pp.load().items() if not k.startswith("_")}
        if not prof:
            print("No saved details yet. Run `quill profile` to add them.")
        labels = {k: q for k, q, _ in pp.FIELDS}
        for k, v in prof.items():
            print(f"  {labels.get(k, k)}: {v}")
        return 0
    edit_profile()
    return 0


OFFICE_USE = re.compile(r"office use|official use|do not write below|for agency use", re.I)


def office_lines(doc) -> dict[int, float]:
    """Per page, the top of the first "for office use" heading (visible coords)."""
    import quill_blanks as pb

    out = {}
    for page in doc:
        lines: dict[tuple, list] = {}
        for w in pb.page_words(page):
            lines.setdefault((w[2], w[3]), []).append(w)
        tops = [min(w[0].y0 for w in line) for line in lines.values()
                if OFFICE_USE.search(" ".join(w[1] for w in line))]
        if tops:
            out[page.number] = min(tops)
    return out


def autofill(form: Form, prof: dict, show: bool = True) -> dict[str, tuple[dict, object]]:
    """Fill empty fields from your details and the date. Returns what was set."""
    import quill_profile as pp

    done = {}
    office = office_lines(form.doc)
    for f, key, value in pp.plan(form.fillable(), prof, form.maxlens()):
        pno, xref = f["widgets"][0]
        page = form.doc[pno]
        w = page.load_widget(xref)
        if w is not None and (w.rect * page.rotation_matrix).y0 > office.get(pno, 1e9):
            continue  # below "For office use only" / "Do not write below this line"
        try:
            v = check_value(f, value)
        except FillError:
            continue
        done[f["name"]] = (f, v)
        if show:
            print(f"  {(f['label'] or f['short'])[:48]}: {value}")
    return done


def cmd_autofill(args) -> int:
    import quill_profile as pp
    import quill_sign as ps

    interactive = bool(args.pause)
    form = Form(args.pdf, args.password)
    out = pick_output(args, "filled")
    if not form.fields:
        print("This form has no fields to fill, so quill is finding its blanks first.")
        if not form.make_fillable_if_flat(ask_first=False):
            raise FillError(no_fields_message(form.doc, args.pdf))
    prof = pp.load()
    if not prof:
        if interactive and ask_yes("You haven't saved your details yet. Do that now? [Y/n] ", True):
            prof = edit_profile()
        else:
            print("No saved details yet (run `quill profile`), so only dates are filled.")
    print("Filled in:")
    done = autofill(form, prof)
    if not done:
        print("  nothing: no fields matched your details.")
    for f, v in done.values():
        apply_value(form.doc, f, v)
    signed = False
    if not args.sign and interactive and ps.load_saved() and ps.find_spots(form.doc):
        args.sign = ask_yes("Sign it too, with your saved signature? [Y/n] ", True)
    if args.sign:
        try:
            sig = get_signature(args, interactive)
            spots = ps.find_spots(form.doc)
            if spots:
                spot = choose_spot(spots, interactive)
                ps.place(form.doc, spot, sig)
                signed = True
                print(f"Signed \"{spot.label or 'Signature'}\".")
            else:
                print("No signature line found, so it isn't signed.")
        except ps.SignError as e:
            raise FillError(str(e)) from e
    if args.dry_run:
        print("(Dry run: nothing saved.)")
        return 0
    if not done and not signed:
        print("Nothing to save.")
        return 0
    if has_xfa(form.doc):
        remove_xfa(form.doc)
    save(form, out, args.flatten)
    left = [f for f in form.fillable() if f["name"] not in done and not f["value"]]
    print(f"Saved {out}")
    if left:
        print(f"{len(left)} field(s) still empty. Fill the rest in Chrome, or run "
              f"`quill \"{out}\"` to be asked.")
    if args.open:
        open_result(out)
    return 0


# ---------------------------------------------------------------- guided mode


def ask(prompt: str) -> str:
    # PowerShell 5.1 starts piped input with a byte-order mark; drop it
    return input(prompt).lstrip("\ufeff").strip()


def clean_path(s: str) -> str:
    # A file dragged into a console arrives quoted, and PowerShell may add "& ".
    s = s.strip()
    if s.startswith("& "):
        s = s[2:].strip()
    return s.strip('"').strip("'")


def ask_yes(prompt: str, default: bool) -> bool:
    raw = ask(prompt).lower()
    return default if not raw else raw in TRUE_WORDS


def question(f: dict, i: int, n: int) -> str:
    label = f["label"] or f["short"]
    lines = [f"\n[{i}/{n}] {label}"]
    if f["type"] == "checkbox":
        lines.append("    yes or no")
    elif f["options"]:
        for k, opt in enumerate(f["options"], start=1):
            lines.append(f"    {k}. {opt}")
    if f["value"]:
        lines.append(f"    now: {f['value']}")
    return "\n".join(lines)


def guided(pdf: str | None, output: str | None, password: str | None,
           open_after: bool = False) -> int:
    if not pdf:
        print("Quill: fill in a PDF form.")
        pdf = clean_path(ask("Drag the PDF into this window (or type its path), then press Enter:\n> "))
        if not pdf:
            return 1
    import quill_sign as ps

    import quill_profile as pp

    form = Form(pdf, password)
    if not form.fillable() and not form.make_fillable_if_flat(ask_first=True):
        raise FillError(no_fields_message(form.doc, pdf))
    fields = form.fillable()
    sig = ps.load_saved()
    if sig:  # signature boxes are offered at the end, with the saved signature
        fields = [f for f in fields if not ps.SIG_WORDS.search(f"{f['name']} {f['label']}")]

    prof = pp.load()
    if not pp.path().exists():  # asked once; a "no" is remembered
        if ask_yes("\nSave your details (name, address, phone...) once, so forms fill in "
                   "by themselves? [Y/n] ", True):
            print()
            prof = edit_profile()
        else:
            pp.save({"_setup": "skipped"})
            print("OK. Run `quill profile` any time to add them.")
    auto = autofill(form, prof, show=False)
    if auto:
        print(f"\nFilled {len(auto)} answer(s) from your details and today's date:")
        for f, v in auto.values():
            print(f"  {(f['label'] or f['short'])[:48]}: {v}")
    fields = [f for f in fields if f["name"] not in auto]
    src = Path(pdf)
    if output:
        out = Path(output)
        check_output(out, src, force=False)
    else:
        out = default_output(src, "filled")

    left = "questions" if not auto else "questions left"
    print(f"\n{src.name}: {len(fields)} {left}.")
    if fields:
        print("Type an answer and press Enter. Enter alone skips (keeps what's there).")
        print("Type < to go back, or press Ctrl+C to quit without saving.")
    answers: dict[str, tuple[dict, object]] = dict(auto)
    i = 0
    while i < len(fields):
        f = fields[i]
        print(question(f, i + 1, len(fields)))
        if f["name"] in answers:
            print(f"    your answer: {answers[f['name']][1]}")
        raw = ask("> ")
        if raw == "<":
            i = max(0, i - 1)
            continue
        if raw == "":
            i += 1
            continue
        if f["type"] in ("radio", "dropdown", "list") and raw.isdigit() \
                and 1 <= int(raw) <= len(f["options"]):
            raw = f["options"][int(raw) - 1]
        try:
            answers[f["name"]] = (f, check_value(f, raw))
        except FillError as e:
            print(f"    {e}")
            continue
        i += 1

    for f, v in answers.values():
        apply_value(form.doc, f, v)
    signed = False
    spots = ps.find_spots(form.doc)
    if sig and spots and ask_yes(f"\nSign it with {ps.describe(sig)}? [Y/n] ", True):
        spot = choose_spot(spots, interactive=True)
        ps.place(form.doc, spot, sig)
        signed = True
        offer_date(form.doc, spot, interactive=True, wanted=False)
    if not answers and not signed:
        print("\nNo answers given, so nothing was saved.")
        return 0
    if has_xfa(form.doc):
        remove_xfa(form.doc)
    save(form, out, flatten=False)
    print(f"\nSaved {out}")
    if spots and not sig:
        print("To sign it: right-click the saved PDF > Send to > Sign PDF.")
    if open_after or ask_yes("Open it now? [Y/n] ", True):
        open_result(out)
    return 0


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quill", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"quill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, output_help=None):
        sp.add_argument("pdf", help="the PDF form")
        sp.add_argument("--password", help="password, if the PDF needs one to open")
        if output_help:
            sp.add_argument("-o", "--output", help=output_help)
            sp.add_argument("--force", action="store_true", help="overwrite the output")

    sp = sub.add_parser("fields", help="list the form's fields")
    common(sp)
    sp.add_argument("--json", action="store_true", help="print JSON instead of text")
    sp.set_defaults(func=cmd_fields)

    sp = sub.add_parser("template", help="write a JSON or CSV file of field names")
    common(sp, "file to write (default: FORM-template.json or .csv)")
    sp.add_argument("--csv", action="store_true", help="write a CSV for filling many copies")
    sp.set_defaults(func=cmd_template)

    sp = sub.add_parser("fill", help="fill the form and save a copy",
                        description="Fill form fields and/or place text. Positions are "
                        "in points (72 per inch) from the top-left corner of the page "
                        "as you see it; y is the text's baseline. Page numbers start at 1.")
    common(sp, "PDF to write (default: FORM-filled.pdf); with --csv, a folder")
    sp.add_argument("--set", action="append", metavar="NAME=VALUE",
                    help="set one field (repeatable; overrides --data)")
    sp.add_argument("--data", help="JSON file of {\"field name\": \"value\"}; keys like "
                    "\"100,200\" or \"100,200,2\" place text at that position")
    sp.add_argument("--csv", help="CSV with one row per copy; columns are field names, "
                    "plus an optional 'filename' column")
    sp.add_argument("--text", action="append", metavar="X,Y[,PAGE]=TEXT",
                    help="place text at a position (repeatable)")
    sp.add_argument("--font-size", type=float, default=10, help="size for placed text (default 10)")
    sp.add_argument("--flatten", action="store_true",
                    help="make the filled fields permanent (no longer editable)")
    sp.set_defaults(func=cmd_fill)

    sp = sub.add_parser("grid", help="add a coordinate grid to find positions")
    common(sp, "PDF to write (default: FORM-grid.pdf)")
    sp.add_argument("--step", type=float, default=25, help="grid spacing in points (default 25)")
    sp.set_defaults(func=cmd_grid)

    sp = sub.add_parser("make-fillable", help="turn a flat or scanned form's blanks into fields",
                        description="Find the lines, boxes and checkboxes on a flat or "
                        "scanned form and add form fields there, so you can click and type "
                        "in any PDF viewer.")
    common(sp, "PDF to write (default: FORM-fillable.pdf)")
    sp.add_argument("--open", action="store_true", help="open the result when done")
    sp.add_argument("--anyway", action="store_true",
                    help="look for more blanks even if the PDF already has fields")
    sp.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_make_fillable)

    sp = sub.add_parser("signature", help="save your signature once (photo or typed name)",
                        description="Save a signature to use with `quill sign`. A photo of "
                        "your signature on white paper is cleaned up (background removed, "
                        "cropped). Or use --name for your name in a handwriting font.")
    sp.add_argument("photo", nargs="?", help="photo or scan of your signature, or vector "
                    "artwork of it (PDF, Illustrator .ai or SVG)")
    sp.add_argument("--name", help="use your typed name instead of a photo")
    sp.add_argument("--font", choices=["script", "print", "inkfree"],
                    help="handwriting font for --name (default: script)")
    sp.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                    help="turn the photo first, if it comes out sideways")
    sp.add_argument("--open", action=argparse.BooleanOptionalAction, default=True,
                    help="show the cleaned-up signature (default: yes)")
    sp.add_argument("--remove", action="store_true", help="delete the saved signature")
    sp.set_defaults(func=cmd_signature)

    sp = sub.add_parser("sign", help="put your signature on the form's signature line",
                        description="Place your saved signature on the form's signature "
                        "field or line. This is a picture of a signature, not a "
                        "certificate-based digital signature.")
    common(sp, "PDF to write (default: FORM-signed.pdf)")
    sp.add_argument("--at", help="which spot: part of its label (e.g. \"employee\"), or "
                    "X,Y[,PAGE] for the signature's bottom-left corner")
    sp.add_argument("--date", action="store_true", help="also fill the nearest date box with today")
    sp.add_argument("--image", help="use this photo of a signature instead of the saved one")
    sp.add_argument("--name", help="use this typed name instead of the saved signature")
    sp.add_argument("--font", choices=["script", "print", "inkfree"], help="font for --name")
    sp.add_argument("--open", action="store_true", help="open the result when done")
    sp.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_sign)

    sp = sub.add_parser("profile", help="save your details (name, address...) for auto-fill",
                        description="Save your details once; forms then fill them in by "
                        "themselves. With no options, asks for each one.")
    sp.add_argument("--show", action="store_true", help="print your saved details")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="set one detail, e.g. --set city=Austin (repeatable); your own keys "
                    "work too, e.g. --set \"badge number=1234\"")
    sp.add_argument("--clear", action="store_true", help="delete your saved details")
    sp.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("autofill", help="fill a form from your details and today's date",
                        description="Fill every empty field that matches your saved details "
                        "(see `quill profile`) plus today's date, without questions. Flat "
                        "forms are made fillable first.")
    common(sp, "PDF to write (default: FORM-filled.pdf)")
    sp.add_argument("--sign", action="store_true", help="also sign it with your saved signature")
    sp.add_argument("--flatten", action="store_true", help="make the answers permanent")
    sp.add_argument("--dry-run", action="store_true", help="show what would be filled; save nothing")
    sp.add_argument("--open", action="store_true", help="open the result when done")
    sp.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_autofill, name=None, image=None, font=None)
    return p


def guided_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quill", add_help=False)
    p.add_argument("pdf", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("--password")
    p.add_argument("--pause", action="store_true")
    p.add_argument("--open", action="store_true")  # open the result without asking
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if not argv or argv[0] not in SUBCOMMANDS + ("-h", "--help", "--version"):
        a, extra = guided_parser().parse_known_args(argv)
        if extra:
            build_parser().parse_args(argv)  # prints the usual usage error
        pause = a.pause or not argv  # double-clicked or Send To: keep the window open
        try:
            return guided(a.pdf, a.output, a.password, a.open)
        except FillError as e:
            print(f"\nerror: {e}", file=sys.stderr)
            return 2
        except (KeyboardInterrupt, EOFError):
            print("\nStopped. Nothing was saved.")
            return 1
        finally:
            if pause:
                try:
                    input("\nPress Enter to close.")
                except (KeyboardInterrupt, EOFError):
                    pass

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FillError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        if getattr(args, "pause", False):  # launched from Send To: keep the window open
            try:
                input("\nPress Enter to close.")
            except (KeyboardInterrupt, EOFError):
                pass


if __name__ == "__main__":
    sys.exit(main())
