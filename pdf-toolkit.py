"""
PDF Toolkit - Merge, Compress, Split & Reorder - standalone desktop tool.

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

    def __init__(self, parent, on_click=None, height=190):
        super().__init__(parent)
        self.on_click = on_click
        self._photo_refs = []
        self._load_token = 0

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
                if f not in doc_cache:
                    doc_cache[f] = fitz.open(f)
                page = doc_cache[f][idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(self.THUMB_ZOOM, self.THUMB_ZOOM))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
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
        for i, img in enumerate(images):
            photo = ImageTk.PhotoImage(img)
            self._photo_refs.append(photo)
            cell = ttk.Frame(self.inner)
            cell.pack(side="left", padx=4, pady=4)
            lbl = tk.Label(cell, image=photo, relief="solid", bd=1,
                            cursor="hand2" if self.on_click else "")
            lbl.pack()
            ttk.Label(cell, text=str(i + 1)).pack()
            if self.on_click:
                lbl.bind("<Button-1>", lambda e, idx=i: self.on_click(idx))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))


class PageOrderDialog(tk.Toplevel):
    def __init__(self, parent, files, existing_order=None, on_apply=None):
        super().__init__(parent)
        self.title("Edit Page Order")
        self.geometry("560x660")
        self.minsize(460, 520)
        self.on_apply = on_apply
        self._original_files = list(files)
        self.entries = list(existing_order) if existing_order else default_page_entries(files)
        self._build_ui()
        self.transient(parent)
        self.grab_set()

    def _build_ui(self):
        ttk.Label(self, text="Reorder or remove individual pages, then Apply.\n"
                              "This only affects the Merge step. Click a thumbnail below to "
                              "jump to it in the list.",
                  justify="left").pack(padx=10, pady=(10, 4), anchor="w")

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        self.listbox = tk.Listbox(frame, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, command=self.listbox.yview)
        scroll.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(frame)
        btns.pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="Move Up", command=lambda: self._move(-1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Down", command=lambda: self._move(1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove Selected", command=self._remove_selected).pack(fill="x", pady=(12, 2))
        ttk.Button(btns, text="Reset to Default", command=self._reset).pack(fill="x", pady=2)

        preview_frame = ttk.LabelFrame(self, text="Preview")
        preview_frame.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        self.preview = ThumbnailStrip(preview_frame, on_click=self._select_index, height=150)
        self.preview.pack(fill="both", expand=True, padx=6, pady=6)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        ttk.Button(bottom, text="Apply", command=self._apply).pack(side="right", padx=4)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")

        self._refresh()

    def _select_index(self, idx):
        self.listbox.selection_clear(0, "end")
        if 0 <= idx < self.listbox.size():
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

    def _refresh(self):
        self.listbox.delete(0, "end")
        counts = {}
        for f, _ in self.entries:
            if f not in counts:
                try:
                    d = fitz.open(f)
                    counts[f] = d.page_count
                    d.close()
                except Exception:
                    counts[f] = "?"
        for i, (f, idx) in enumerate(self.entries, start=1):
            self.listbox.insert("end", f"{i:02d}. {os.path.basename(f)} — page {idx + 1} of {counts.get(f, '?')}")
        self.preview.load_entries(self.entries)

    def _move(self, direction):
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
        self._refresh()
        for idx in new_sel:
            self.listbox.selection_set(idx)

    def _remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            del self.entries[i]
        self._refresh()

    def _reset(self):
        self.entries = default_page_entries(self._original_files)
        self._refresh()

    def _apply(self):
        if not self.entries:
            messagebox.showwarning("No pages", "At least one page must remain.", parent=self)
            return
        if self.on_apply:
            self.on_apply(list(self.entries))
        self.destroy()


class PDFToolkitApp:
    def __init__(self, root):
        self.root = root
        root.title("PDF Toolkit - Merge, Compress & Split")
        root.geometry("700x620")
        root.minsize(600, 520)

        self.files = []
        self.custom_page_order = None  # None = default natural order (old whole-file behavior)
        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.merge_var = tk.BooleanVar(value=True)
        self.compress_var = tk.BooleanVar(value=True)
        self.preset_var = tk.StringVar(value="Medium (recommended)")
        self.merged_name_var = tk.StringVar(value="merged.pdf")

        self.split_files = []
        self.split_output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.split_mode = tk.StringVar(value="each_page")
        self.split_n = tk.IntVar(value=5)
        self.split_range_spec = tk.StringVar(value="")

        self.reorder_file = None
        self.reorder_entries = []

        self._build_ui()

    # -- UI construction --------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        tab1 = ttk.Frame(self.notebook)
        tab2 = ttk.Frame(self.notebook)
        tab3 = ttk.Frame(self.notebook)
        self.notebook.add(tab1, text="Merge & Compress")
        self.notebook.add(tab2, text="Split")
        self.notebook.add(tab3, text="Reorder")

        self._build_merge_tab(tab1, pad)
        self._build_split_tab(tab2, pad)
        self._build_reorder_tab(tab3, pad)

    def _build_merge_tab(self, parent, pad):
        # File list
        list_frame = ttk.LabelFrame(parent, text="PDF files (order matters for Merge)")
        list_frame.pack(fill="both", expand=True, **pad)

        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(list_frame, command=self.listbox.yview)
        scroll.pack(side="left", fill="y", pady=8)
        self.listbox.config(yscrollcommand=scroll.set)

        btn_col = ttk.Frame(list_frame)
        btn_col.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(btn_col, text="Add Files...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Remove Selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Clear All", command=self.clear_all).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Move Up", command=lambda: self.move(-1)).pack(fill="x", pady=(12, 2))
        ttk.Button(btn_col, text="Move Down", command=lambda: self.move(1)).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Edit Page Order...", command=self.open_page_order_dialog).pack(fill="x", pady=(12, 2))

        # Options
        opt_frame = ttk.LabelFrame(parent, text="Options")
        opt_frame.pack(fill="x", **pad)

        ttk.Checkbutton(opt_frame, text="Merge", variable=self.merge_var).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(opt_frame, text="Compress", variable=self.compress_var).grid(row=0, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(opt_frame, text="Compression level:").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(opt_frame, textvariable=self.preset_var, state="readonly",
                     values=list(COMPRESSION_PRESETS.keys()), width=22).grid(row=1, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(opt_frame, text="Merged file name:").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(opt_frame, textvariable=self.merged_name_var, width=24).grid(row=2, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(opt_frame, text="Output folder:").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        out_row = ttk.Frame(opt_frame)
        out_row.grid(row=3, column=1, columnspan=2, sticky="we", padx=8, pady=4)
        ttk.Entry(out_row, textvariable=self.output_dir, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse...", command=self.pick_output_dir).pack(side="left", padx=(6, 0))

        # Run
        run_row = ttk.Frame(parent)
        run_row.pack(fill="x", **pad)
        self.run_btn = ttk.Button(run_row, text="Run", command=self.run_clicked)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        # Log
        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_split_tab(self, parent, pad):
        list_frame = ttk.LabelFrame(parent, text="PDF files to split (each one is split independently)")
        list_frame.pack(fill="both", expand=True, **pad)

        self.split_listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        self.split_listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll = ttk.Scrollbar(list_frame, command=self.split_listbox.yview)
        scroll.pack(side="left", fill="y", pady=8)
        self.split_listbox.config(yscrollcommand=scroll.set)

        btn_col = ttk.Frame(list_frame)
        btn_col.pack(side="left", fill="y", padx=8, pady=8)
        ttk.Button(btn_col, text="Add Files...", command=self.add_split_files).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Remove Selected", command=self.remove_split_selected).pack(fill="x", pady=2)
        ttk.Button(btn_col, text="Clear All", command=self.clear_split_all).pack(fill="x", pady=2)

        self.split_listbox.bind("<<ListboxSelect>>", self._on_split_select)

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

        out_frame = ttk.LabelFrame(parent, text="Output")
        out_frame.pack(fill="x", **pad)
        ttk.Label(out_frame, text="Output folder:").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        out_row = ttk.Frame(out_frame)
        out_row.grid(row=0, column=1, sticky="we", padx=8, pady=4)
        ttk.Entry(out_row, textvariable=self.split_output_dir, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse...", command=self.pick_split_output_dir).pack(side="left", padx=(6, 0))

        run_row = ttk.Frame(parent)
        run_row.pack(fill="x", **pad)
        self.split_run_btn = ttk.Button(run_row, text="Split", command=self.split_clicked)
        self.split_run_btn.pack(side="left")
        self.split_progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.split_progress.pack(side="left", fill="x", expand=True, padx=10)

        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.split_log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.split_log_text.pack(fill="both", expand=True, padx=8, pady=8)

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

        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        self.reorder_listbox = tk.Listbox(frame, selectmode=tk.EXTENDED)
        self.reorder_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, command=self.reorder_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.reorder_listbox.config(yscrollcommand=scroll.set)

        btns = ttk.Frame(frame)
        btns.pack(side="left", fill="y", padx=8)
        ttk.Button(btns, text="Move Up", command=lambda: self.move_reorder(-1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Move Down", command=lambda: self.move_reorder(1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove Selected", command=self.remove_reorder_selected).pack(fill="x", pady=(12, 2))
        ttk.Button(btns, text="Reset to Original", command=self.reset_reorder).pack(fill="x", pady=2)

        preview_frame = ttk.LabelFrame(parent, text="Preview (click a thumbnail to jump to it)")
        preview_frame.pack(fill="both", expand=True, **pad)
        self.reorder_preview = ThumbnailStrip(preview_frame, on_click=self._reorder_select_index, height=150)
        self.reorder_preview.pack(fill="both", expand=True, padx=6, pady=6)

        run_row = ttk.Frame(parent)
        run_row.pack(fill="x", **pad)
        self.reorder_save_btn = ttk.Button(run_row, text="Save As...", command=self.save_reorder_clicked)
        self.reorder_save_btn.pack(side="left")
        self.reorder_progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.reorder_progress.pack(side="left", fill="x", expand=True, padx=10)

        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.reorder_log_text = tk.Text(log_frame, height=6, state="disabled", wrap="word")
        self.reorder_log_text.pack(fill="both", expand=True, padx=8, pady=8)

    def log(self, msg):
        def _do():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _do)

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Select PDF files", filetypes=[("PDF files", "*.pdf")])
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", os.path.basename(p))
        if paths:
            self._invalidate_custom_order()

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]
        self._invalidate_custom_order()

    def clear_all(self):
        self.listbox.delete(0, "end")
        self.files = []
        self._invalidate_custom_order()

    def _invalidate_custom_order(self):
        if self.custom_page_order is not None:
            self.custom_page_order = None
            self.log("Note: file list changed - custom page order reset to default "
                      "(use 'Edit Page Order...' again if needed).")

    def open_page_order_dialog(self):
        if not self.files:
            messagebox.showinfo("No files", "Add PDF files first.")
            return
        PageOrderDialog(self.root, self.files, existing_order=self.custom_page_order,
                         on_apply=self._set_custom_order)

    def _set_custom_order(self, entries):
        self.custom_page_order = entries
        self.log(f"Custom page order applied: {len(entries)} page(s) in the new order.")

    def move(self, direction):
        # single-item reorder only (unambiguous, no multi-select edge cases)
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

    def pick_output_dir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.output_dir.set(d)

    # -- split tab: file list -------------------------------------------
    def add_split_files(self):
        paths = filedialog.askopenfilenames(title="Select PDF files", filetypes=[("PDF files", "*.pdf")])
        for p in paths:
            if p not in self.split_files:
                self.split_files.append(p)
                self.split_listbox.insert("end", os.path.basename(p))

    def remove_split_selected(self):
        for i in reversed(self.split_listbox.curselection()):
            self.split_listbox.delete(i)
            del self.split_files[i]
        self.split_preview.clear()

    def clear_split_all(self):
        self.split_listbox.delete(0, "end")
        self.split_files = []
        self.split_preview.clear()

    def _on_split_select(self, event=None):
        sel = self.split_listbox.curselection()
        if len(sel) == 1:
            self.split_preview.load(self.split_files[sel[0]])
        else:
            self.split_preview.clear()

    def pick_split_output_dir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.split_output_dir.set(d)

    def log_split(self, msg):
        def _do():
            self.split_log_text.config(state="normal")
            self.split_log_text.insert("end", msg + "\n")
            self.split_log_text.see("end")
            self.split_log_text.config(state="disabled")
        self.root.after(0, _do)

    def split_clicked(self):
        if not self.split_files:
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

        self.split_run_btn.config(state="disabled")
        self.split_progress.start(12)
        self.split_log_text.config(state="normal")
        self.split_log_text.delete("1.0", "end")
        self.split_log_text.config(state="disabled")

        args = dict(
            files=list(self.split_files),
            output_dir=self.split_output_dir.get().strip(),
            mode=mode,
            n=n,
            range_spec=self.split_range_spec.get().strip(),
            log=self.log_split,
        )
        threading.Thread(target=self._split_worker, kwargs=args, daemon=True).start()

    def _split_worker(self, **kwargs):
        try:
            results = split_batch(**kwargs)
            ok, failed = results["ok"], results["failed"]
            self.log_split(f"\nDone. {len(ok)} output file(s) created, {len(failed)} input file(s) failed.")
            if failed:
                self.log_split("Failed:")
                for name, reason in failed:
                    self.log_split(f"  - {name}: {reason}")
            self.root.after(0, lambda: messagebox.showinfo(
                "Finished",
                f"{len(ok)} output file(s) created in:\n{kwargs['output_dir']}"
                + (f"\n\n{len(failed)} file(s) failed - see log." if failed else "")
            ))
        except Exception as e:
            self.log_split(f"\n[fatal] {e}\n{traceback.format_exc()}")
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, self.split_progress.stop)
            self.root.after(0, lambda: self.split_run_btn.config(state="normal"))

    # -- run ------------------------------------------------------------
    def run_clicked(self):
        if not self.files:
            messagebox.showwarning("No files", "Add at least one PDF file first.")
            return
        if not self.merge_var.get() and not self.compress_var.get():
            messagebox.showwarning("No option selected", "Tick Merge, Compress, or both.")
            return

        self.run_btn.config(state="disabled")
        self.progress.start(12)
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        preset = COMPRESSION_PRESETS[self.preset_var.get()]
        args = dict(
            files=list(self.files),
            output_dir=self.output_dir.get().strip(),
            do_merge=self.merge_var.get(),
            do_compress=self.compress_var.get(),
            quality=preset["quality"],
            max_dim=preset["max_dim"],
            merged_name=self.merged_name_var.get().strip() or "merged.pdf",
            page_entries=self.custom_page_order,
            log=self.log,
        )
        threading.Thread(target=self._run_worker, kwargs=args, daemon=True).start()

    def _run_worker(self, **kwargs):
        try:
            results = process(**kwargs)
            ok, failed = results["ok"], results["failed"]
            self.log(f"\nDone. {len(ok)} output file(s) created, {len(failed)} file(s) failed.")
            if failed:
                self.log("Failed:")
                for name, reason in failed:
                    self.log(f"  - {name}: {reason}")
            self.root.after(0, lambda: messagebox.showinfo(
                "Finished",
                f"{len(ok)} output file(s) created in:\n{kwargs['output_dir']}"
                + (f"\n\n{len(failed)} file(s) failed - see log." if failed else "")
            ))
        except Exception as e:
            self.log(f"\n[fatal] {e}\n{traceback.format_exc()}")
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self.run_btn.config(state="normal"))

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
        self.reorder_entries = default_page_entries([path])
        self.reorder_file_label.config(text=os.path.basename(path))
        self._refresh_reorder_listbox()

    def _refresh_reorder_listbox(self):
        self.reorder_listbox.delete(0, "end")
        for i, (f, idx) in enumerate(self.reorder_entries, start=1):
            self.reorder_listbox.insert("end", f"{i:02d}. Page {idx + 1}")
        self.reorder_preview.load_entries(self.reorder_entries)

    def _reorder_select_index(self, idx):
        self.reorder_listbox.selection_clear(0, "end")
        if 0 <= idx < self.reorder_listbox.size():
            self.reorder_listbox.selection_set(idx)
            self.reorder_listbox.see(idx)

    def move_reorder(self, direction):
        sel = list(self.reorder_listbox.curselection())
        if not sel:
            return
        order = sel if direction < 0 else list(reversed(sel))
        new_sel = []
        for i in order:
            j = i + direction
            if 0 <= j < len(self.reorder_entries):
                self.reorder_entries[i], self.reorder_entries[j] = self.reorder_entries[j], self.reorder_entries[i]
                new_sel.append(j)
            else:
                new_sel.append(i)
        self._refresh_reorder_listbox()
        for idx in new_sel:
            self.reorder_listbox.selection_set(idx)

    def remove_reorder_selected(self):
        sel = list(self.reorder_listbox.curselection())
        if not sel:
            return
        if len(sel) >= len(self.reorder_entries):
            messagebox.showwarning("Cannot remove all pages", "At least one page must remain.")
            return
        for i in reversed(sel):
            del self.reorder_entries[i]
        self._refresh_reorder_listbox()

    def reset_reorder(self):
        if not self.reorder_file:
            return
        self.reorder_entries = default_page_entries([self.reorder_file])
        self._refresh_reorder_listbox()

    def log_reorder(self, msg):
        def _do():
            self.reorder_log_text.config(state="normal")
            self.reorder_log_text.insert("end", msg + "\n")
            self.reorder_log_text.see("end")
            self.reorder_log_text.config(state="disabled")
        self.root.after(0, _do)

    def save_reorder_clicked(self):
        if not self.reorder_file or not self.reorder_entries:
            messagebox.showwarning("No file", "Open a PDF first.")
            return
        base = os.path.splitext(os.path.basename(self.reorder_file))[0]
        suggested = f"{base}_reordered.pdf"
        out_path = filedialog.asksaveasfilename(
            title="Save reordered PDF as",
            defaultextension=".pdf",
            initialfile=suggested,
            filetypes=[("PDF files", "*.pdf")],
        )
        if not out_path:
            return

        self.reorder_save_btn.config(state="disabled")
        self.reorder_progress.start(12)
        self.reorder_log_text.config(state="normal")
        self.reorder_log_text.delete("1.0", "end")
        self.reorder_log_text.config(state="disabled")

        entries = list(self.reorder_entries)
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
