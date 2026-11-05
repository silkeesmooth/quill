# Quill

Fill in PDF forms on your own computer. No account, no upload, no web server.

## Install (Windows)

Needs [Python 3.10+](https://www.python.org/downloads/). In this folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

On Mac or Linux, run `pip install .` instead.

## Use

**Save your details once:** Start menu → **My details for PDF forms** (or
`quill profile`). Your name, address, phone, email and date of birth then fill
in by themselves, along with today's date.

**Shift+right-click a PDF → Fill in with Quill** (or right-click → Show more
options; it's also under Send to). It fills in what it knows, asks the rest in
plain words, offers to sign, and opens `form-filled.pdf` in Chrome.
**Auto-fill with Quill** does the same without asking anything.

It leaves fields about other people empty: a parent, a spouse, an emergency
contact, a name at birth, a previous address. It also skips office-use
sections and never overwrites an answer that's already there.

From a terminal:

```powershell
quill form.pdf                              # same step-by-step questions
quill autofill form.pdf --sign              # no questions
quill fields form.pdf                       # list the field names
quill fill form.pdf --set firstname=Jane    # fill exact fields
```

**Many copies:** `quill template form.pdf --csv` makes a spreadsheet. Add a row
per person, then run `quill fill form.pdf --csv form-template.csv`.

**Flat or scanned forms** (no boxes to click): right-click → Send to → **Make
PDF fillable**, or run `quill make-fillable form.pdf`. It finds the lines,
boxes and checkboxes and turns them into fields. Then open `form-fillable.pdf` in
Chrome, type in the boxes and press Ctrl+S. Check the result: it can miss a blank
or add an extra one, and it works best on straight scans.

**Sign:** save your signature once, either from a phone photo of it on white
paper (the background is removed) or as your name in handwriting:

```powershell
quill signature photo.jpg
quill signature --name "Jane Doe"
quill signature logo.ai      # or .pdf / .svg artwork of your signature
```

Then right-click a PDF → Send to → **Sign PDF**, or run `quill sign form.pdf --date`.
It finds the signature line and fills in today's date nearby. The guided
questions offer to sign at the end too. This places a picture of your
signature, which most forms accept. It is not a certificate-based digital
signature.

Add `--flatten` to lock the answers. Run `quill fill -h` for every option.

## Good to know

- Your original PDF is never changed.
- Your details and signature are kept in `C:\Users\<you>\.quill`, as plain text
  that only your Windows account can open. Don't add numbers like your SSN.
- Forms that only work in Adobe Reader (XFA forms) can't be filled.
- Uninstall with `install.ps1 -Uninstall`.

## Security

Quill never connects to the internet, and it never runs anything that comes
from a PDF. It opens untrusted PDFs with [PyMuPDF](https://pymupdf.readthedocs.io/),
so keep that up to date. Found a problem? Please open an issue.

## License

Quill is MIT licensed (see [LICENSE](LICENSE)). It uses PyMuPDF, which is
licensed under AGPL-3.0 (or a commercial license from Artifex). If you
redistribute Quill together with PyMuPDF, PyMuPDF's terms apply to it.
