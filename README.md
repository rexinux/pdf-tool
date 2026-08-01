# PDF Toolkit — Setup Guide (Windows)

~~ Built Using Claue ~~
This gives you a double-clickable `PDF Toolkit.exe` — no Python knowledge needed to *use* it.
You only need Python once, to *build* the exe.

## What it does
Four tabs:

**Merge & Compress**
- **Merge**: combine any number of PDFs into one, in the order you list them.
- **Edit Page Order...** *(optional)*: opens a page-by-page list with a thumbnail preview
  so you can reorder or drop individual pages before merging — not just whole files. Drag a
  thumbnail to move it, or click one to jump to it in the list. Leave it alone and merging
  works the normal way (every page of every file, in list order).
- **Compress**: shrink file size (downsamples/re-encodes embedded images + strips redundant
  data). If you select Compress **without** Merge, it compresses **every selected PDF
  separately** — you get one compressed output per input, not just one combined file.
- **Both together**: merges first (using your custom page order if you set one), then
  compresses the merged result.

**Split**
- Add any number of PDFs — each one is split independently, so you can batch several at once.
- Click a file in the list to preview all of its pages as thumbnails before splitting —
  useful for picking where a custom range should cut.
- Three modes:
  - **One PDF per page** — every page becomes its own file.
  - **Every N pages** — chunks of N consecutive pages (last chunk keeps whatever's left over).
  - **Custom page ranges** — type something like `1-3, 5, 7-end`. This is checked against
    each file's own page count, so mixed-length files in the same batch are handled correctly.

**Reorder**
- Open a single PDF, reorder or remove its own pages — drag a thumbnail to a new spot to
  move it, or click a thumbnail to jump to it in the list and use Move Up/Move Down/Remove
  Selected — then Save As a new file. This is for when you just need to fix page order or
  drop a page or two, without merging anything else in.

**Unprotect**
- Add any number of password-protected PDFs, enter the password once, click **Remove
  Password**. The same password is tried on every file in the batch. A file that turns
  out not to need a password is just copied through as-is - no need to sort your files
  first. Wrong password on one file is reported and skipped; it won't stop the rest.
- The password is only ever held in memory for that run - it's never written to the log
  or saved anywhere.

All four tabs: bad/corrupt files in a batch are skipped and reported — they won't stop
the rest.

## Does it break OCR / searchable text?
No. Merge, Split, and the page-copying part of Reorder all copy each page's full internal
structure (PyMuPDF's `insert_pdf`) rather than flattening it into an image — so any
existing invisible OCR text layer travels with the page untouched. Compress only ever
swaps out the embedded *image* object for a smaller one; it never touches the text layer.
Every one of these operations also runs an automatic check — comparing extracted text
character-count before and after — and logs the result, so you get proof rather than my
word for it. Look for a line like:
```
Text/OCR check (merge): 1042 -> 1042 characters - OK, nothing lost.
```
in the log box after any run. If it ever shows a drop, it'll say `[WARNING]` instead of
`OK` — that's your cue to check the output by hand before relying on it.

## Step 1 — Install Python (one-time, only on the build machine)
1. Go to https://www.python.org/downloads/ and download the latest Windows installer.
2. Run it. **Tick "Add python.exe to PATH"** at the bottom of the first screen before clicking Install.

## Step 2 — Install the two dependencies + the packager
Put `requirements.txt` in the same folder as `pdf_toolkit.py`, open **Command Prompt**
(search "cmd" in the Start menu) in that folder, and run:
```
pip install -r requirements.txt pyinstaller
```
That's it — only 2 libraries for the app itself (`pymupdf` for reading/writing PDFs,
`pillow` for image recompression), plus `pyinstaller` which is only needed to build the exe.

## Step 3 — Build the standalone exe
1. Put `pdf_toolkit.py` in a folder, e.g. `C:\PDFToolkit\`.
2. In Command Prompt, navigate there: `cd C:\PDFToolkit`
3. Run:
```
pyinstaller --onefile --windowed --name "PDF Toolkit" pdf_toolkit.py
```
4. Wait for it to finish. Your app is now at:
```
C:\PDFToolkit\dist\PDF Toolkit.exe
```
Copy that one file anywhere you like (Desktop, a USB stick, another PC) — it runs on its own,
no install needed. Expect it to be roughly 40–70 MB (that's the whole Python engine bundled in;
still far lighter than the Electron/npm-based tools you tried).

## Step 4 — Using it
1. Double-click `PDF Toolkit.exe`.

**To merge/compress:**
2. On the **Merge & Compress** tab, click **Add Files** → pick your PDFs (any number).
3. Reorder with **Move Up/Move Down** (whole-file order), or click **Edit Page Order...**
   if you need to reorder or remove individual pages instead.
4. Tick **Merge**, **Compress**, or both.
5. Pick a **Compression level** (Medium is a good default) and an **Output folder**.
6. Click **Run**. Progress and results show in the log box; a confirmation pops up when done.

**To split:**
2. Switch to the **Split** tab, click **Add Files** → pick your PDFs.
3. Click a file in the list to preview its pages.
4. Choose a split mode (one page per file / every N pages / custom ranges) and an
   **Output folder**.
5. Click **Split**.

**To reorder a single PDF:**
2. Switch to the **Reorder** tab, click **Open PDF...** → pick one file.
3. Drag a thumbnail to move it, or click one to jump to it in the list and use
   **Move Up/Move Down/Remove Selected**.
4. Click **Save As...** and choose where to save the result.

**To remove a password:**
2. Switch to the **Unprotect** tab, click **Add Files** → pick your protected PDFs.
3. Type the password (check **Show password** if you want to see what you typed).
4. Pick an **Output folder** and click **Remove Password**.

## Notes
- If Windows SmartScreen warns about an "unrecognized app" the first time you run the exe,
  that's normal for unsigned personal tools — click "More info" → "Run anyway".
- No internet connection is used or required at any point; everything runs locally on your PC.
