"""
PDF Merger & Compressor - standalone desktop tool.

Dependencies (only two, both single-purpose, no external binaries required):
    pip install pymupdf pillow

Build a Windows .exe (run this ON Windows, not on the dev machine):
    pip install pymupdf pillow pyinstaller
    pyinstaller --onefile --windowed --name "PDF Toolkit" pdf_toolkit.py
    -> output exe will be in the dist/ folder

Behavior summary:
  - Merge only            -> combines all selected PDFs (in list order) into ONE output file.
  - Compress only          -> compresses EVERY selected PDF INDIVIDUALLY. N inputs -> N outputs.
                              (This is the case most tools get wrong: compress-without-merge
                              must not silently drop files or only touch the first one.)
  - Merge + Compress       -> merges everything first, then compresses that single merged file.
  - Any number of files (1 or more) is accepted for either mode.
  - One bad/corrupt file never aborts the whole batch; it's skipped and reported.
"""

import os
import sys
import io
import tempfile
import threading
import traceback

import fitz  # PyMuPDF
from PIL import Image

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


def compress_pdf(input_path, output_path, image_quality=65, max_dim=1600, log=None):
    """Downsamples/re-encodes embedded images + structural cleanup. Returns a stats dict."""
    doc = fitz.open(input_path)
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

    doc.save(output_path, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)
    before = os.path.getsize(input_path)
    after = os.path.getsize(output_path)
    pages = doc.page_count
    doc.close()

    return {
        "before_kb": round(before / 1024, 1),
        "after_kb": round(after / 1024, 1),
        "reduction_pct": round((1 - after / before) * 100, 1) if before else 0,
        "pages": pages,
    }


def merge_pdfs(input_paths, output_path):
    merged = fitz.open()
    for p in input_paths:
        src = fitz.open(p)
        merged.insert_pdf(src)
        src.close()
    merged.save(output_path, garbage=4, deflate=True)
    n = merged.page_count
    merged.close()
    return n


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}({i}){ext}"):
        i += 1
    return f"{base}({i}){ext}"


def process(files, output_dir, do_merge, do_compress, quality=65, max_dim=1600,
            merged_name="merged.pdf", log=None):
    """
    Orchestrates the four supported combinations. Returns:
        {"ok": [(kind, output_path, stats_or_None), ...], "failed": [(name, reason), ...]}
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
        merge_target = unique_path(os.path.join(output_dir, merged_name))
        try:
            if do_compress:
                _log(f"Merging {len(valid_files)} file(s)...")
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_merged = os.path.join(tmp, "_tmp_merged.pdf")
                    merge_pdfs(valid_files, tmp_merged)
                    _log("Compressing merged file...")
                    stats = compress_pdf(tmp_merged, merge_target, quality, max_dim, log=_log)
                    _log(f"  -> {os.path.basename(merge_target)}: "
                         f"{stats['before_kb']}KB -> {stats['after_kb']}KB "
                         f"({stats['reduction_pct']}% smaller), {stats['pages']} pages")
                    results["ok"].append(("merge+compress", merge_target, stats))
            else:
                _log(f"Merging {len(valid_files)} file(s)...")
                n = merge_pdfs(valid_files, merge_target)
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

class PDFToolkitApp:
    def __init__(self, root):
        self.root = root
        root.title("PDF Toolkit - Merge & Compress")
        root.geometry("640x560")
        root.minsize(560, 480)

        self.files = []
        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop"))
        self.merge_var = tk.BooleanVar(value=True)
        self.compress_var = tk.BooleanVar(value=True)
        self.preset_var = tk.StringVar(value="Medium (recommended)")
        self.merged_name_var = tk.StringVar(value="merged.pdf")

        self._build_ui()

    # -- UI construction --------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # File list
        list_frame = ttk.LabelFrame(self.root, text="PDF files (order matters for Merge)")
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

        # Options
        opt_frame = ttk.LabelFrame(self.root, text="Options")
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
        run_row = ttk.Frame(self.root)
        run_row.pack(fill="x", **pad)
        self.run_btn = ttk.Button(run_row, text="Run", command=self.run_clicked)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(run_row, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        # Log
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    # -- helpers ------------------------------------------------------------
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

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_all(self):
        self.listbox.delete(0, "end")
        self.files = []

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
