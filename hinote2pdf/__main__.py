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
    ap.add_argument("--no-background", action="store_true", help="leave out the imported PDF page")
    ap.add_argument("--no-layers", action="store_true", help="do not create toggleable ink layers")
    ap.add_argument("--embed-source", action="store_true", help="attach the original .hinote to the PDF")
    ap.add_argument("--dump-json", action="store_true", help="also write all decoded stroke data as JSON")
    ap.add_argument("--verify", action="store_true",
                    help="compare each page with the preview stored in the note (needs pypdfium2, pillow)")
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
                   embed_source=args.embed_source, weight=args.weight)
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


def _pick_files():
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        print("usage: python -m hinote2pdf FILE.hinote [...]  (see --help)")
        return []
    root = tkinter.Tk()
    root.withdraw()
    files = filedialog.askopenfilenames(title="Choose Huawei Notes files",
                                        filetypes=[("Huawei Notes", "*.hinote"), ("All files", "*.*")])
    root.destroy()
    return list(files)


def _message(text, failures):
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if failures else messagebox.showinfo)("hinote2pdf", text)
        root.destroy()
    except ImportError:
        pass


if __name__ == "__main__":
    sys.exit(run())
