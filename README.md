# pdf-merge-compress

# PDF Toolkit — Setup Guide (Windows)

This gives you a double-clickable `PDF Toolkit.exe` — no Python knowledge needed to *use* it.
You only need Python once, to *build* the exe.

## What it does
- **Merge**: combine any number of PDFs into one, in the order you list them.
- **Compress**: shrink file size (downsamples/re-encodes embedded images + strips redundant
  data). If you select Compress **without** Merge, it compresses **every selected PDF
  separately** — you get one compressed output per input, not just one combined file.
- **Both together**: merges first, then compresses the merged result.
- Bad/corrupt files in a batch are skipped and reported — they won't stop the rest.

## Step 1 — Install Python (one-time, only on the build machine)
1. Go to https://www.python.org/downloads/ and download the latest Windows installer.
2. Run it. **Tick "Add python.exe to PATH"** at the bottom of the first screen before clicking Install.

## Step 2 — Install the two dependencies + the packager
Open **Command Prompt** (search "cmd" in the Start menu) and run:
```
pip install pymupdf pillow pyinstaller
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
2. **Add Files** → pick your PDFs (any number).
3. Reorder with **Move Up/Move Down** if you're merging (order = page order in the merged file).
4. Tick **Merge**, **Compress**, or both.
5. Pick a **Compression level** (Medium is a good default).
6. Pick an **Output folder**.
7. Click **Run**. Progress and results are shown in the log box; a confirmation pops up when done.

## Notes
- If Windows SmartScreen warns about an "unrecognized app" the first time you run the exe,
  that's normal for unsigned personal tools — click "More info" → "Run anyway".
- No internet connection is used or required at any point; everything runs locally on your PC.
