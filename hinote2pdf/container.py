"""Reader for the .hinote container (a ZIP archive written by Huawei Notes).

Layout::

    <noteId>.jhinote            gzip'd JSON: note metadata + note-level attachments
    pages/<pageId>.jhinote      gzip'd JSON: one per page (number, ratio, background, ink file)
    files/<pageId>.bin          PENCILENGINE ink for the page (see pencilengine.py)
    files/<pageId>.jpg          thumbnail the app rendered for the page
    files/<id>_pdf.pdf          imported PDF used as page background (attachType 3)
    files/<id>.JPG              note cover image (attachType 5)
    custom_md.jhinote           gzip'd JSON: SHA-256 of every file's content and name
"""

import gzip
import hashlib
import json
import posixpath
import zipfile
from dataclasses import dataclass, field

from . import pencilengine

ATTACH_PAGE_INK = 0
ATTACH_PDF = 3
ATTACH_COVER = 5


@dataclass
class Page:
    id: str
    number: int
    ratio: float              # height / width
    orientation: int
    color_argb: int           # -1 == opaque white
    template: str             # "base1" == blank paper
    bkg_attachment_id: str
    bkg_index: int            # page index inside the background PDF
    ink_file: str             # zip member name of the PENCILENGINE file, or None
    thumbnail_file: str
    elements: list
    raw: dict


@dataclass
class Note:
    path: str
    id: str
    title: str
    pages: list
    attachments: dict          # attachment id -> zip member name
    attachment_types: dict     # attachment id -> attachType
    raw: dict
    zf: zipfile.ZipFile
    warnings: list = field(default_factory=list)

    def read(self, member):
        return self.zf.read(member)

    def canvas(self, page):
        if not page.ink_file:
            return None
        return pencilengine.parse(self.read(page.ink_file))

    def background_pdf(self, page):
        """(zip member of the background PDF, page index) or None."""
        member = self.attachments.get(page.bkg_attachment_id)
        if member and self.attachment_types.get(page.bkg_attachment_id) == ATTACH_PDF:
            return member, page.bkg_index
        return None

    def close(self):
        self.zf.close()


def _json(zf, name):
    data = zf.read(name)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


def _member_for(zf_names, device_path):
    """Map an on-device path (/data/data/com.huawei.hinote/files/...) to its zip member."""
    if not device_path:
        return None
    base = posixpath.basename(device_path)
    cand = "files/" + base
    return cand if cand in zf_names else None


def open_hinote(path):
    zf = zipfile.ZipFile(path)
    names = set(zf.namelist())
    warnings = []

    note_json_name = next((n for n in names if "/" not in n and n.endswith(".jhinote")
                           and n != "custom_md.jhinote"), None)
    if note_json_name is None:
        raise ValueError("%s: no note metadata (.jhinote) found" % path)
    raw = _json(zf, note_json_name)
    content = raw.get("customNoteContent", {})

    attachments, attach_types = {}, {}
    for att in content.get("attachment", []):
        member = _member_for(names, att.get("filePath"))
        if member:
            attachments[att["id"]] = member
            attach_types[att["id"]] = att.get("attachType")

    pages = []
    for name in sorted(n for n in names if n.startswith("pages/") and n.endswith(".jhinote")):
        pj = _json(zf, name)
        pc = pj.get("customNotePageContent", {})
        ink = None
        for att in pc.get("attachment", []):
            if att.get("attachType") == ATTACH_PAGE_INK and not att.get("isDelete"):
                ink = _member_for(names, att.get("filePath"))
        if ink is None and "files/%s.bin" % pc.get("id") in names:
            ink = "files/%s.bin" % pc.get("id")
        if pc.get("isDelete"):
            continue
        pages.append(Page(
            id=pc.get("id"), number=int(pc.get("pageNumber", len(pages) + 1)),
            ratio=float(pc.get("pageRatio") or content.get("pageRatio") or 0),
            orientation=int(pc.get("pageOrientation", content.get("pageOrientation", 0))),
            color_argb=int(pc.get("pageColor", -1)),
            template=pc.get("background", ""),
            bkg_attachment_id=pc.get("bkgAttachmentId"),
            bkg_index=int(pc.get("bkgAttachmentIndex", 0)),
            ink_file=ink,
            thumbnail_file=_member_for(names, pc.get("thumbnail")),
            elements=pc.get("pageElement", []),
            raw=pc))
    pages.sort(key=lambda p: p.number)

    _verify_hashes(zf, names, raw, warnings)
    return Note(path=path, id=content.get("id"), title=content.get("noteTitle", ""),
                pages=pages, attachments=attachments, attachment_types=attach_types,
                raw=raw, zf=zf, warnings=warnings)


def _verify_hashes(zf, names, raw, warnings):
    """custom_md.jhinote stores sha256(content) for every file; report mismatches."""
    if "custom_md.jhinote" not in names:
        return
    try:
        md = _json(zf, "custom_md.jhinote")
    except (ValueError, OSError):
        warnings.append("custom_md.jhinote is unreadable")
        return
    expected = {e.get("fileNameMdStr"): e.get("fileMdStr") for e in md.get("customMdContents", [])}
    for name in names:
        if not name.startswith("files/") or name.endswith("/"):
            continue
        key = hashlib.sha256(posixpath.basename(name).encode("utf-8")).hexdigest()
        if key in expected and hashlib.sha256(zf.read(name)).hexdigest() != expected[key]:
            warnings.append("checksum mismatch for %s (file may be corrupted)" % name)
