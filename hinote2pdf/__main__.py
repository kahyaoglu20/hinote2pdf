"""Command line / drag-and-drop entry point:  python -m hinote2pdf FILE.hinote [...]"""

import argparse
import dataclasses
import json
import os
import sys

from . import __version__
from .container import open_hinote
from .pdfout import Options, convert


def _inputs(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.lower().endswith(".hinote"):
                        yield os.path.join(root, f)
        else:
            yield p


def _out_path(src, out, many):
    base = os.path.splitext(os.path.basename(src))[0] + ".pdf"
    if out is None:
        return os.path.join(os.path.dirname(os.path.abspath(src)), base)
    if many or os.path.isdir(out) or out.endswith(("/", "\\")):
        os.makedirs(out, exist_ok=True)
        return os.path.join(out, base)
    return out


def dump_json(note, path):
    pages = []
    for page in note.pages:
        canvas = note.canvas(page)
        pages.append({
            "number": page.number, "id": page.id, "ratio": page.ratio,
            "background_pdf": note.background_pdf(page),
            "canvas": None if canvas is None else {
                "width": canvas.width, "height": canvas.height, "shapes": canvas.shapes,
                "strokes": [dataclasses.asdict(s) for s in canvas.strokes]},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"title": note.title, "id": note.id, "pages": pages}, f, ensure_ascii=False)


def run(argv=None):
    ap = argparse.ArgumentParser(prog="hinote2pdf", description=(
        "Convert Huawei Notes .hinote files into resolution-independent vector PDFs."))
    ap.add_argument("inputs", nargs="*", help=".hinote files or folders (none: open a file picker)")
    ap.add_argument("-o", "--output", help="output .pdf (single input) or folder")
    ap.add_argument("--annotations", action="store_true",
                    help="write strokes as editable PDF ink annotations instead of page content")
    ap.add_argument("--polyline", action="store_true",
                    help="join the recorded samples with straight segments instead of a smooth curve")
    ap.add_argument("--weight", type=float, default=1.0, metavar="X",
                    help="scale every tip by X (default 1 = true stroke geometry; screens and "
                         "thumbnails round tiny tips up to whole pixels, so ~1.3 matches their look)")
    ap.add_argument("--no-background", action="store_true",
                    help="ink only on a white page: leave out the imported PDF page and the paper colour")
    ap.add_argument("--quality", choices=("lossless", "hq"), default="lossless",
                    help="lossless (default): exact stroke geometry; hq: high-quality lossy, still vector, "
                         "about 4x smaller files with no visible difference")
    ap.add_argument("--no-layers", action="store_true", help="do not create toggleable ink layers")
    ap.add_argument("--embed-source", action="store_true", help="attach the original .hinote to the PDF")
    ap.add_argument("--dump-json", action="store_true", help="also write all decoded stroke data as JSON")
    ap.add_argument("--verify", action="store_true",
                    help="compare each page with the preview stored in the note (needs pypdfium2, pillow)")
    ap.add_argument("--ask", action="store_true",
                    help="choose background, quality and extras in a small window (used by hinote2pdf.bat)")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    gui = not args.inputs
    if gui:
        args.inputs = _pick_files()
        if not args.inputs:
            return 0
    srcs = list(_inputs(args.inputs))
    opts = Options(annotations=args.annotations, smooth=not args.polyline,
                   background=not args.no_background, layers=not args.no_layers,
                   embed_source=args.embed_source, weight=args.weight, quality=args.quality)
    if (gui or args.ask) and srcs:
        opts = _ask_options(opts, len(srcs))
        if opts is None:
            print("Cancelled.")
            return 0
    report, failures = [], 0
    for src in srcs:
        dst = _out_path(src, args.output, len(srcs) > 1)
        try:
            note = open_hinote(src)
            warnings = convert(note, dst, opts)
            line = "%s -> %s (%d page%s)" % (src, dst, len(note.pages), "" if len(note.pages) == 1 else "s")
            report.append(line)
            print(line)
            for w in warnings:
                print("  warning:", w)
            if args.dump_json:
                dump_json(note, os.path.splitext(dst)[0] + ".strokes.json")
            if args.verify:
                from .verify import verify
                for n, match, err, base in verify(note, dst):
                    print("  page %d: reproduces %.1f%% of the note's stored preview "
                          "(error %.1f vs %.1f for the background alone)" % (n, 100 * match, err, base))
            note.close()
        except Exception as e:  # keep going with the other files
            failures += 1
            report.append("%s: FAILED - %s" % (src, e))
            print("%s: FAILED - %s" % (src, e), file=sys.stderr)
    if gui:
        _message("\n".join(report) or "Nothing converted.", failures)
    return 1 if failures else 0


def _sharp_windows():
    """Ask Windows for real pixels, so the small windows are not blurry on high-DPI screens."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def _pick_files():
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        print("usage: python -m hinote2pdf FILE.hinote [...]  (see --help)")
        return []
    _sharp_windows()
    root = tkinter.Tk()
    root.withdraw()
    files = filedialog.askopenfilenames(title="Choose Huawei Notes files",
                                        filetypes=[("Huawei Notes", "*.hinote"), ("All files", "*.*")])
    root.destroy()
    return list(files)


def _ask_options(opts, count):
    """Small window to pick background, quality and extras; None if cancelled."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return opts
    _sharp_windows()
    root = tk.Tk()
    root.title("hinote2pdf")
    root.resizable(False, False)
    bg = tk.StringVar(root, "with" if opts.background else "without")
    quality = tk.StringVar(root, opts.quality)
    ann = tk.BooleanVar(root, opts.annotations)
    bold = tk.BooleanVar(root, opts.weight > 1.0)
    done = {}

    def finish(ok):
        done["ok"] = ok
        root.destroy()

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="Convert %d note%s to PDF" % (count, "" if count == 1 else "s"),
              font=("TkDefaultFont", 11, "bold")).grid(sticky="w", pady=(0, 10))
    groups = (
        ("Background", bg, (("with", "With background: imported PDF or book page, paper colour"),
                            ("without", "Without background: ink only, on a white page"))),
        ("Quality", quality, (("lossless", "Lossless vector: exact stroke geometry, largest files"),
                              ("hq", "HQ lossy vector: about 4x smaller, no visible difference"))),
    )
    for title, var, choices in groups:
        box = ttk.LabelFrame(frame, text=title, padding=(10, 6))
        box.grid(sticky="ew", pady=4)
        for value, text in choices:
            ttk.Radiobutton(box, text=text, variable=var, value=value).grid(sticky="w", pady=1)
    box = ttk.LabelFrame(frame, text="Extras", padding=(10, 6))
    box.grid(sticky="ew", pady=4)
    ttk.Checkbutton(box, text="Editable ink (PDF annotations you can move or delete)",
                    variable=ann).grid(sticky="w", pady=1)
    ttk.Checkbutton(box, text="Bolder thin pens, as they look on the tablet screen",
                    variable=bold).grid(sticky="w", pady=1)
    buttons = ttk.Frame(frame)
    buttons.grid(sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="Cancel", command=lambda: finish(False)).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Convert", command=lambda: finish(True), default="active").grid(row=0, column=1)
    root.bind("<Return>", lambda e: finish(True))
    root.bind("<Escape>", lambda e: finish(False))
    root.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    if not done.get("ok"):
        return None
    return dataclasses.replace(opts, background=bg.get() == "with", quality=quality.get(),
                               annotations=ann.get(),
                               weight=(opts.weight if opts.weight > 1.0 else 1.3) if bold.get() else 1.0)


def _message(text, failures):
    try:
        import tkinter
        from tkinter import messagebox
        _sharp_windows()
        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if failures else messagebox.showinfo)("hinote2pdf", text)
        root.destroy()
    except ImportError:
        pass


if __name__ == "__main__":
    sys.exit(run())
