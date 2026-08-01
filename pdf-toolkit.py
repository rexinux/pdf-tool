"""
PDF Toolkit - Merge, Compress, Split, Reorder & Unprotect - standalone desktop tool.

Dependencies (only two, both single-purpose, no external binaries required):
    pip install pymupdf pillow

Build a Windows .exe (run this ON Windows, not on the dev machine):
    pip install pymupdf pillow pyinstaller
    pyinstaller --onefile --windowed --name "PDF Toolkit" pdf_toolkit.py
    -> output exe will be in the dist/ folder

Merge & Compress tab:
  - Merge only            -> combines all selected PDFs (in list order) into ONE output file.
  - Compress only          -> compresses EVERY selected PDF INDIVIDUALLY. N inputs -> N outputs.
                              (This is the case most tools get wrong: compress-without-merge
                              must not silently drop files or only touch the first one.)
  - Merge + Compress       -> merges everything first, then compresses that single merged file.
  - "Edit Page Order..." (optional, advanced) - reorder or drop individual pages before
    merging, with a thumbnail preview, instead of only whole-file order. Leave untouched
    and merge behaves exactly as whole-file merge (every page of every file, in list order).
  - Any number of files (1 or more) is accepted for either mode.
  - One bad/corrupt file never aborts the whole batch; it's skipped and reported.

Split tab:
  - Accepts any number of PDFs; each is split independently.
  - Click a file in the list to preview all of its pages as thumbnails before splitting.
  - Three modes: one file per page, every N pages, or custom page ranges
    (e.g. "1-3, 5, 7-end" - evaluated per file against that file's own page count).
  - One bad/corrupt file never aborts the whole batch.

Reorder tab:
  - Open one PDF, reorder or drop its own pages (with a click-to-jump thumbnail preview),
    and save as a new file. Internally this is a single-file merge, so it shares the
    exact same tested code path as the Merge tab's page-order feature.

Unprotect tab:
  - Add any number of password-protected PDFs; the same password is tried on all of them.
  - Files that turn out not to need a password (including owner-password-only files with
    restricted permissions but no open password) are handled too - just copied through,
    no password needed.
  - Wrong password / missing password on a given file is reported per-file, not fatal to
    the batch. The password is only ever held in memory for the run - never logged.

Text/OCR integrity:
  - Merge, Split, and Compress all copy page content structurally (PyMuPDF insert_pdf /
    image-xref replacement) rather than rastering anything, so an existing searchable/OCR
    text layer is never touched. Every operation still runs an automated character-count
    check (source text vs. output text) and logs the result - not just an assumption.
"""

import os
import sys
import io
import tempfile
import threading
import traceback
from functools import partial

import fitz  # PyMuPDF
from PIL import Image, ImageTk

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# Core logic (pure functions, no GUI dependency - independently testable)
# ---------------------------------------------------------------------------

COMPRESSION_PRESETS = {
    "Low (best quality)":   {"quality": 85, "max_dim": 2000},
    "Medium (recommended)": {"quality": 65, "max_dim": 1600},
    "High (smallest size)": {"quality": 40, "max_dim": 1200},
}


def _text_char_count(doc):
    """Total extractable text length across a fitz Document - used as an integrity
    check that a searchable/OCR text layer wasn't lost by an operation."""
    return sum(len(page.get_text()) for page in doc)


def _check_text_preserved(before_chars, after_chars, log, context):
    """Logs a pass/warning line comparing text character counts before vs after an
    operation. insert_pdf-based operations (merge/split) never touch text content,
    only image xrefs are ever rewritten by compress - so a mismatch here would mean
    something unexpected happened, not just normal recompression."""
    if not log:
        return
    if after_chars >= before_chars:
        log(f"  Text/OCR check ({context}): {before_chars} -> {after_chars} characters - OK, nothing lost.")
    else:
        log(f"  [WARNING] Text/OCR check ({context}): {before_chars} -> {after_chars} characters - "
            f"some text content may have been lost. Please verify the output manually.")


def compress_pdf(input_path, output_path, image_quality=65, max_dim=1600, log=None):
    """Downsamples/re-encodes embedded images + structural cleanup. Returns a stats dict.
    Only ever rewrites image xrefs - the text/OCR layer is a separate content stream
    and is never touched, which is verified below rather than just assumed."""
    doc = fitz.open(input_path)
    before_chars = _text_char_count(doc)
    seen_xrefs = set()

    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                base_image = doc.extract_image(xref)
                img_bytes = base_image["image"]
                pil_img = Image.open(io.BytesIO(img_bytes))

                if pil_img.mode in ("RGBA", "P", "LA", "CMYK"):
                    pil_img = pil_img.convert("RGB")

                w, h = pil_img.size
                if max(w, h) > max_dim:
                    scale = max_dim / max(w, h)
                    pil_img = pil_img.resize(
                        (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
                    )

                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=image_quality, optimize=True)
                new_bytes = buf.getvalue()

                if len(new_bytes) < len(img_bytes):
                    page.replace_image(xref, stream=new_bytes)
            except Exception as e:
                if log:
                    log(f"    [warn] skipped one image in {os.path.basename(input_path)}: {e}")
                continue

    after_chars = _text_char_count(doc)
    doc.save(output_path, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)
    before = os.path.getsize(input_path)
    after = os.path.getsize(output_path)
    pages = doc.page_count
    doc.close()

    _check_text_preserved(before_chars, after_chars, log, os.path.basename(input_path))

    return {
        "before_kb": round(before / 1024, 1),
        "after_kb": round(after / 1024, 1),
        "reduction_pct": round((1 - after / before) * 100, 1) if before else 0,
        "pages": pages,
    }


def default_page_entries(files):
    """Builds the natural (file, page_index) order: every page of every file, in list order.
    This reproduces the old whole-file merge behavior when no custom page order is set."""
    entries = []
    for f in files:
        try:
            d = fitz.open(f)
            entries.extend((f, i) for i in range(d.page_count))
            d.close()
        except Exception:
            continue  # unreadable files are already reported elsewhere; skip defensively
    return entries


def merge_pages(page_entries, output_path, log=None):
    """Merges individual pages in the exact given order.
    page_entries: list of (file_path, zero_based_page_index) tuples.
    Opens each distinct source file only once regardless of how many of its pages are used."""
    if not page_entries:
        raise ValueError("No pages to merge.")
    merged = fitz.open()
    doc_cache = {}
    before_chars = 0
    try:
        for file_path, page_idx in page_entries:
            if file_path not in doc_cache:
                doc_cache[file_path] = fitz.open(file_path)
            src_page = doc_cache[file_path][page_idx]
            before_chars += len(src_page.get_text())
            merged.insert_pdf(doc_cache[file_path], from_page=page_idx, to_page=page_idx)
    finally:
        for d in doc_cache.values():
            d.close()
    after_chars = _text_char_count(merged)
    merged.save(output_path, garbage=4, deflate=True)
    n = merged.page_count
    merged.close()
    _check_text_preserved(before_chars, after_chars, log, "merge")
    return n


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}({i}){ext}"):
        i += 1
    return f"{base}({i}){ext}"


def parse_page_ranges(spec, page_count):
    """
    Parses a 1-based, human-typed range spec like '1-3, 5, 7-end' into a list of
    0-based (start, end) inclusive tuples. Raises ValueError with a clear message
    on any malformed or out-of-bounds input - never guesses silently.
    """
    if not spec or not spec.strip():
        raise ValueError("Page range is empty.")
    ranges = []
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ValueError("Page range is empty.")
    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = a.strip(), b.strip()
            try:
                start = int(a) if a else 1
                end = page_count if b.lower() == "end" else int(b)
            except ValueError:
                raise ValueError(f"Could not parse range '{part}'.")
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ValueError(f"Could not parse page '{part}'.")
        if start < 1 or end < 1 or start > page_count or end > page_count:
            raise ValueError(f"Range '{part}' is out of bounds (this file has {page_count} pages).")
        if start > end:
            raise ValueError(f"Range '{part}' has start after end.")
        ranges.append((start - 1, end - 1))
    return ranges


def split_pdf(input_path, output_dir, mode, n=1, range_spec=None, log=None):
    """
    Splits one PDF. mode is one of:
      'each_page'     -> one output file per page
      'every_n'       -> chunks of n consecutive pages each
      'custom_ranges' -> ranges from range_spec, e.g. '1-3, 5, 7-end'
    Returns list of (output_path, page_count_in_that_file).
    """
    def _log(m):
        if log:
            log(m)

    doc = fitz.open(input_path)
    page_count = doc.page_count
    base = os.path.splitext(os.path.basename(input_path))[0]
    created = []
    os.makedirs(output_dir, exist_ok=True)
    total_before, total_after = 0, 0

    def _write_range(start, end, out_name):
        nonlocal total_before, total_after
        out = unique_path(os.path.join(output_dir, out_name))
        part = fitz.open()
        part.insert_pdf(doc, from_page=start, to_page=end)
        total_before += sum(len(doc[p].get_text()) for p in range(start, end + 1))
        total_after += _text_char_count(part)
        part.save(out, garbage=4, deflate=True)
        part.close()
        created.append((out, end - start + 1))

    try:
        if mode == "each_page":
            for i in range(page_count):
                _write_range(i, i, f"{base}_p{i+1:03d}.pdf")

        elif mode == "every_n":
            if n < 1:
                raise ValueError("Pages-per-file must be at least 1.")
            idx, part_num = 0, 1
            while idx < page_count:
                end = min(idx + n - 1, page_count - 1)
                _write_range(idx, end, f"{base}_part{part_num:02d}_p{idx+1}-{end+1}.pdf")
                idx = end + 1
                part_num += 1

        elif mode == "custom_ranges":
            ranges = parse_page_ranges(range_spec, page_count)
            for i, (start, end) in enumerate(ranges, start=1):
                _write_range(start, end, f"{base}_range{i:02d}_p{start+1}-{end+1}.pdf")

        else:
            raise ValueError(f"Unknown split mode: {mode}")
    finally:
        doc.close()

    _check_text_preserved(total_before, total_after, log, os.path.basename(input_path))
    _log(f"  {os.path.basename(input_path)}: {len(created)} file(s) created")
    return created


def split_batch(files, output_dir, mode, n=1, range_spec=None, log=None):
    """Splits any number of PDFs the same way. One bad file is skipped and reported,
    the rest still get processed - same robustness contract as process()."""
    def _log(m):
        if log:
            log(m)

    if not files:
        raise ValueError("No files provided.")
    os.makedirs(output_dir, exist_ok=True)
    results = {"ok": [], "failed": []}

    for f in files:
        try:
            d = fitz.open(f)
            d.close()
        except Exception as e:
            results["failed"].append((os.path.basename(f), f"not a readable PDF: {e}"))
            _log(f"[skip] {os.path.basename(f)} - not a readable PDF")
            continue
        try:
            created = split_pdf(f, output_dir, mode, n=n, range_spec=range_spec, log=_log)
            results["ok"].extend(created)
        except Exception as e:
            results["failed"].append((os.path.basename(f), str(e)))
            _log(f"[error] {os.path.basename(f)}: {e}")

    return results


def remove_password(input_path, output_path, password=None, log=None):
    """Removes password protection / encryption from a single PDF.
    If the file needs a password to open, one must be supplied and correct.
    Files that don't need a password (including owner-password-only files with
    restricted permissions but no open password) are handled the same way,
    without requiring one. Returns {"needed_password": bool, "pages": int}."""
    doc = fitz.open(input_path)
    needed_password = bool(doc.needs_pass)
    if needed_password:
        if not password:
            doc.close()
            raise ValueError("This file is password-protected - a password is required.")
        if doc.authenticate(password) == 0:
            doc.close()
            raise ValueError("Incorrect password.")
    before_chars = _text_char_count(doc)
    doc.save(output_path, encryption=fitz.PDF_ENCRYPT_NONE)
    pages = doc.page_count
    doc.close()

    out_doc = fitz.open(output_path)
    after_chars = _text_char_count(out_doc)
    out_doc.close()
    _check_text_preserved(before_chars, after_chars, log, os.path.basename(input_path))

    return {"needed_password": needed_password, "pages": pages}


def unprotect_batch(files, output_dir, password=None, log=None):
    """Removes password protection from any number of PDFs. The same password (if any)
    is tried on every file. One bad/wrong-password file is skipped and reported, the
    rest still get processed - same robustness contract as the other batch operations."""
    def _log(m):
        if log:
            log(m)

    if not files:
        raise ValueError("No files provided.")
    os.makedirs(output_dir, exist_ok=True)
    results = {"ok": [], "failed": []}

    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        out = unique_path(os.path.join(output_dir, f"{base}_unprotected.pdf"))
        try:
            stats = remove_password(f, out, password=password, log=_log)
            note = "password removed" if stats["needed_password"] else "was not password-protected (copied as-is)"
            _log(f"  {os.path.basename(f)}: {note} -> {os.path.basename(out)}")
            results["ok"].append((out, stats))
        except Exception as e:
            results["failed"].append((os.path.basename(f), str(e)))
            _log(f"[error] {os.path.basename(f)}: {e}")

    return results


def process(files, output_dir, do_merge, do_compress, quality=65, max_dim=1600,
            merged_name="merged.pdf", page_entries=None, log=None):
    """
    Orchestrates the four supported combinations. Returns:
        {"ok": [(kind, output_path, stats_or_None), ...], "failed": [(name, reason), ...]}

    page_entries: optional custom (file_path, page_index) order for the Merge step.
    If None, the natural order (every page of every file, in list order) is used -
    this reproduces the original whole-file merge behavior.
    """
    def _log(msg):
        if log:
            log(msg)

    if not files:
        raise ValueError("No files provided.")
    if not do_merge and not do_compress:
        raise ValueError("Select at least one of Merge or Compress.")

    os.makedirs(output_dir, exist_ok=True)
    results = {"ok": [], "failed": []}

    valid_files = []
    for f in files:
        try:
            d = fitz.open(f)
            d.close()
            valid_files.append(f)
        except Exception as e:
            results["failed"].append((os.path.basename(f), f"not a readable PDF: {e}"))
            _log(f"[skip] {os.path.basename(f)} - not a readable PDF")

    if not valid_files:
        raise ValueError("None of the supplied files are readable PDFs.")

    if do_merge:
        entries = page_entries if page_entries is not None else default_page_entries(valid_files)
        if not entries:
            raise ValueError("No pages available to merge.")
        merge_target = unique_path(os.path.join(output_dir, merged_name))
        try:
            if do_compress:
                _log(f"Merging {len(entries)} page(s)...")
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_merged = os.path.join(tmp, "_tmp_merged.pdf")
                    merge_pages(entries, tmp_merged, log=_log)
                    _log("Compressing merged file...")
                    stats = compress_pdf(tmp_merged, merge_target, quality, max_dim, log=_log)
                    _log(f"  -> {os.path.basename(merge_target)}: "
                         f"{stats['before_kb']}KB -> {stats['after_kb']}KB "
                         f"({stats['reduction_pct']}% smaller), {stats['pages']} pages")
                    results["ok"].append(("merge+compress", merge_target, stats))
            else:
                _log(f"Merging {len(entries)} page(s)...")
                n = merge_pages(entries, merge_target, log=_log)
                _log(f"  -> {os.path.basename(merge_target)}: {n} pages")
                results["ok"].append(("merge", merge_target, None))
        except Exception as e:
            _log(f"[error] merge step failed: {e}")
            results["failed"].append(("merge step", str(e)))
    else:
        # Compress-only, no merge: every input is compressed INDIVIDUALLY.
        _log(f"Compressing {len(valid_files)} file(s) individually (no merge)...")
        for f in valid_files:
            base = os.path.splitext(os.path.basename(f))[0]
            out = unique_path(os.path.join(output_dir, f"{base}_compressed.pdf"))
            try:
                stats = compress_pdf(f, out, quality, max_dim, log=_log)
                _log(f"  -> {os.path.basename(out)}: "
                     f"{stats['before_kb']}KB -> {stats['after_kb']}KB "
                     f"({stats['reduction_pct']}% smaller), {stats['pages']} pages")
                results["ok"].append(("compress", out, stats))
            except Exception as e:
                _log(f"[error] {os.path.basename(f)}: {e}")
                results["failed"].append((os.path.basename(f), str(e)))

    return results


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Page order dialog (opt-in, advanced) - lets the user reorder or drop
# individual pages before a Merge. Doing nothing here = old whole-file
# merge behavior, unchanged.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reusable page-thumbnail strip - horizontally scrollable, click a thumbnail
# to select the matching row in whatever listbox it's paired with. Rendering
# happens in a background thread; PhotoImage objects are only ever created on
# the main thread (Tk image objects aren't safe to build off-thread).
# ---------------------------------------------------------------------------

class ThumbnailStrip(ttk.Frame):
    THUMB_ZOOM = 0.18
    DRAG_THRESHOLD = 5  # pixels of movement before a press counts as a drag, not a click

    def __init__(self, parent, on_click=None, on_reorder=None, height=190):
        super().__init__(parent)
        self.on_click = on_click
        self.on_reorder = on_reorder  # callback(from_index, to_index); enables dragging
        self._photo_refs = []
        self._load_token = 0
        self._image_cache = {}     # (file, page_idx) -> PIL Image; persists across loads to skip re-render
        self._cells = []           # ordered list of the outer cell Frames
        self._labels = []          # ordered list of the image Labels (for drag highlight)
        self._number_labels = []   # ordered list of the page-number caption Labels
        self._drag_idx = None
        self._drag_started = False
        self._press_xy = None
        self._hover_idx = None

        self.canvas = tk.Canvas(self, height=height, bg="#e8e8e8", highlightthickness=0)
        hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=hbar.set)
        self.canvas.pack(side="top", fill="both", expand=True)
        hbar.pack(side="bottom", fill="x")

        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.status_label = ttk.Label(self, text="No file selected.")
        self.status_label.place(relx=0.5, rely=0.5, anchor="center")

    def clear(self, status_text=""):
        self._load_token += 1
        for w in self.inner.winfo_children():
            w.destroy()
        self._photo_refs.clear()
        self._cells.clear()
        self._labels.clear()
        self._number_labels.clear()
        self._drag_idx = None
        self._drag_started = False
        self._hover_idx = None
        self.status_label.config(text=status_text)
        self.status_label.place(relx=0.5, rely=0.5, anchor="center")

    def load(self, file_path):
        """Convenience: preview every page of a single file, in order."""
        self.load_entries(default_page_entries([file_path]) if file_path else [])

    def load_entries(self, entries):
        """Preview an arbitrary (file_path, page_index) list, e.g. a custom merge order."""
        self.clear()
        if not entries:
            self.status_label.config(text="No pages to preview.")
            return
        self.status_label.config(text="Loading preview...")
        token = self._load_token
        threading.Thread(target=self._render_worker, args=(list(entries), token), daemon=True).start()

    def _render_worker(self, entries, token):
        try:
            images = []
            doc_cache = {}
            for f, idx in entries:
                key = (f, idx)
                cached = self._image_cache.get(key)
                if cached is not None:
                    images.append(cached)
                    continue
                if f not in doc_cache:
                    doc_cache[f] = fitz.open(f)
                page = doc_cache[f][idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(self.THUMB_ZOOM, self.THUMB_ZOOM))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                self._image_cache[key] = img
                images.append(img)
            for d in doc_cache.values():
                d.close()
        except Exception as e:
            self.after(0, lambda: self._show_error(str(e), token))
            return
        self.after(0, lambda: self._populate(images, token))

    def _show_error(self, msg, token):
        if token != self._load_token:
            return
        self.status_label.config(text=f"Could not preview: {msg}")

    def _populate(self, images, token):
        if token != self._load_token:
            return  # a newer load() call already superseded this one
        self.status_label.place_forget()
        draggable = self.on_reorder is not None
        for i, img in enumerate(images):
            photo = ImageTk.PhotoImage(img)
            self._photo_refs.append(photo)
            cell = ttk.Frame(self.inner)
            cell.pack(side="left", padx=4, pady=4)
            cursor = "fleur" if draggable else ("hand2" if self.on_click else "")
            lbl = tk.Label(cell, image=photo, relief="solid", bd=1,
                            highlightthickness=3, highlightbackground="#e8e8e8", cursor=cursor)
            lbl.pack()
            num_lbl = ttk.Label(cell, text=str(i + 1))
            num_lbl.pack()
            self._cells.append(cell)
            self._labels.append(lbl)
            self._number_labels.append(num_lbl)
            if draggable:
                lbl.bind("<ButtonPress-1>", lambda e, idx=i: self._on_press(e, idx))
                lbl.bind("<B1-Motion>", self._on_drag_motion)
                lbl.bind("<ButtonRelease-1>", self._on_release)
            elif self.on_click:
                lbl.bind("<Button-1>", lambda e, idx=i: self.on_click(idx))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # -- drag to reorder --------------------------------------------------
    def _on_press(self, event, idx):
        self._drag_idx = idx
        self._drag_started = False
        self._press_xy = (event.x_root, event.y_root)

    def _on_drag_motion(self, event):
        if self._drag_idx is None:
            return
        if not self._drag_started:
            dx = abs(event.x_root - self._press_xy[0])
            dy = abs(event.y_root - self._press_xy[1])
            if dx < self.DRAG_THRESHOLD and dy < self.DRAG_THRESHOLD:
                return
            self._drag_started = True
        target = self._closest_index(event.x_root)
        if target != self._hover_idx:
            if self._hover_idx is not None and self._hover_idx < len(self._labels):
                self._labels[self._hover_idx].configure(highlightbackground="#e8e8e8")
            if target is not None and target != self._drag_idx:
                self._labels[target].configure(highlightbackground="#2a8a4a")
                self._hover_idx = target
            else:
                self._hover_idx = None

    def _on_release(self, event):
        if self._hover_idx is not None and self._hover_idx < len(self._labels):
            self._labels[self._hover_idx].configure(highlightbackground="#e8e8e8")
        if self._drag_started and self._drag_idx is not None:
            target = self._closest_index(event.x_root)
            if target is not None and target != self._drag_idx:
                self.local_reorder(self._drag_idx, target)
                if self.on_reorder:
                    self.on_reorder(self._drag_idx, target)
        elif self._drag_idx is not None and self.on_click:
            self.on_click(self._drag_idx)  # was a click, not a drag
        self._drag_idx = None
        self._drag_started = False
        self._hover_idx = None

    def local_reorder(self, from_idx, to_idx):
        """Repacks already-rendered thumbnails into a new order instantly, without
        re-fetching/re-rendering from disk. Callers still need to update their own
        source-of-truth entries list and text listbox separately."""
        cell = self._cells.pop(from_idx)
        lbl = self._labels.pop(from_idx)
        num_lbl = self._number_labels.pop(from_idx)
        photo = self._photo_refs.pop(from_idx)
        self._cells.insert(to_idx, cell)
        self._labels.insert(to_idx, lbl)
        self._number_labels.insert(to_idx, num_lbl)
        self._photo_refs.insert(to_idx, photo)
        for c in self._cells:
            c.pack_forget()
        for c in self._cells:
            c.pack(side="left", padx=4, pady=4)
        for i, num_lbl in enumerate(self._number_labels, start=1):
            num_lbl.configure(text=str(i))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _closest_index(self, x_root):
        if not self._cells:
            return None
        best_i, best_dist = None, None
        for i, cell in enumerate(self._cells):
            try:
                cx = cell.winfo_rootx() + cell.winfo_width() / 2
            except tk.TclError:
                continue
            dist = abs(x_root - cx)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_i = i
        return best_i


class FileListPanel(ttk.Frame):
    """Reusable Add/Remove/Clear PDF file list, optionally reorderable (Move Up/Down,
    single-selection only - unambiguous, no multi-select edge cases). Shared by the
    Merge, Split, and Unprotect tabs, which previously each hand-rolled this."""

    def __init__(self, parent, on_change=None, reorderable=False):
        super().__init__(parent)
        self.files = []
        self.on_change = on_change

        self.listbox = tk.Listbox(self, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(self, command=self.listbox.yview)
        scroll.pack(side="left", fill="y", pady=8)
        self.listbox.config(yscrollcommand=scroll.set)

        self.button_col = ttk.Frame(self)  # exposed so callers can pack extra buttons (e.g. Edit Page Order)
        self.button_col.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(self.button_col, text="Add Files...", command=self.add).pack(fill="x", pady=2)
        ttk.Button(self.button_col, text="Remove Selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(self.button_col, text="Clear All", command=self.clear).pack(fill="x", pady=2)
        if reorderable:
            ttk.Button(self.button_col, text="Move Up", command=lambda: self.move(-1)).pack(fill="x", pady=(12, 2))
            ttk.Button(self.button_col, text="Move Down", command=lambda: self.move(1)).pack(fill="x", pady=2)

    def _notify(self):
        if self.on_change:
            self.on_change()

    def add(self):
        paths = filedialog.askopenfilenames(title="Select PDF files", filetypes=[("PDF files", "*.pdf")])
        added = False
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", os.path.basename(p))
                added = True
        if added:
            self._notify()

    def remove_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        for i in reversed(sel):
            self.listbox.delete(i)
            del self.files[i]
        self._notify()

    def clear(self):
        self.listbox.delete(0, "end")
        self.files = []
        self._notify()

    def move(self, direction):
        sel = self.listbox.curselection()
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + direction
        if not (0 <= j < len(self.files)):
            return
        self.files[i], self.files[j] = self.files[j], self.files[i]
        text_i, text_j = self.listbox.get(i), self.listbox.get(j)
        lo, hi = min(i, j), max(i, j)
        self.listbox.delete(lo, hi)
        self.listbox.insert(lo, text_j)
        self.listbox.insert(hi, text_i)
        self.listbox.selection_set(j)


class PageOrderEditor(ttk.Frame):
    """Editable, drag-reorderable page list + thumbnail preview. Shared by the Merge
    tab's 'Edit Page Order' dialog and the standalone Reorder tab - previously two
    separate, near-identical implementations."""

    def __init__(self, parent, get_default_entries, label_fmt=None, min_pages=1):
        super().__init__(parent)
        self.entries = []
        self.min_pages = min_pages
        self._get_default_entries = get_default_entries
        self._label_fmt = label_fmt or (
            lambda i, f, idx, total: f"{i:02d}. {os.path.basename(f)} — page {idx + 1} of {total}")
        self._count_cache = {}

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(frame, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, command=self.listbox.yview)
        scroll.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(frame)
        btns.pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="Move Up", command=lambda: self.move(-1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Down", command=lambda: self.move(1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(fill="x", pady=(12, 2))
        ttk.Button(btns, text="Reset", command=self.reset).pack(fill="x", pady=2)

        preview_frame = ttk.LabelFrame(self, text="Preview (click a thumbnail to jump to it; drag to reorder)")
        preview_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.preview = ThumbnailStrip(preview_frame, on_click=self.select_index,
                                       on_reorder=self._thumb_reordered, height=150)
        self.preview.pack(fill="both", expand=True, padx=6, pady=6)

    def set_entries(self, entries):
        self.entries = list(entries)
        self._count_cache.clear()
        self._refresh_text()
        self.preview.load_entries(self.entries)

    def select_index(self, idx):
        self.listbox.selection_clear(0, "end")
        if 0 <= idx < self.listbox.size():
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

    def _page_count(self, f):
        if f not in self._count_cache:
            try:
                d = fitz.open(f)
                self._count_cache[f] = d.page_count
                d.close()
            except Exception:
                self._count_cache[f] = "?"
        return self._count_cache[f]

    def _refresh_text(self):
        self.listbox.delete(0, "end")
        for i, (f, idx) in enumerate(self.entries, start=1):
            self.listbox.insert("end", self._label_fmt(i, f, idx, self._page_count(f)))

    def _thumb_reordered(self, from_idx, to_idx):
        entry = self.entries.pop(from_idx)
        self.entries.insert(to_idx, entry)
        self._refresh_text()  # preview already updated itself instantly (local_reorder)
        self.select_index(to_idx)

    def move(self, direction):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        order = sel if direction < 0 else list(reversed(sel))
        new_sel = []
        for i in order:
            j = i + direction
            if 0 <= j < len(self.entries):
                self.entries[i], self.entries[j] = self.entries[j], self.entries[i]
                new_sel.append(j)
            else:
                new_sel.append(i)
        self._refresh_text()
        self.preview.load_entries(self.entries)
        for idx in new_sel:
            self.listbox.selection_set(idx)

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        if len(self.entries) - len(sel) < self.min_pages:
            messagebox.showwarning("Cannot remove", f"At least {self.min_pages} page(s) must remain.")
            return
        for i in reversed(sel):
            del self.entries[i]
        self._refresh_text()
        self.preview.load_entries(self.entries)

    def reset(self):
        self.entries = list(self._get_default_entries())
        self._count_cache.clear()
        self._refresh_text()
        self.preview.load_entries(self.entries)


class PageOrderDialog(tk.Toplevel):
    """Thin modal wrapper around PageOrderEditor, for the Merge tab's optional
    per-page (rather than per-file) ordering."""

    def __init__(self, parent, files, existing_order=None, on_apply=None):
        super().__init__(parent)
        self.title("Edit Page Order")
        self.geometry("560x660")
        self.minsize(460, 520)
        self.on_apply = on_apply

        ttk.Label(self, text="Reorder or remove individual pages, then Apply.\n"
                              "This only affects the Merge step.",
                  justify="left").pack(padx=10, pady=(10, 4), anchor="w")

        self.editor = PageOrderEditor(self, get_default_entries=lambda: default_page_entries(files))
        self.editor.pack(fill="both", expand=True, padx=10, pady=4)
        self.editor.set_entries(existing_order if existing_order else default_page_entries(files))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        ttk.Button(bottom, text="Apply", command=self._apply).pack(side="right", padx=4)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")

        self.transient(parent)
        self.grab_set()

    def _apply(self):
        if not self.editor.entries:
            messagebox.showwarning("No pages", "At least one page must remain.", parent=self)
            return
        if self.on_apply:
            self.on_apply(list(self.editor.entries))
        self.destroy()


class PDFToolkitApp:
    def __init__(self, root):
        self.root = root
        root.title("PDF Toolkit - Merge, Compress, Split, Reorder & Unprotect")
        root.geometry("700x620")
        root.minsize(600, 520)

        self.custom_page_order = None  # None = default natural order (old whole-file behavior)
        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.merge_var = tk.BooleanVar(value=True)
        self.compress_var = tk.BooleanVar(value=True)
        self.preset_var = tk.StringVar(value="Medium (recommended)")
        self.merged_name_var = tk.StringVar(value="merged.pdf")

        self.split_output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.split_mode = tk.StringVar(value="each_page")
        self.split_n = tk.IntVar(value=5)
        self.split_range_spec = tk.StringVar(value="")

        self.reorder_file = None

        self.unprotect_password = tk.StringVar(value="")
        self.unprotect_show_pw = tk.BooleanVar(value=False)
        self.unprotect_output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))

        self._build_ui()

    # -- generic helpers shared by every tab --------------------------------
    def _log_to(self, widget, msg):
        def _do():
            widget.config(state="normal")
            widget.insert("end", msg + "\n")
            widget.see("end")
            widget.config(state="disabled")
        self.root.after(0, _do)

    def _pick_dir(self, var):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            var.set(d)

    def _start_job(self, btn, progress, log_text):
        btn.config(state="disabled")
        progress.start(12)
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    def _run_batch(self, fn, job_kwargs, log_fn, progress, btn, output_dir, noun="output file", failure_hint=""):
        """Runs one of the *_batch()/process() functions, reports ok/failed counts to the
        log and a popup, then re-enables the UI. Shared by Merge/Split/Unprotect, which
        previously each hand-rolled this exact try/except/finally shape."""
        try:
            results = fn(**job_kwargs)
            ok, failed = results["ok"], results["failed"]
            log_fn(f"\nDone. {len(ok)} {noun}(s) created, {len(failed)} failed.")
            if failed:
                log_fn("Failed:")
                for name, reason in failed:
                    log_fn(f"  - {name}: {reason}")
            msg = f"{len(ok)} {noun}(s) created in:\n{output_dir}"
            if failed:
                msg += f"\n\n{len(failed)} file(s) failed - see log.{failure_hint}"
            self.root.after(0, lambda: messagebox.showinfo("Finished", msg))
        except Exception as e:
            log_fn(f"\n[fatal] {e}\n{traceback.format_exc()}")
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, progress.stop)
            self.root.after(0, lambda: btn.config(state="normal"))

    def _build_labeled_dir_row(self, parent, label, var, pick_cmd, **pad):
        frame = ttk.LabelFrame(parent, text=label)
        frame.pack(fill="x", **pad)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row, textvariable=var, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=pick_cmd).pack(side="left", padx=(6, 0))
        return frame

    def _build_run_row(self, parent, text, command, **pad):
        row = ttk.Frame(parent)
        row.pack(fill="x", **pad)
        btn = ttk.Button(row, text=text, command=command)
        btn.pack(side="left")
        progress = ttk.Progressbar(row, mode="indeterminate")
        progress.pack(side="left", fill="x", expand=True, padx=10)
        return btn, progress

    def _build_log_box(self, parent, height=8, **pad):
        frame = ttk.LabelFrame(parent, text="Log")
        frame.pack(fill="both", expand=True, **pad)
        text = tk.Text(frame, height=height, state="disabled", wrap="word")
        text.pack(fill="both", expand=True, padx=8, pady=8)
        return text

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        tabs = [("Merge & Compress", self._build_merge_tab),
                ("Split", self._build_split_tab),
                ("Reorder", self._build_reorder_tab),
                ("Unprotect", self._build_unprotect_tab)]
        for title, builder in tabs:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            builder(frame, pad)

    def _build_merge_tab(self, parent, pad):
        list_frame = ttk.LabelFrame(parent, text="PDF files (order matters for Merge)")
        list_frame.pack(fill="both", expand=True, **pad)
        self.merge_panel = FileListPanel(list_frame, on_change=self._invalidate_custom_order, reorderable=True)
        self.merge_panel.pack(fill="both", expand=True)
        ttk.Button(self.merge_panel.button_col, text="Edit Page Order...",
                   command=self.open_page_order_dialog).pack(fill="x", pady=(12, 2))

        opt_frame = ttk.LabelFrame(parent, text="Options")
        opt_frame.pack(fill="x", **pad)
        ttk.Checkbutton(opt_frame, text="Merge", variable=self.merge_var).grid(
            row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(opt_frame, text="Compress", variable=self.compress_var).grid(
            row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(opt_frame, text="Compression level:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(opt_frame, textvariable=self.preset_var, state="readonly",
                     values=list(COMPRESSION_PRESETS.keys()), width=22).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(opt_frame, text="Merged file name:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(opt_frame, textvariable=self.merged_name_var, width=24).grid(row=2, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(opt_frame, text="Output folder:").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        out_row = ttk.Frame(opt_frame)
        out_row.grid(row=3, column=1, columnspan=2, sticky="we", padx=8, pady=4)
        ttk.Entry(out_row, textvariable=self.output_dir, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse...",
                   command=partial(self._pick_dir, self.output_dir)).pack(side="left", padx=(6, 0))

        self.run_btn, self.progress = self._build_run_row(parent, "Run", self.run_clicked, **pad)
        self.log_text = self._build_log_box(parent, **pad)
        self.log = partial(self._log_to, self.log_text)

    def _build_split_tab(self, parent, pad):
        list_frame = ttk.LabelFrame(parent, text="PDF files to split (each one is split independently)")
        list_frame.pack(fill="both", expand=True, **pad)
        self.split_panel = FileListPanel(list_frame, on_change=lambda: self.split_preview.clear())
        self.split_panel.pack(fill="both", expand=True)
        self.split_panel.listbox.bind("<<ListboxSelect>>", self._on_split_select)

        preview_frame = ttk.LabelFrame(parent, text="Preview (click a file above to see its pages)")
        preview_frame.pack(fill="both", expand=True, **pad)
        self.split_preview = ThumbnailStrip(preview_frame, height=150)
        self.split_preview.pack(fill="both", expand=True, padx=6, pady=6)

        mode_frame = ttk.LabelFrame(parent, text="Split mode")
        mode_frame.pack(fill="x", **pad)
        ttk.Radiobutton(mode_frame, text="One PDF per page", variable=self.split_mode,
                         value="each_page").grid(row=0, column=0, sticky="w", padx=8, pady=4, columnspan=3)
        ttk.Radiobutton(mode_frame, text="Every N pages:", variable=self.split_mode,
                         value="every_n").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Spinbox(mode_frame, from_=1, to=999, textvariable=self.split_n, width=6).grid(
            row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Radiobutton(mode_frame, text="Custom page ranges:", variable=self.split_mode,
                         value="custom_ranges").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(mode_frame, textvariable=self.split_range_spec, width=28).grid(
            row=2, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(mode_frame, text="e.g. 1-3, 5, 7-end  (applied per file, against that file's own page count)",
                  foreground="#555").grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        self._build_labeled_dir_row(parent, "Output", self.split_output_dir,
                                     partial(self._pick_dir, self.split_output_dir), **pad)

        self.split_run_btn, self.split_progress = self._build_run_row(parent, "Split", self.split_clicked, **pad)
        self.split_log_text = self._build_log_box(parent, **pad)
        self.log_split = partial(self._log_to, self.split_log_text)

    def _build_reorder_tab(self, parent, pad):
        ttk.Label(parent, text="Open a single PDF, reorder or remove its pages, "
                                "then save. Page content is copied as-is - any existing "
                                "searchable/OCR text layer is carried over unchanged "
                                "(verified in the log below).",
                  justify="left", wraplength=620).pack(padx=10, pady=(10, 4), anchor="w")

        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Open PDF...", command=self.open_reorder_file).pack(side="left")
        self.reorder_file_label = ttk.Label(top, text="No file open.")
        self.reorder_file_label.pack(side="left", padx=10)

        self.reorder_editor = PageOrderEditor(
            parent,
            get_default_entries=lambda: default_page_entries([self.reorder_file]) if self.reorder_file else [],
            label_fmt=lambda i, f, idx, total: f"{i:02d}. Page {idx + 1}",
        )
        self.reorder_editor.pack(fill="both", expand=True, padx=10, pady=4)

        self.reorder_save_btn, self.reorder_progress = self._build_run_row(
            parent, "Save As...", self.save_reorder_clicked, **pad)
        self.reorder_log_text = self._build_log_box(parent, height=6, **pad)
        self.log_reorder = partial(self._log_to, self.reorder_log_text)

    def _build_unprotect_tab(self, parent, pad):
        ttk.Label(parent, text="Add password-protected PDFs, enter the password, and get "
                                "back passwordless copies. The same password is tried on "
                                "every file; files that turn out not to be protected are "
                                "just copied through as-is. The password is only held in "
                                "memory for this run - never written to the log or saved.",
                  justify="left", wraplength=620).pack(padx=10, pady=(10, 4), anchor="w")

        list_frame = ttk.LabelFrame(parent, text="Protected PDF files")
        list_frame.pack(fill="both", expand=True, **pad)
        self.unprotect_panel = FileListPanel(list_frame)
        self.unprotect_panel.pack(fill="both", expand=True)

        pw_frame = ttk.LabelFrame(parent, text="Password")
        pw_frame.pack(fill="x", **pad)
        ttk.Label(pw_frame, text="Password:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.unprotect_pw_entry = ttk.Entry(pw_frame, textvariable=self.unprotect_password, show="*", width=28)
        self.unprotect_pw_entry.grid(row=0, column=1, sticky="w", padx=4, pady=6)
        ttk.Checkbutton(pw_frame, text="Show password", variable=self.unprotect_show_pw,
                         command=self._toggle_unprotect_pw_visibility).grid(row=0, column=2, sticky="w", padx=8)

        self._build_labeled_dir_row(parent, "Output", self.unprotect_output_dir,
                                     partial(self._pick_dir, self.unprotect_output_dir), **pad)

        self.unprotect_run_btn, self.unprotect_progress = self._build_run_row(
            parent, "Remove Password", self.unprotect_clicked, **pad)
        self.unprotect_log_text = self._build_log_box(parent, **pad)
        self.log_unprotect = partial(self._log_to, self.unprotect_log_text)

    # -- merge tab ------------------------------------------------------
    def _invalidate_custom_order(self):
        if self.custom_page_order is not None:
            self.custom_page_order = None
            self.log("Note: file list changed - custom page order reset to default "
                      "(use 'Edit Page Order...' again if needed).")

    def open_page_order_dialog(self):
        if not self.merge_panel.files:
            messagebox.showinfo("No files", "Add PDF files first.")
            return
        PageOrderDialog(self.root, self.merge_panel.files, existing_order=self.custom_page_order,
                         on_apply=self._set_custom_order)

    def _set_custom_order(self, entries):
        self.custom_page_order = entries
        self.log(f"Custom page order applied: {len(entries)} page(s) in the new order.")

    def run_clicked(self):
        if not self.merge_panel.files:
            messagebox.showwarning("No files", "Add at least one PDF file first.")
            return
        if not self.merge_var.get() and not self.compress_var.get():
            messagebox.showwarning("No option selected", "Tick Merge, Compress, or both.")
            return
        self._start_job(self.run_btn, self.progress, self.log_text)
        preset = COMPRESSION_PRESETS[self.preset_var.get()]
        out_dir = self.output_dir.get().strip()
        job_kwargs = dict(
            files=list(self.merge_panel.files), output_dir=out_dir,
            do_merge=self.merge_var.get(), do_compress=self.compress_var.get(),
            quality=preset["quality"], max_dim=preset["max_dim"],
            merged_name=self.merged_name_var.get().strip() or "merged.pdf",
            page_entries=self.custom_page_order, log=self.log,
        )
        threading.Thread(
            target=self._run_batch,
            args=(process, job_kwargs, self.log, self.progress, self.run_btn, out_dir),
            daemon=True,
        ).start()

    # -- split tab --------------------------------------------------------
    def _on_split_select(self, event=None):
        sel = self.split_panel.listbox.curselection()
        if len(sel) == 1:
            self.split_preview.load(self.split_panel.files[sel[0]])
        else:
            self.split_preview.clear()

    def split_clicked(self):
        if not self.split_panel.files:
            messagebox.showwarning("No files", "Add at least one PDF file first.")
            return
        mode = self.split_mode.get()
        if mode == "custom_ranges" and not self.split_range_spec.get().strip():
            messagebox.showwarning("No ranges", "Enter a page range, e.g. 1-3, 5, 7-end.")
            return
        try:
            n = int(self.split_n.get())
        except (tk.TclError, ValueError):
            n = 0
        if mode == "every_n" and n < 1:
            messagebox.showwarning("Invalid N", "'Every N pages' must be 1 or more.")
            return
        self._start_job(self.split_run_btn, self.split_progress, self.split_log_text)
        out_dir = self.split_output_dir.get().strip()
        job_kwargs = dict(files=list(self.split_panel.files), output_dir=out_dir, mode=mode, n=n,
                           range_spec=self.split_range_spec.get().strip(), log=self.log_split)
        threading.Thread(
            target=self._run_batch,
            args=(split_batch, job_kwargs, self.log_split, self.split_progress, self.split_run_btn, out_dir),
            daemon=True,
        ).start()

    # -- reorder tab ------------------------------------------------------
    def open_reorder_file(self):
        path = filedialog.askopenfilename(title="Select a PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        try:
            d = fitz.open(path)
            d.close()
        except Exception as e:
            messagebox.showerror("Cannot open file", str(e))
            return
        self.reorder_file = path
        self.reorder_file_label.config(text=os.path.basename(path))
        self.reorder_editor.set_entries(default_page_entries([path]))

    def save_reorder_clicked(self):
        if not self.reorder_file or not self.reorder_editor.entries:
            messagebox.showwarning("No file", "Open a PDF first.")
            return
        base = os.path.splitext(os.path.basename(self.reorder_file))[0]
        out_path = filedialog.asksaveasfilename(
            title="Save reordered PDF as", defaultextension=".pdf",
            initialfile=f"{base}_reordered.pdf", filetypes=[("PDF files", "*.pdf")],
        )
        if not out_path:
            return
        self._start_job(self.reorder_save_btn, self.reorder_progress, self.reorder_log_text)
        entries = list(self.reorder_editor.entries)
        threading.Thread(target=self._save_reorder_worker, args=(entries, out_path), daemon=True).start()

    def _save_reorder_worker(self, entries, out_path):
        try:
            n = merge_pages(entries, out_path, log=self.log_reorder)
            self.log_reorder(f"\nSaved {n} page(s) -> {out_path}")
            self.root.after(0, lambda: messagebox.showinfo("Saved", f"Saved to:\n{out_path}"))
        except Exception as e:
            self.log_reorder(f"\n[fatal] {e}\n{traceback.format_exc()}")
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, self.reorder_progress.stop)
            self.root.after(0, lambda: self.reorder_save_btn.config(state="normal"))

    # -- unprotect tab ------------------------------------------------------
    def _toggle_unprotect_pw_visibility(self):
        self.unprotect_pw_entry.config(show="" if self.unprotect_show_pw.get() else "*")

    def unprotect_clicked(self):
        if not self.unprotect_panel.files:
            messagebox.showwarning("No files", "Add at least one PDF file first.")
            return
        out_dir = self.unprotect_output_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("No output folder", "Choose an output folder.")
            return
        self._start_job(self.unprotect_run_btn, self.unprotect_progress, self.unprotect_log_text)
        job_kwargs = dict(files=list(self.unprotect_panel.files), output_dir=out_dir,
                           password=self.unprotect_password.get() or None, log=self.log_unprotect)
        threading.Thread(
            target=self._run_batch,
            args=(unprotect_batch, job_kwargs, self.log_unprotect, self.unprotect_progress,
                  self.unprotect_run_btn, out_dir),
            kwargs={"noun": "file", "failure_hint": " (often a wrong password)"},
            daemon=True,
        ).start()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = PDFToolkitApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
