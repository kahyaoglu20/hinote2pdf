"""Parser for Huawei Notes' PENCILENGINE ink files (``files/<pageId>.bin``).

All integers and floats are big-endian.  The file is a tree of blocks:

    "PENCILENGINE" | u32 76 (header size) | 60-byte file header | LAYER block at offset 76
        LAYER  (type 2) payload: u32 0, u32 1000, u32 1000, STROKE blocks..., SHAPES block
        STROKE (type 0) payload: 68-byte attributes | 48-byte style | point list
        SHAPES (type 3) payload: registry linking shape strokes to their source strokes

Every block starts with the same 32 bytes::

    u32 header_len | u32 type | u32 version (0x00010000) | u32 payload_len | 16-byte UUID

followed by ``(header_len - 32) / 4`` type-specific u32 fields, then the payload.
See FORMAT.md for the full field-by-field description.
"""

import struct
from dataclasses import dataclass, field

MAGIC = b"PENCILENGINE"

BLOCK_STROKE = 0
BLOCK_LAYER = 2
BLOCK_SHAPES = 3

# Stroke.phase values (per point)
PHASE_DOWN, PHASE_UP, PHASE_MOVE = 4, 5, 6

# Stroke.path_type: how the points are connected
PATH_CR_SPLINE, PATH_BEZIER, PATH_LINE = 0, 1, 2

# Stroke.shape values
SHAPE_FREEHAND = -1
SHAPE_LINE = 0
SHAPE_RECTANGLE = 7


class FormatError(ValueError):
    pass


@dataclass
class Point:
    x: float          # page units, origin top-left, page width == canvas width (1000)
    y: float
    t_ms: int         # milliseconds since the stroke began
    pressure: float   # 0..1
    azimuth: float    # pen orientation, radians
    tilt: float       # pen tilt
    speed: float      # speed factor for the pen's size; 0.2 (neutral) in every file seen so far
    phase: int        # 4 = pen down, 6 = move, 5 = pen up (shape vertices are all 4)
    index: float      # original sample index; Stroke.start/end are expressed in this space


@dataclass
class Stroke:
    uuid: str
    brush: int            # stored brush id: 1 pen, 2 ballpoint, 3 pencil, 4 marker, 5 highlighter
    rgb: tuple            # 0..1 floats (stored on disk as B, G, R)
    stamp_alpha: float    # alpha of each stamp (stored alongside the opacity)
    opacity: float        # stroke opacity chosen in the app
    size: float           # tip size in page units
    created_ms: int       # unix epoch ms
    start: float          # visible range of the stroke in Point.index space (eraser trims it)
    end: float
    flag: int             # stored boolean; 0 seen on one eraser-split remnant that is still drawn
    shape: int            # -1 freehand, 0 line, 7 rectangle
    t_begin_ms: int
    t_end_ms: int
    path_type: int        # PATH_CR_SPLINE (freehand), PATH_BEZIER, PATH_LINE (shapes)
    points: list = field(default_factory=list)


@dataclass
class Canvas:
    uuid: str
    width: int            # always 1000 so far: coordinate units across the page
    height: int
    strokes: list = field(default_factory=list)
    shapes: list = field(default_factory=list)   # (shape stroke uuid, source stroke uuid)
    warnings: list = field(default_factory=list)


def _block_header(buf, off, end):
    if off + 32 > end:
        raise FormatError("truncated block header at 0x%x" % off)
    hlen, btype, version, plen = struct.unpack_from(">IIII", buf, off)
    if hlen < 32 or off + hlen + plen > end:
        raise FormatError("bad block lengths at 0x%x (header %d, payload %d)" % (off, hlen, plen))
    uuid = buf[off + 16:off + 32].hex()
    extra = struct.unpack_from(">%dI" % ((hlen - 32) // 4), buf, off + 32)
    return hlen, btype, version, plen, uuid, extra


def _parse_stroke(buf, p, uuid, extra):
    attr_len, style_len, points_off, points_len = extra[:4]
    (created, start, end, flag, shape,
     t_begin, t_end) = struct.unpack_from(">QffB3xiQQ", buf, p)
    brush, _, b, g, r, c3, opacity, size = struct.unpack_from(">II6f", buf, p + attr_len)
    q = p + points_off
    path_type, count, stride = struct.unpack_from(">III", buf, q)
    if stride < 36 or 20 + count * stride > points_len:
        raise FormatError("bad point table in stroke %s" % uuid)
    pts = [Point(*struct.unpack_from(">ffIffffIf", buf, q + 20 + i * stride)) for i in range(count)]
    return Stroke(uuid=uuid, brush=brush, rgb=(r, g, b), stamp_alpha=c3, opacity=opacity, size=size,
                  created_ms=created, start=start, end=end, flag=flag, shape=shape,
                  t_begin_ms=t_begin, t_end_ms=t_end, path_type=path_type, points=pts)


def _parse_shapes(buf, p, plen):
    if plen < 8:
        return []        # a page without shapes may store the registry with an empty payload
    _, count = struct.unpack_from(">II", buf, p)
    out, q = [], p + 8
    for _ in range(count):
        if q + 56 > p + plen:
            break
        out.append((buf[q + 16:q + 32].hex(), buf[q + 36:q + 52].hex()))
        q += 56
    return out


def parse(data):
    """Parse the bytes of a PENCILENGINE file into a Canvas."""
    if data[:12] != MAGIC:
        raise FormatError("not a PENCILENGINE file")
    off = struct.unpack_from(">I", data, 12)[0]   # total header size == offset of the layer block
    end = len(data)
    hlen, btype, _, plen, uuid, _ = _block_header(data, off, end)
    if btype != BLOCK_LAYER:
        raise FormatError("expected layer block at 0x%x, got type %d" % (off, btype))
    p = off + hlen
    _, width, height = struct.unpack_from(">III", data, p)
    canvas = Canvas(uuid=uuid, width=width, height=height)
    o, pend = p + 12, p + plen
    while o < pend:
        hlen, btype, _, plen, uuid, extra = _block_header(data, o, pend)
        if btype == BLOCK_STROKE:
            canvas.strokes.append(_parse_stroke(data, o + hlen, uuid, extra))
        elif btype == BLOCK_SHAPES:
            canvas.shapes = _parse_shapes(data, o + hlen, plen)
        else:
            canvas.warnings.append("skipped unknown block type %d at 0x%x" % (btype, o))
        o += hlen + plen
    return canvas


def visible_samples(stroke):
    """Points of the stroke inside its [start, end] range as (x, y, pressure) tuples.

    The range is in Point.index space, so the endpoints may fall between two samples;
    those are linearly interpolated, exactly as an eraser cut would leave them.
    """
    pts = stroke.points
    if not pts:
        return []
    if len(pts) == 1:
        p = pts[0]
        return [(p.x, p.y, p.pressure)]
    a = max(stroke.start, pts[0].index)
    b = min(stroke.end, pts[-1].index)
    if b < a:
        return []

    def lerp(p0, p1, t):
        f = 0.0 if p1.index == p0.index else (t - p0.index) / (p1.index - p0.index)
        return (p0.x + (p1.x - p0.x) * f, p0.y + (p1.y - p0.y) * f,
                p0.pressure + (p1.pressure - p0.pressure) * f)

    out = []
    for p0, p1 in zip(pts, pts[1:]):
        if p1.index < a or p0.index > b:
            continue
        if not out:
            out.append(lerp(p0, p1, a) if p0.index < a else (p0.x, p0.y, p0.pressure))
        if p1.index <= b:
            out.append((p1.x, p1.y, p1.pressure))
        else:
            out.append(lerp(p0, p1, b))
            break
    return out
