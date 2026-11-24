"""Tests for hinote2pdf.  Run:  python -m unittest discover -s tests"""
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pypdf import PdfReader  # noqa: E402

from hinote2pdf import pdfout, pencilengine  # noqa: E402
from hinote2pdf.container import open_hinote  # noqa: E402

SAMPLE = os.path.join(ROOT, "examples", "sample.hinote")


class _Page:
    def __init__(self, ratio, thumbnail_file=None):
        self.ratio, self.thumbnail_file = ratio, thumbnail_file


class _Note:
    def __init__(self, files):
        self.files = files

    def read(self, name):
        return self.files[name]


def _jpeg_header(width, height):
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00" * 3
    return b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0" + struct.pack(">H", 2 + len(sof)) + sof + b"\xff\xd9"


class ConvertSample(unittest.TestCase):
    def test_sample_converts(self):
        note = open_hinote(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            for opts in (pdfout.Options(), pdfout.Options(annotations=True, embed_source=True),
                         pdfout.Options(smooth=False, layers=False, background=False, weight=1.3)):
                out = os.path.join(tmp, "out.pdf")
                pdfout.convert(note, out, opts)
                pdf = PdfReader(out)
                self.assertEqual(len(pdf.pages), 2)
                for page in pdf.pages:            # A4 portrait
                    self.assertAlmostEqual(float(page.mediabox.width), 595.276, places=1)
                    self.assertAlmostEqual(float(page.mediabox.height), 841.89, places=1)
                if opts.background:               # the imported page keeps its text
                    self.assertIn("Fourier", pdf.pages[0].extract_text())
                if opts.annotations:
                    self.assertTrue(len(pdf.pages[0]["/Annots"]) > 50)
        note.close()

    def test_strokes_parsed(self):
        note = open_hinote(SAMPLE)
        counts = [len(note.canvas(p).strokes) for p in note.pages]
        note.close()
        self.assertTrue(all(c > 100 for c in counts), counts)


class InkFile(unittest.TestCase):
    def test_empty_shape_registry(self):
        """A page without shapes may end with a shape registry that has no payload."""
        note = open_hinote(SAMPLE)
        data = bytearray(note.read(note.pages[1].ink_file))
        note.close()
        before = len(pencilengine.parse(bytes(data)).strokes)
        layer = struct.unpack_from(">I", data, 12)[0]
        shapes = len(data) - 48                   # 40-byte header + 8-byte payload
        self.assertEqual(struct.unpack_from(">I", data, shapes + 4)[0], 3)
        struct.pack_into(">I", data, layer + 12, struct.unpack_from(">I", data, layer + 12)[0] - 8)
        struct.pack_into(">I", data, shapes + 12, 0)
        canvas = pencilengine.parse(bytes(data[:-8]))
        self.assertEqual(len(canvas.strokes), before)
        self.assertEqual(canvas.shapes, [])


class PageRatio(unittest.TestCase):
    def test_ratio_read_from_preview(self):
        note = _Note({"t.jpg": _jpeg_header(763, 1080)})
        self.assertAlmostEqual(pdfout._page_ratio(note, _Page(0.70696, "t.jpg"), None), 1 / 0.70696)
        self.assertAlmostEqual(pdfout._page_ratio(note, _Page(1.41450, "t.jpg"), None), 1.41450)
        note = _Note({"t.jpg": _jpeg_header(1080, 763)})
        self.assertAlmostEqual(pdfout._page_ratio(note, _Page(1.41450, "t.jpg"), None), 1 / 1.41450)

    def test_ratio_from_ink_without_preview(self):
        pt = pencilengine.Point(500, 1300, 0, 0.5, 0, 0, 0.2, 4, 0)
        st = pencilengine.Stroke("u", 1, (0, 0, 0), 1, 1, 3, 0, 0, 0, 1, -1, 0, 0, 0, [pt])
        canvas = pencilengine.Canvas("c", 1000, 1000, [st])
        self.assertAlmostEqual(pdfout._page_ratio(_Note({}), _Page(0.70696), canvas), 1 / 0.70696)
        self.assertAlmostEqual(pdfout._page_ratio(_Note({}), _Page(1.41450), canvas), 1.41450)

    def test_ranges(self):
        self.assertEqual(pdfout._ranges([5, 1, 2, 3, 9, 10]), "1-3, 5, 9-10")


if __name__ == "__main__":
    unittest.main()
