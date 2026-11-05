# Quill

Fill, auto-fill and sign PDF forms on your own computer. No account, no upload.

## Install (Windows)

Needs [Python 3.10+](https://www.python.org/downloads/). In this folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Then:

1. Start menu → **My details for PDF forms**. Save your name, address, phone
   and so on once.
2. Save a signature: `quill signature photo.jpg` (a photo of it on white paper)
   or `quill signature --name "Jane Doe"`.
3. Optional: for the Chrome button, open `chrome://extensions`, turn on
   **Developer mode**, click **Load unpacked** and pick the `chrome` folder.

## Use

- **Right-click a PDF** (Shift+right-click on Windows 11) → **Fill in**,
  **Auto-fill** or **Sign with Quill**.
- **In Chrome**, click the Quill button on a PDF's tab. A panel shows what will
  fill in, with **Auto-fill**, **Auto-fill and sign** and **Answer the rest**.
  No window opens.
- **In a terminal**, run `quill form.pdf`, or `quill -h` for everything else.

Quill fills in what it knows, including today's date, and asks the rest. It
saves `form-filled.pdf` next to the original and opens it in Chrome. Flat or
scanned forms are made fillable first. Questions about other people (a parent,
a spouse, an emergency contact) or office use come last, and you can skip them
as a group. In the terminal, `>>` skips the rest. Answers already on the form
are never changed.

Your details and signature stay in `C:\Users\<you>\.quill` as plain text, so
don't add numbers like your SSN. A signature here is a picture of your
signature, not a certificate-based digital signature.

## Security

Quill never connects to the internet. A web PDF is downloaded by Chrome before
Quill sees it. Quill never runs anything that comes from a PDF. Only the Quill
extension can talk to Quill, and only about PDFs saved on this computer, never
network paths. PDFs are read with [PyMuPDF](https://pymupdf.readthedocs.io/),
so keep it up to date. Found a problem? Please open an issue.

## License

MIT (see [LICENSE](LICENSE)). Quill uses PyMuPDF, which is AGPL-3.0 (or
commercial, from Artifex). If you redistribute the two together, PyMuPDF's
terms apply to it.
