"""Compare converted pages with the thumbnails Huawei Notes rendered (needs pypdfium2 + Pillow)."""

import io


def _mse(a, b):
    from PIL import ImageChops, ImageFilter, ImageStat
    # a light blur hides JPEG ringing in the thumbnail, which no vector output can reproduce
    a, b = a.filter(ImageFilter.GaussianBlur(0.8)), b.filter(ImageFilter.GaussianBlur(0.8))
    rms = ImageStat.Stat(ImageChops.difference(a, b)).rms
    return sum(v * v for v in rms) / len(rms)


def verify(note, pdf_path, save_dir=None):
    """Yield (page number, match, error, error of background alone) per page.

    match = 1 - error / background_error: the share of the difference between the bare
    background and the stored thumbnail (i.e. the handwriting) that the PDF reproduces.
    """
    import pypdfium2 as pdfium
    from PIL import Image

    out = pdfium.PdfDocument(pdf_path)
    bg_docs = {}
    for i, page in enumerate(note.pages):
        if not page.thumbnail_file:
            continue
        thumb = Image.open(io.BytesIO(note.read(page.thumbnail_file))).convert("RGB")
        rendered = _render(out[i], thumb.size)

        base = Image.new("RGB", thumb.size, _page_rgb(page))
        bg = note.background_pdf(page)
        if bg:
            member, index = bg
            if member not in bg_docs:
                bg_docs[member] = pdfium.PdfDocument(note.read(member))
            base = _render(bg_docs[member][index], thumb.size)
        if save_dir:
            sheet = Image.new("RGB", (thumb.width, thumb.height * 2 + 8), "white")
            sheet.paste(rendered, (0, 0))
            sheet.paste(thumb, (0, thumb.height + 8))
            sheet.save("%s/page-%d-vs-app.png" % (save_dir, page.number))
        err, base_err = _mse(rendered, thumb), _mse(base, thumb)
        yield page.number, (1.0 - err / base_err) if base_err else 1.0, err, base_err


def _page_rgb(page):
    """Paper colour of the page (ARGB int, -1 = white), so the baseline is the bare paper."""
    v = page.color_argb & 0xFFFFFFFF
    if not v >> 24:
        return (255, 255, 255)
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def _render(pdf_page, size, oversample=4):
    """Render at the thumbnail's scale (page width -> size[0]) with area-averaged antialiasing.

    pdfium rounds the bitmap height to whole pixels, stretching the page slightly; that is
    undone at the oversampled resolution before downsampling.
    """
    from PIL import Image
    w, h = pdf_page.get_size()
    scale = oversample * size[0] / w
    img = pdf_page.render(scale=scale).to_pil().convert("RGB")
    img = img.resize((img.width, max(1, round(h * scale))), Image.BICUBIC)
    img = img.resize((size[0], max(1, round(img.height / oversample))), Image.BOX)
    return _fit(img, size)


def _fit(img, size):
    if img.size == size:
        return img
    from PIL import Image
    canvas = Image.new("RGB", size, "white")
    canvas.paste(img.crop((0, 0, min(size[0], img.width), min(size[1], img.height))), (0, 0))
    return canvas
