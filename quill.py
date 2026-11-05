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
import datetime
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
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

    if os.environ.get("QUILL_NO_OPEN"):
        return
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
    used = {w.field_name.lower() for page in doc for w in page.widgets()}
    counts = {"text": 0, "multiline": 0, "checkbox": 0}
    for page in doc:
        rot = page.rotation_matrix
        avoid = [w.rect * rot for w in page.widgets()]
        found = find_blanks(page, avoid)
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
    if not wanted and not (interactive and ask_yes("Fill in today's date too? [Y/n] ", True)):
        return
    label = fill_date(doc, spot)
    if label:
        print(f"Dated \"{label}\" {today_text()}.")
    else:
        print("No empty date box found near the signature, so no date was added.")


def get_signature(args, interactive: bool) -> dict:
    import tempfile

    if args.name:
        return {"kind": "name", "name": args.name, "font": args.font or "script"}
    if args.image:
        import atexit
        import shutil

        tmpdir = Path(tempfile.mkdtemp(prefix="quill-"))
        # the cleaned signature is only needed for this run: never leave it in %TEMP%
        atexit.register(shutil.rmtree, tmpdir, True)
        if is_vector_file(args.image):
            extract_vector(args.image, tmpdir / "signature.pdf")
            return {"kind": "vector", "path": str(tmpdir / "signature.pdf")}
        clean_photo(args.image, tmpdir / "signature.png")
        return {"kind": "image", "path": str(tmpdir / "signature.png")}
    sig = load_saved()
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
        sig = save_image(raw)
        print(f"Saved it for next time: {home() / sig['file']}")
    else:
        sig = save_name(raw)
        print(f"Saved it for next time: {describe(sig)}.")
    return sig


def cmd_signature(args) -> int:
    try:
        if args.remove:
            print("Removed your saved signature." if remove_saved() else "No saved signature.")
            return 0
        if args.photo and args.name:
            raise FillError("Give a photo or --name, not both.")
        if args.name:
            sig = save_name(args.name, args.font or "script")
            print(f"Saved {describe(sig)}.")
            if not font_file(sig["font"]):
                warn("that handwriting font isn't installed; italic Times will be used.")
            return 0
        if args.photo and is_vector_file(args.photo):
            sig, dropped = save_vector(args.photo)
            out = home() / sig["file"]
            note = f" Left out {dropped} small shape(s), like a tagline." if dropped else ""
            print(f"Saved your signature artwork: {out}.{note}")
            if args.open and hasattr(os, "startfile"):
                os.startfile(preview_png(out))  # noqa: S606  # nosec B606 - our preview
            return 0
        if args.photo:
            print("Cleaning up the photo (takes a few seconds)...")
            sig = save_image(args.photo, args.rotate)
            out = home() / sig["file"]
            print(f"Saved {out}")
            print("Have a look at it. If it's sideways, run this again with --rotate 90 "
                  "(or 180, 270).")
            if args.open and hasattr(os, "startfile"):
                os.startfile(out)  # noqa: S606  # nosec B606 - our cleaned image
            return 0
        sig = load_saved()
        if sig:
            print(f"Saved signature: {describe(sig)}")
        else:
            print("No saved signature yet. Set one up with:\n"
                  "  quill signature photo.jpg\n  quill signature --name \"Your Name\"")
        return 0
    except SignError as e:
        raise FillError(str(e)) from e


def cmd_sign(args) -> int:
    interactive = bool(args.pause)
    try:
        sig = get_signature(args, interactive)
        out = pick_output(args, "signed")
        doc = open_pdf(args.pdf, args.password)
        spots = find_spots(doc)
        if args.at:
            spot = spot_matching(spots, doc, args.at)
        elif not spots:
            scan = not any(page.get_text("words") for page in doc)
            why = ("This looks like a scan with no text layer, so the \"Signature\" label "
                   "can't be read." if scan else
                   "No signature field, and no \"Signature\" label with room next to it.")
            raise FillError(f"Couldn't find where to sign. {why} Use --at X,Y,PAGE for the "
                            "signature's bottom-left corner (`quill grid` shows positions).")
        else:
            spot = choose_spot(spots, interactive)
        place(doc, spot, sig)
        offer_date(doc, spot, interactive and not args.date, wanted=args.date)
    except SignError as e:
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
    prof = load_details()
    print("Your details fill in forms automatically. Press Enter to keep what's shown,")
    print("type - to clear an answer, or Ctrl+C to stop without saving.")
    print(f"They're saved as plain text in {details_path()} (only your Windows account can open it).")
    for key, question, example in DETAIL_FIELDS:
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
                prof[key] = check_detail(key, raw)
                break
            except ValueError as e:
                print(f"    {e}")
    prof.pop("_setup", None)
    save_details(prof)
    print(f"\nSaved {len(prof)} details.")
    return prof


def cmd_profile(args) -> int:
    if args.clear:
        try:
            details_path().unlink()
            print("Deleted your saved details.")
        except FileNotFoundError:
            print("No saved details.")
        return 0
    if args.set:
        prof = load_details()
        for k, v in parse_pairs(args.set, "--set").items():
            std = k.strip().lower().replace(" ", "_")
            key = std if std in DETAIL_KEYS else k.strip()  # your own keys keep their wording
            if SENSITIVE.search(key):
                warn(f"'{key}' is saved as plain text on this PC.")
            try:
                prof[key] = check_detail(key, v)
            except ValueError as e:
                raise FillError(f"{k}: {e}") from e
            if not prof[key]:
                prof.pop(key)
        save_details(prof)
        print(f"Saved. You have {len(prof)} details.")
        return 0
    if args.show:
        prof = {k: v for k, v in load_details().items() if not k.startswith("_")}
        if not prof:
            print("No saved details yet. Run `quill profile` to add them.")
        labels = {k: q for k, q, _ in DETAIL_FIELDS}
        for k, v in prof.items():
            print(f"  {labels.get(k, k)}: {v}")
        return 0
    edit_profile()
    return 0


OFFICE_USE = re.compile(r"office use|official use|do not write below|for agency use", re.I)


def office_lines(doc) -> dict[int, float]:
    """Per page, the top of the first "for office use" heading (visible coords)."""
    out = {}
    for page in doc:
        lines: dict[tuple, list] = {}
        for w in page_words(page):
            lines.setdefault((w[2], w[3]), []).append(w)
        tops = [min(w[0].y0 for w in line) for line in lines.values()
                if OFFICE_USE.search(" ".join(w[1] for w in line))]
        if tops:
            out[page.number] = min(tops)
    return out


def autofill(form: Form, prof: dict, show: bool = True) -> dict[str, tuple[dict, object]]:
    """Fill empty fields from your details and the date. Returns what was set."""
    done = {}
    office = office_lines(form.doc)
    for f, key, value in autofill_plan(form.fillable(), prof, form.maxlens()):
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
    interactive = bool(args.pause)
    form = Form(args.pdf, args.password)
    out = pick_output(args, "filled")
    if not form.fields:
        print("This form has no fields to fill, so quill is finding its blanks first.")
        if not form.make_fillable_if_flat(ask_first=False):
            raise FillError(no_fields_message(form.doc, args.pdf))
    prof = load_details()
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
    if not args.sign and interactive and load_saved() and find_spots(form.doc):
        args.sign = ask_yes("Sign it too, with your saved signature? [Y/n] ", True)
    if args.sign:
        try:
            sig = get_signature(args, interactive)
            spots = find_spots(form.doc)
            if spots:
                spot = choose_spot(spots, interactive)
                place(form.doc, spot, sig)
                signed = True
                print(f"Signed \"{spot.label or 'Signature'}\".")
            else:
                print("No signature line found, so it isn't signed.")
        except SignError as e:
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


def split_questions(form: Form, fields: list[dict]) -> tuple[list[dict], list[dict]]:
    """(yours, unrelated). Unrelated: about someone else (a parent, spouse,
    emergency contact, a name at birth...) or in an office-use section."""
    office = office_lines(form.doc)
    mine, other = [], []
    for f in fields:
        pno, xref = f["widgets"][0]
        page = form.doc[pno]
        w = page.load_widget(xref)
        below = w is not None and (w.rect * page.rotation_matrix).y0 > office.get(pno, 1e9)
        about_others = SOMEONE_ELSE.search(field_text(f["name"], f["label"]))
        (other if below or about_others else mine).append(f)
    return mine, other


def guided(pdf: str | None, output: str | None, password: str | None,
           open_after: bool = False) -> int:
    if not pdf:
        print("Quill: fill in a PDF form.")
        pdf = clean_path(ask("Drag the PDF into this window (or type its path), then press Enter:\n> "))
        if not pdf:
            return 1
    form = Form(pdf, password)
    if not form.fillable() and not form.make_fillable_if_flat(ask_first=True):
        raise FillError(no_fields_message(form.doc, pdf))
    fields = form.fillable()
    sig = load_saved()
    if sig:  # signature boxes are offered at the end, with the saved signature
        fields = [f for f in fields if not SIG_WORDS.search(f"{f['name']} {f['label']}")]

    prof = load_details()
    if not details_path().exists():  # asked once; a "no" is remembered
        if ask_yes("\nSave your details (name, address, phone...) once, so forms fill in "
                   "by themselves? [Y/n] ", True):
            print()
            prof = edit_profile()
        else:
            save_details({"_setup": "skipped"})
            print("OK. Run `quill profile` any time to add them.")
    auto = autofill(form, prof, show=False)
    if auto:
        print(f"\nFilled {len(auto)} answer(s) from your details and today's date:")
        for f, v in auto.values():
            print(f"  {(f['label'] or f['short'])[:48]}: {v}")
    mine, other = split_questions(form, [f for f in fields if f["name"] not in auto])
    fields = mine + other  # yours first; the unrelated ones can be skipped as a group
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
        print("Type < to go back, >> to skip the rest, or press Ctrl+C to quit without saving.")
        if other:
            print(f"The last {len(other)} look unrelated (other people or office use); "
                  "you can skip them when you get there.")
    answers: dict[str, tuple[dict, object]] = dict(auto)
    i = 0
    offered = False
    while i < len(fields):
        if i == len(mine) and other and not offered:
            offered = True
            if ask_yes(f"\nThe other {len(other)} questions look like they're about someone "
                       "else or for office use. Skip them? [Y/n] ", True):
                break
        f = fields[i]
        print(question(f, i + 1, len(fields)))
        if f["name"] in answers:
            print(f"    your answer: {answers[f['name']][1]}")
        raw = ask("> ")
        if raw == ">>":
            break
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
    spots = find_spots(form.doc)
    if sig and spots and ask_yes(f"\nSign it with {describe(sig)}? [Y/n] ", True):
        spot = choose_spot(spots, interactive=True)
        place(form.doc, spot, sig)
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


# ============================================================================
# Finding the blanks on flat or scanned forms
# ============================================================================

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


# ============================================================================
# Signatures: save one once, then place it on forms
# ============================================================================

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
    other: bool = False        # looks like someone else's signature line


# "Signature Date", "Date signed": the box for the date, not for signing
SIG_DATE_BOX = re.compile(r"signature\s*date|date\s*(of\s*)?sign|date\s*signed", re.I)
# whose line it is: someone other than the person filling in the form
OTHER_SIGNER = re.compile(
    r"\b(director'?s?|sponsor'?s?|iso|alternate|designee'?s?|approv\w*|authoriz\w*|officer'?s?|"
    r"official|reviewer'?s?|reviewed|witness\w*|notary|manager'?s?|supervisor'?s?|hr|department|"
    r"agency|representative'?s?|committee|chair\w*|dean|advisor'?s?|parent'?s?|guardian'?s?|"
    r"spouse'?s?|co-?signer|employer'?s?|preparer'?s?|interpreter'?s?|office use)\b", re.I)


def _rot(page):
    return page.rotation_matrix


def rank_spots(spots: list[Spot]) -> list[Spot]:
    """Best first: your own line before anyone else's, real signature fields
    before text boxes before lines found on the page, then page order."""
    order = {"field": 0, "text-field": 1, "line": 2, "label": 2}
    for s in spots:
        s.other = bool(OTHER_SIGNER.search(s.label))
    return sorted(spots, key=lambda s: (s.other, order.get(s.source, 3), s.page,
                                        round(s.rect.y0 / 6), s.rect.x0))


def find_spots(doc: pymupdf.Document) -> list[Spot]:
    spots: list[Spot] = []
    for page in doc:
        for w in page.widgets():
            text = f"{w.field_name} {w.field_label or ''}"
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                spots.append(Spot(page.number, w.rect * _rot(page),
                                  w.field_label or w.field_name, "field", w.xref))
            elif w.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT and SIG_WORDS.search(text) \
                    and not SIG_DATE_BOX.search(text):
                spots.append(Spot(page.number, w.rect * _rot(page),
                                  w.field_label or w.field_name, "text-field", w.xref))
    if spots:
        return rank_spots(spots)


    for page in doc:
        words = page_words(page)
        sig_lines = [line for line in _text_lines(words).values()
                     if len(line) <= 6 and SIG_WORDS.search(" ".join(w[1] for w in line))]
        if not sig_lines:
            continue
        found = [Spot(page.number, b.rect, b.label, "line")
                 for b in find_blanks(page) if b.kind != "checkbox" and SIG_WORDS.search(b.label)]
        r = Raster(page)
        for line in sig_lines:
            text = " ".join(w[1] for w in line)
            box = pymupdf.Rect(line[0][0]).include_rect(line[-1][0])
            if "___" in text:
                # "Signature: ________": the underscores are the line to sign on
                unders = [h for h in page.search_for("___", clip=box + (-1, -2, 1, 2))]
                if unders:
                    line_box = pymupdf.Rect(unders[0])
                    for h in unders[1:]:
                        line_box |= h
                    line_box = line_box * page.rotation_matrix if page.rotation else line_box
                    room = pymupdf.Rect(line_box.x0, line_box.y1 - 26, line_box.x1, line_box.y1 - 2)
                    # ink already above the line means it's been signed: don't offer it
                    x0, x1 = px(room.x0), px(room.x1)
                    ys = range(px(room.y0), px(room.y1) - px(3))
                    ink = sum(r.dark(y, x0, x1) for y in ys)
                    found = [s for s in found if overlap(s.rect, room) < 0.5]  # same line, seen twice
                    if ink < 0.03 * max(1, (x1 - x0) * len(ys)):
                        found.append(Spot(page.number, room, text, "line"))
                    continue
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
        spots.extend(found)
    return rank_spots(spots)


def _text_lines(words):
    lines: dict[tuple, list] = {}
    for w in words:
        lines.setdefault((w[2], w[3]), []).append(w)
    for v in lines.values():
        v.sort(key=lambda w: w[0].x0)
    return lines


def _space_right(r, box: pymupdf.Rect) -> pymupdf.Rect | None:
    """Empty room to the right of a "Signature" label on the same line."""
    # how far right the label's own line stays empty...
    y0, y1 = max(0, px(box.y0)), min(r.h, px(box.y1))
    x = px(box.x1 + 6)
    start = x
    while x < r.w and sum(1 for y in range(y0, y1) if r.rows[y][x] == ONE) <= 1:
        x += 1
    stop = x - px(4)
    if (stop - start) / ZOOM < 54:  # under three quarters of an inch: no room
        return None
    # ...then grow up and down while that stretch stays empty, so the
    # signature gets as much height as the form leaves free
    for _ in range(px(12)):
        if y0 <= 0 or r.dark(y0 - 1, start, stop) > 1:
            break
        y0 -= 1
    for _ in range(px(10)):
        if y1 >= r.h or r.dark(y1, start, stop) > 1:
            break
        y1 += 1
    return pymupdf.Rect(start, y0 + 2, stop, y1 - 2) / ZOOM


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
    # look at the page without the field itself (its look may include a tag)
    hidden = []
    for w in page.widgets():
        if w.xref == own_xref:
            hidden.append((w.xref, page.parent.xref_get_key(w.xref, "F")))
            page.parent.xref_set_key(w.xref, "F", "2")
    try:
        r = Raster(page)
    finally:
        for xref, (kind, val) in hidden:
            page.parent.xref_set_key(xref, "F", val if kind != "null" else "null")
    x0, x1 = px(rect.x0) + 2, px(rect.x1) - 2
    y = px(rect.y0) - 1
    limit = px(rect.y1 - MAX_SIG_HEIGHT)
    while y > max(0, limit) and r.dark(y, x0, x1) <= 1:
        y -= 1
    return min(rect.y0, (y + 3) / ZOOM)


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

    # Boxes under "For office use" / "Do not write below this line" aren't ours.
    office_y = min((w[0].y0 for w in page_words(page)
                    if OFFICE.search(" ".join(x[1] for x in page_words(page)
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
        for b in find_blanks(page):
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


# ============================================================================
# Your saved details, and matching them to fields
# ============================================================================

# key, question, example
DETAIL_FIELDS = [
    ("first_name", "First name", ""),
    ("middle_name", "Middle name", ""),
    ("last_name", "Last name", ""),
    ("suffix", "Suffix (Jr., III)", ""),
    ("email", "Personal email", ""),
    ("work_email", "Work email", ""),
    ("phone", "Phone", "555-123-4567"),
    ("street", "Street address", "123 Main St"),
    ("address2", "Apartment / unit", "Apt 4"),
    ("city", "City", ""),
    ("state", "State", "TX or Texas"),
    ("zip", "ZIP code", ""),
    ("country", "Country", "United States"),
    ("birth_date", "Date of birth", "MM/DD/YYYY"),
    ("birth_city", "City of birth", ""),
    ("birth_state", "State or country of birth", ""),
    ("drivers_license", "Driver's license number", ""),
    ("employer", "Employer", ""),
    ("job_title", "Job title", ""),
]
DETAIL_KEYS = [k for k, _, _ in DETAIL_FIELDS]
SENSITIVE = re.compile(r"ssn|social|passport|license|account|routing|card|pin|password", re.I)

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico",
}
BY_NAME = {v.lower(): k for k, v in STATES.items()}


# ---------------------------------------------------------------- storage


def details_path():
    return home() / "profile.json"


def load_details() -> dict:
    try:
        data = json.loads(details_path().read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if str(v).strip()}
    except (OSError, ValueError):
        return {}


def save_details(data: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    clean = {k: v.strip() for k, v in data.items() if v and v.strip()}
    details_path().write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_date(text: str) -> datetime.date | None:
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def check_detail(key: str, value: str) -> str:
    """Normalize a profile answer, or raise ValueError with a reason."""
    value = value.strip()
    if key == "birth_date" and value:
        d = parse_date(value)
        if not d:
            raise ValueError("Use MM/DD/YYYY, for example 04/23/1990.")
        return d.strftime("%m/%d/%Y")
    if key == "phone" and value and len(re.sub(r"\D", "", value)) < 7:
        raise ValueError("That doesn't look like a phone number.")
    if key in ("email", "work_email") and value and "@" not in value:
        raise ValueError("That doesn't look like an email address.")
    return value


# ---------------------------------------------------------------- field text


VOCAB = sorted("""first last middle full name names zip code area phone number street address
city state birth date email mailing of at on mothers fathers mother father home work cell
mobile postal country today todays signed suffix prefix emergency contact title employer
company diff different recent card other used day month year dob mail apt unit line
drivers driver license licence dl no num
""".split(), key=len, reverse=True)


def split_glued(word: str) -> str:
    """'cityofbirth' -> 'city of birth', using a small vocabulary."""
    n = len(word)
    best: list = [None] * (n + 1)
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        for v in VOCAB:
            if word.startswith(v, i) and best[i + len(v)] is None:
                best[i + len(v)] = best[i] + [v]
    return " ".join(best[n]) if best[n] else word


def field_text(name: str, label: str) -> str:
    """Lower-case words describing a field, from its name and label."""
    last = name.rsplit(".", 1)[-1]
    last = re.sub(r"\[\d+\]", " ", last)
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    words = re.split(r"[^A-Za-z0-9']+", last)
    words = [split_glued(w.lower()) if len(w) > 6 else w.lower() for w in words if w]
    lab = re.sub(r"[^a-z0-9'/]+", " ", (label or "").lower())
    return re.sub(r"\s+", " ", " ".join(words) + " | " + lab).strip()


SOMEONE_ELSE = re.compile(
    r"\b(mother'?s?|father'?s?|parents?'?|spouse'?s?|husband'?s?|wife'?s?|partner'?s?|emergency|"
    r"contact|child(ren)?'?s?|dependents?|beneficiar\w*|guardian'?s?|witness|supervisor|manager|"
    r"reference|physician|doctor|preparer|notary|agent|previous|prior|former|maiden|alias|"
    r"other|different|diff|recent|landlord|owner|relationship|relative|co ?applicant|"
    r"office use|for office|official)\b")
BIRTH = re.compile(r"\b(birth|dob|born)\b")
DATE_NOT_TODAY = re.compile(
    r"\b(birth|dob|born|expir\w*|issue[ds]?|hire[ds]?|start|end|effective|due|arriv\w*|"
    r"depart\w*|from|through|period|admission|discharge|incident|loss|injury|purchase|"
    r"marriage|death|employment|graduat\w*|move[ds]?|office)\b")
WORKISH = r"\b(work|business|office|company|employer|corporate|job)\b"


@dataclass
class Rule:
    key: str
    pattern: re.Pattern
    birth_ok: bool = False            # may the field mention birth?
    unless: re.Pattern | None = None  # skip this rule (try the next) if this matches


def R(key, pattern, birth_ok=False, unless=None):
    return Rule(key, re.compile(pattern), birth_ok, re.compile(unless) if unless else None)


# Most specific first; the first rule that matches wins.
RULES = [
    # a work email box gets only the work email (empty if none is saved)
    R("work_email", rf"(?=.*{WORKISH}).*\be ?mail\b"),
    R("email", r"\be ?mail\b"),
    R("birth_date", r"\b(date of birth|birth ?date|dob|born on)\b", True),
    R("birth_city", r"\b(city|place|town) (of|at) birth\b|\bbirth ?(city|place)\b|place of birth",
      True),
    R("birth_state", r"\b(state|country)\b.*\b(of|at) birth\b|\bbirth ?(state|country)\b", True),
    # license state / class / dates aren't the number: no saved value, so left empty
    R("license_detail", r"\blicen[cs]e\b.*\b(state|class|type|expir\w*|issued?|date|restrictions?|"
      r"endorsements?|country)\b|\b(state|class|expir\w*|issued?)\b.*\blicen[cs]e\b"),
    # only a *driver's* license: a bare "license number" may be a professional one
    R("drivers_license", r"\b(drivers?|driver's|driving|operator'?s?)\b.*\blicen[cs]e\b|"
      r"\blicen[cs]e\b.*\bdriv\w*|\bdl\b|\bd ?l ?(#|no|num|number)\b"),
    R("age", r"\bage\b"),
    R("city_state_zip", r"\bcity\b.*\bstate\b.*\b(zip|postal)\b"),
    R("area_code", r"\barea code\b"),
    R("phone", r"\b(phone|telephone|tel|mobile|cell)\b"),
    R("zip", r"\b(zip|postal ?code|post ?code)\b"),
    # city/state/country before street: "Mailing address - City" is a city
    R("city", r"\b(city|town)\b"),
    R("state", r"\bstate\b"),
    R("country", r"\bcountry\b"),
    R("address2", r"\b(address (line )?2|line 2|apt|apartment|suite|unit)\b", unless=r"\bstreet\b"),
    R("street", r"\b(street|address( line)?( 1)?|addr|residence)\b"),
    R("first_name", r"\b(first|given) name\b|\bfname\b|\| first$|: first$|^first \||\bfirst$"),
    R("middle_initial", r"\b(mi|m i|middle initial|initial)\b"),
    R("middle_name", r"\bmiddle( name)?\b"),
    R("last_name", r"\b(last|family|sur) ?name\b|\blname\b|\| last$|: last$|^last \||\blast$"),
    R("suffix", r"\bsuffix\b"),
    R("prefix", r"\b(prefix|salutation|honorific)\b"),
    R("employer", r"\b(employer|company|organization|business)( name)?\b"),
    R("job_title", r"\b(job title|occupation|position)\b"),
    R("full_name", r"\b(full name|printed name|print name|your name|name|applicant)\b"),
    R("today", r"\b(today'?s?( date)?|date signed|signature date|date)\b"),
]
NOT_A_NAME = re.compile(r"\b(company|business|organization|user|file|account|bank|street|"
                        r"school|employer|product|event|project|plan|pet)\b")
WORK = re.compile(r"\b(work|office|business|fax|daytime work)\b")


# ---------------------------------------------------------------- values


def date_format(text: str) -> str:
    # field text has dashes turned to spaces, so compare with separators removed
    t = re.sub(r"[\s/.-]", "", text.lower())
    if "yyyymmdd" in t:
        return "%Y-%m-%d"
    if "ddmmyyyy" in t:
        return "%d/%m/%Y"
    if re.search(r"mmddyy(?!yy)", t):
        return "%m/%d/%y"
    return "%m/%d/%Y"


def state_value(stored: str, text: str, maxlen: int) -> str:
    s = stored.strip()
    abbr = s.upper() if s.upper() in STATES else BY_NAME.get(s.lower())
    if not abbr:
        return s  # a foreign country or region: use as given
    if maxlen and maxlen <= 3:
        return abbr
    if "abbreviat" in text:  # "do not abbreviate"
        return STATES[abbr]
    return s


def phone_parts(phone: str) -> tuple[str, str, str]:
    d = re.sub(r"\D", "", phone)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return d[:3], f"{d[3:6]}-{d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return "", phone, phone


def value_for(key: str, prof: dict, text: str, maxlen: int, form_keys: set) -> str:
    today = datetime.date.today()
    if key == "today":
        return today.strftime(date_format(text))
    if key == "birth_date":
        d = parse_date(prof.get("birth_date", ""))
        return d.strftime(date_format(text)) if d else ""
    if key == "age":
        d = parse_date(prof.get("birth_date", ""))
        if not d:
            return ""
        return str(today.year - d.year - ((today.month, today.day) < (d.month, d.day)))
    if key == "full_name":
        parts = [prof.get("first_name"), prof.get("middle_name"), prof.get("last_name"),
                 prof.get("suffix")]
        return " ".join(p for p in parts if p)
    if key == "middle_initial":
        m = prof.get("middle_name", "")
        return m[:1].upper() if m else ""
    if key in ("state", "birth_state"):
        return state_value(prof.get(key, ""), text, maxlen) if prof.get(key) else ""
    if key == "city_state_zip":
        st = state_value(prof.get("state", ""), "", 2) if prof.get("state") else ""
        left = ", ".join(p for p in (prof.get("city"), st) if p)
        return f"{left} {prof.get('zip', '')}".strip()
    if key in ("phone", "area_code"):
        if not prof.get("phone"):
            return ""
        area, rest, full = phone_parts(prof["phone"])
        if key == "area_code":
            return area
        return rest if "area_code" in form_keys and area else full
    if key == "street":
        street = prof.get("street", "")
        if street and prof.get("address2") and "address2" not in form_keys:
            street = f"{street}, {prof['address2']}"
        return street
    return prof.get(key, "")


def rule_for(name: str, label: str, ftype: str) -> str | None:
    text = field_text(name, label)
    if SOMEONE_ELSE.search(text):
        return None
    for rule in RULES:
        if not rule.pattern.search(text) or (rule.unless and rule.unless.search(text)):
            continue
        if not rule.birth_ok and BIRTH.search(text):
            return None  # e.g. "Name at birth": not the current value
        if rule.key == "today" and DATE_NOT_TODAY.search(text):
            return None
        if rule.key in ("phone", "area_code") and WORK.search(text):
            return None
        if rule.key == "full_name" and NOT_A_NAME.search(text):
            return None
        if rule.key in NAME_PARTS and _names_mentioned(text) > 1:
            return None  # "First Full Middle Name Last": a whole row, not one box
        return rule.key
    return None


NAME_PARTS = ("first_name", "middle_name", "last_name")


def _names_mentioned(text: str) -> int:
    lab = text.split("|", 1)[-1]
    return sum(1 for w in ("first", "middle", "last") if re.search(rf"\b{w}\b", lab))


def custom_rule(name: str, label: str, prof: dict) -> str | None:
    """Keys you added yourself match fields that mention all their words."""
    text = field_text(name, label)
    if SOMEONE_ELSE.search(text):
        return None
    for key in prof:
        if key in DETAIL_KEYS or key.startswith("_"):  # "_" keys are quill's own notes
            continue
        words = re.findall(r"[a-z0-9]+", key.lower().replace("_", " "))
        if words and all(re.search(rf"\b{re.escape(w)}", text) for w in words):
            return key
    return None


def autofill_plan(fields: list[dict], prof: dict, maxlens: dict[str, int] | None = None) -> list[tuple]:
    """(field, key, value) for every empty field we can fill."""
    maxlens = maxlens or {}
    keyed = []
    for f in fields:
        if f["type"] not in ("text", "dropdown", "list") or f["value"]:
            continue
        key = custom_rule(f["name"], f["label"], prof) or rule_for(f["name"], f["label"], f["type"])
        if key:
            keyed.append((f, key))
    # Two fields on one page with the same label and the same match can't be
    # told apart (two "Street" lines, area code and number both "Phone"): skip.
    def same(f, k):
        return (f.get("pages", [0])[0], k, field_text("", f["label"]))
    seen: dict[tuple, int] = {}
    for f, k in keyed:
        seen[same(f, k)] = seen.get(same(f, k), 0) + 1
    keyed = [(f, k) for f, k in keyed if not f["label"] or seen[same(f, k)] == 1]
    form_keys = {k for _, k in keyed}
    out = []
    for f, key in keyed:
        text = field_text(f["name"], f["label"])
        maxlen = maxlens.get(f["name"], 0)
        value = value_for(key, prof, text, maxlen, form_keys)
        if not value:
            continue
        if f["type"] in ("dropdown", "list"):
            value = pick_option(value, f["options"])
            if value is None:
                continue
        if maxlen and len(value) > maxlen:
            continue
        out.append((f, key, value))
    return out


def pick_option(value: str, options: list[str]) -> str | None:
    v = value.strip().lower()
    alts = {v}
    if v.upper() in STATES:
        alts.add(STATES[v.upper()].lower())
    if v in BY_NAME:
        alts.add(BY_NAME[v].lower())
    for o in options:
        if o.strip().lower() in alts:
            return o
    return None


# ---------------------------------------------------------------- Chrome panel

# Chrome's Quill button talks to this through native messaging: Chrome starts
# bin\quill-host.exe (no window) and they swap small JSON messages over stdin/stdout.
HOST_NAME = "com.silkeesmooth.quill"


def check_local_pdf(raw: str) -> Path:
    """The PDF must be an existing file on a local drive. Never a network path:
    just opening \\\\server\\x.pdf makes Windows send your login to that server."""
    raw = raw or ""
    if raw.startswith(("\\\\", "//")) or not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise FillError("Quill only opens PDFs saved on this computer, not on network shares "
                        "or websites.")
    pdf = Path(raw)
    if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
        raise FillError(f"Not a PDF on this computer: {raw}")
    return pdf


def _question(f: dict) -> dict:
    return {"name": f["name"], "label": f["label"] or f["short"], "type": f["type"],
            "options": f["options"], "value": f["value"]}


def _prepare(path: str):
    """Open the form (made fillable if flat) and work out what fills itself."""
    form = Form(check_local_pdf(path))
    flat = not form.fillable()
    if flat and not form.make_fillable_if_flat(ask_first=False):
        raise FillError("This PDF has no fields and no blanks Quill can find.")
    prof = load_details()
    auto = autofill(form, prof, show=False)
    return form, flat, prof, auto


def host_inspect(msg: dict) -> dict:
    form, flat, prof, auto = _prepare(msg.get("path", ""))
    sig = load_saved()
    rest = [f for f in form.fillable() if f["name"] not in auto
            and not (sig and SIG_WORDS.search(f"{f['name']} {f['label']}"))]
    mine, other = split_questions(form, rest)
    spots = find_spots(form.doc) if sig else []
    return {
        "spots": [{"label": s.label or "Signature", "page": s.page + 1, "other": s.other}
                  for s in spots],
        "file": form.path.name, "flat": flat,
        "details": any(not k.startswith("_") for k in prof),
        "auto": [{"label": f["label"] or f["short"], "value": str(v)} for f, v in auto.values()],
        "questions": [_question(f) for f in mine],
        "unrelated": [_question(f) for f in other],
        "signature": describe(sig) if sig else "",
        "canSign": bool(spots),
    }


def host_fill(msg: dict) -> dict:
    """Auto-fill, add any answers from the panel, sign if asked, save and open."""
    form, flat, prof, auto = _prepare(msg.get("path", ""))
    answers = {} if msg.get("signOnly") else dict(auto)
    problems = []
    for name, raw in (msg.get("answers") or {}).items():
        f = form.fields.get(name)
        if f is None or raw in ("", None):
            continue
        try:
            answers[name] = (f, check_value(f, raw))
        except FillError as e:
            problems.append(str(e))
    if problems:
        raise FillError(" ".join(problems))
    for f, v in answers.values():
        apply_value(form.doc, f, v)
    signed = ""
    if msg.get("sign"):
        sig = load_saved()
        spots = find_spots(form.doc)
        if not sig or not spots:
            raise FillError("Can't sign: " + ("no saved signature yet (run `quill signature`)."
                                              if not sig else "no signature line found."))
        n = msg.get("spot", 0)  # which line, from the panel's list (best first)
        spot = spots[n] if isinstance(n, int) and 0 <= n < len(spots) else spots[0]
        place(form.doc, spot, sig)
        signed = spot.label or "Signature"
    if not answers and not signed:
        raise FillError("Nothing to fill: no answers, and nothing matched your details.")
    if has_xfa(form.doc):
        remove_xfa(form.doc)
    out = default_output(form.path, "filled")
    save(form, out, flatten=False)
    open_result(out)
    return {"saved": str(out), "filled": len(answers), "signed": signed, "madeFillable": flat}


def host_make_fillable(msg: dict) -> dict:
    form = Form(check_local_pdf(msg.get("path", "")))
    if form.fillable():
        raise FillError("This PDF is already fillable.")
    counts = add_blank_fields(form.doc, verbose=False)
    if not sum(counts.values()):
        raise FillError("No blanks found to turn into fields.")
    out = default_output(form.path, "fillable")
    save(form, out, flatten=False)
    open_result(out)
    return {"saved": str(out), "fields": sum(counts.values())}


HOST_COMMANDS = {"inspect": host_inspect, "fill": host_fill, "make_fillable": host_make_fillable}


def host_reply(msg: dict) -> dict:
    """Answer one message from the panel. Never asks anything: there is no window."""
    import contextlib
    import io

    log = io.StringIO()
    try:
        handler = HOST_COMMANDS.get(msg.get("cmd"))
        if handler is None:
            raise FillError(f"Unknown request: {msg.get('cmd')}")
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            reply = handler(msg)
    except FillError as e:
        reply = {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - report it in the panel rather than vanish
        reply = {"error": f"Something went wrong: {e}"}
    reply["log"] = log.getvalue()[-2000:]
    return reply


def host_main() -> int:
    """Native messaging loop: 4-byte little-endian length, then UTF-8 JSON."""
    import struct

    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        head = stdin.read(4)
        if len(head) < 4:
            return 0
        body = stdin.read(struct.unpack("<I", head)[0])
        try:
            msg = json.loads(body.decode("utf-8"))
        except ValueError:
            msg = {}
        data = json.dumps(host_reply(msg if isinstance(msg, dict) else {})).encode("utf-8")
        stdout.write(struct.pack("<I", len(data)) + data)
        stdout.flush()


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


def host_entry() -> int:
    """quill-host.exe: started by Chrome for the Quill panel (no window)."""
    return host_main()


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
