"""Write a Note as a vector PDF that reproduces how its handwriting looks in Huawei Notes.

Every stroke becomes PDF vector geometry (never pixels), so it stays exact at any zoom.  The
imported background PDF is embedded untouched as a Form XObject.

Path: freehand strokes follow a uniform Catmull-Rom spline through the samples
(with mirrored end points), shapes the straight lines between their vertices.  The visible
[start, end] range is cut out of that curve in sample-index space, as the eraser left it.

Tips (see brushes.py):
* opaque hard tips (pen, opaque ballpoint): the union of the stamped discs, i.e. the envelope
  of a disc whose diameter follows pressure, written as round-capped constant-width runs.  A
  run is cut wherever the width drifts by more than RADIUS_TOL, so the outline stays within
  1.5 * RADIUS_TOL page units of the exact envelope (about 2 micrometres on A4);
* soft or translucent tips (translucent ballpoint, marker, pencil): the individual stamps.
  Each stamp is a radial shading of the tip's alpha profile, multiplied together inside a
  luminosity soft mask, so the accumulated alpha 1 - prod(1 - a * T(r)) is evaluated exactly,
  per output pixel, at any zoom;
* highlighter: flat discs painted once per stroke - an opaque envelope inside a transparency
  group composited at the stroke's opacity.

Strokes are composited in file order, as in the app.
"""

import datetime
import io
import math
import os
import struct
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject, TextStringObject)

from . import brushes, pencilengine

A4_SHORT, A4_LONG = 595.276, 841.89
RADIUS_TOL = 0.005     # page units (page width = 1000)
# quality presets: (width tolerance in page units, decimals of page-unit coordinates).
# "hq" keeps every stroke a vector path but rounds it: its outline stays within ~10 um of the
# exact one on A4 (a 600 dpi printer dot is 42 um), which roughly halves the file again.
QUALITY = {"lossless": (RADIUS_TOL, 4), "hq": (0.03, 2)}
STAMPS_PER_MASK = 12   # stamps per soft mask; small masks keep zooming fast in viewers
KAPPA = 0.5522847498


@dataclass
class Options:
    annotations: bool = False    # strokes as editable /Ink annotations instead of page content
    smooth: bool = True          # Catmull-Rom curve through the samples (False: straight joins)
    background: bool = True      # imported PDF page and paper colour behind the ink (False: ink on white)
    layers: bool = True          # put ink and highlighter on toggleable optional-content layers
    embed_source: bool = False   # attach the original .hinote to the PDF
    weight: float = 1.0          # scale every tip (1.0 = true stroke geometry)
    quality: str = "lossless"    # "lossless" (exact geometry) or "hq" (high-quality lossy, smaller)


def num(v, digits=4):
    s = "%.*f" % (digits, v)
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _name(s):
    return NameObject(s)


def _pdf_date(ms):
    if not ms:
        return None
    t = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc)
    return TextStringObject(t.strftime("D:%Y%m%d%H%M%S+00'00'"))


# ---------------------------------------------------------------------------- curve helpers

def _split_cubic(p0, p1, p2, p3, t):
    def lerp(a, b):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    a, b, c = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
    d, e = lerp(a, b), lerp(b, c)
    f = lerp(d, e)
    return (p0, a, d, f), (f, e, c, p3)


def _sub_cubic(cub, t0, t1):
    if t0 <= 0.0 and t1 >= 1.0:
        return cub
    _, right = _split_cubic(*cub, t0)
    if t1 >= 1.0 or t0 >= 1.0:
        return right
    left, _ = _split_cubic(*right, (t1 - t0) / (1.0 - t0))
    return left


def _cubic_at(cub, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = cub
    s = 1.0 - t
    a, b, c, d = s * s * s, 3 * s * s * t, 3 * s * t * t, t * t * t
    return (a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3)


@dataclass
class Segment:
    cubic: tuple       # ((x, y) start, control 1, control 2, end), page units
    line: bool         # straight segment (controls on the chord)
    p0: float          # pressure at both ends
    p1: float
    v0: float          # speed factor at both ends
    v1: float

    def pressure(self, u):
        return self.p0 + (self.p1 - self.p0) * u

    def speed(self, u):
        return self.v0 + (self.v1 - self.v0) * u


def stroke_path(stroke, smooth):
    """Visible centre line of a stroke: (segments, single point or None).

    A single point (x, y, pressure, speed) is returned for one-sample strokes and for strokes
    whose visible range has zero length; those are drawn as one stamp.
    """
    pts = stroke.points
    if not pts:
        return [], None
    if len(pts) == 1:
        p = pts[0]
        return [], (p.x, p.y, p.pressure, p.speed)
    spline = smooth and stroke.path_type != pencilengine.PATH_LINE
    lo_all, hi_all = stroke.start, stroke.end
    n = len(pts)
    segs, single = [], None
    for i in range(n - 1):
        a, b = pts[i], pts[i + 1]
        if b.index <= a.index:
            continue
        lo, hi = max(lo_all, a.index), min(hi_all, b.index)
        if hi < lo:
            continue
        t0, t1 = (lo - a.index) / (b.index - a.index), (hi - a.index) / (b.index - a.index)
        if spline:
            pp = pts[i - 1] if i > 0 else None
            nn = pts[i + 2] if i + 2 < n else None
            px, py = (pp.x, pp.y) if pp else (2 * a.x - b.x, 2 * a.y - b.y)
            nx, ny = (nn.x, nn.y) if nn else (2 * b.x - a.x, 2 * b.y - a.y)
            c1 = (a.x + (b.x - px) / 6.0, a.y + (b.y - py) / 6.0)
            c2 = (b.x - (nx - a.x) / 6.0, b.y - (ny - a.y) / 6.0)
        else:
            c1 = (a.x + (b.x - a.x) / 3.0, a.y + (b.y - a.y) / 3.0)
            c2 = (a.x + 2 * (b.x - a.x) / 3.0, a.y + 2 * (b.y - a.y) / 3.0)
        cub = _sub_cubic(((a.x, a.y), c1, c2, (b.x, b.y)), t0, t1)
        lerp = lambda u, v, t: u + (v - u) * t
        seg = Segment(cub, not spline, lerp(a.pressure, b.pressure, t0), lerp(a.pressure, b.pressure, t1),
                      lerp(a.speed, b.speed, t0), lerp(a.speed, b.speed, t1))
        if t1 > t0:
            segs.append(seg)
        elif single is None:
            single = (cub[0][0], cub[0][1], seg.p0, seg.v0)
    return segs, (single if not segs else None)


# ---------------------------------------------------------------------------- stroke model

class StrokeModel:
    """Everything needed to draw one stroke as a chain of tip stamps."""

    def __init__(self, stroke, opts):
        self.stroke = stroke
        self.brush = brushes.brush_type(stroke.brush)
        self.size = stroke.size * opts.weight
        self.tip = brushes.tip_for(self.brush, stroke.size, stroke.opacity)
        self.segs, self.single = stroke_path(stroke, opts.smooth)
        self.rgb = stroke.rgb
        self.layer = "highlighter" if self.brush == brushes.BRUSH_HIGHLIGHTER else "ink"
        a = stroke.stamp_alpha
        if self.tip.kind == "flat":
            self.mode = "group"            # highlighter: once per stroke
        elif self.tip.kind == "disc" and a >= 0.999:
            self.mode = "envelope"         # opaque hard discs
        else:
            self.mode = "stamps"
        self.alpha = a
        self.tol, self.digits = QUALITY.get(opts.quality, QUALITY["lossless"])

    def fmt(self, v):
        return num(v, self.digits)

    def empty(self):
        return self.size <= 0 or self.alpha <= 0 or (not self.segs and self.single is None)

    def rel(self, p, v):
        return brushes.relative_size(self.brush, p, v, self.stroke.size)

    def tip_side(self, p, v):
        return self.size * self.rel(p, v)

    def diameter(self, p, v):
        return 2.0 * self.tip.radius * self.tip_side(p, v)


def envelope_ops(m, out):
    """Union of the tip discs along the path as round-capped runs (current colour)."""
    if m.single is not None:
        x, y, p, v = m.single
        disc(x, y, m.diameter(p, v) / 2.0, out, m.digits)
        return
    f, tol = m.fmt, m.tol
    cur = None
    for seg in m.segs:
        cub = seg.cubic
        w_at = lambda u: m.diameter(seg.pressure(u), seg.speed(u))
        w0, w1 = w_at(0.0), w_at(1.0)
        chord = math.hypot(cub[3][0] - cub[0][0], cub[3][1] - cub[0][1])
        n = max(1, int(math.ceil(min(abs(w1 - w0) / 2.0, chord) / tol)))
        for k in range(n):
            u0, u1 = k / n, (k + 1) / n
            w = max(w_at(u0), w_at(u1))
            piece = cub if n == 1 else _sub_cubic(cub, u0, u1)
            if cur is None or not (w - tol <= cur <= w + tol):
                if cur is not None:
                    out.append("S")
                out.append("%s w %s %s m" % (f(w), f(piece[0][0]), f(piece[0][1])))
                cur = w
            if seg.line:
                out.append("%s %s l" % (f(piece[3][0]), f(piece[3][1])))
            else:
                out.append("%s %s %s %s %s %s c" % (f(piece[1][0]), f(piece[1][1]), f(piece[2][0]),
                                                     f(piece[2][1]), f(piece[3][0]), f(piece[3][1])))
    if cur is not None:
        out.append("S")


def stamp_centres(m):
    """Stamp placement: a stamp at the start, then each next one where the chord distance
    from the previous stamp reaches brushes.stamp_spacing (found by bisection)."""
    if m.single is not None:
        x, y, p, v = m.single
        return [(x, y, m.tip_side(p, v), p)]
    out, prev = [], None
    min_step = brushes.stamp_spacing(m.size, 0.25 / max(m.stroke.size, 1e-6), 0.25 / max(m.stroke.size, 1e-6)) / 3.0
    for seg in m.segs:
        cub = seg.cubic
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip([_cubic_at(cub, j / 8) for j in range(8)],
                                     [_cubic_at(cub, j / 8) for j in range(1, 9)]))
        steps = max(1, int(math.ceil(length / min_step)))

        def state(u):
            x, y = _cubic_at(cub, u)
            p, v = seg.pressure(u), seg.speed(u)
            return x, y, m.rel(p, v), p

        u_prev = 0.0
        if prev is None:
            prev = state(0.0)
            out.append(prev)
        for j in range(1, steps + 1):
            u = j / steps
            s = state(u)

            def gap(st):
                return math.hypot(st[0] - prev[0], st[1] - prev[1]) - brushes.stamp_spacing(m.size, prev[2], st[2])

            if gap(s) >= 0:
                lo, hi = u_prev, u
                for _ in range(24):
                    mid = (lo + hi) / 2
                    if gap(state(mid)) >= 0:
                        hi = mid
                    else:
                        lo = mid
                prev = state(hi)
                out.append(prev)
            u_prev = u
    return [(x, y, m.size * r, p) for x, y, r, p in out]


def disc(x, y, r, out, digits=4):
    k = KAPPA * r
    f = lambda v: num(v, digits)
    out.append("%s %s m" % (f(x + r), f(y)))
    out.append("%s %s %s %s %s %s c" % (f(x + r), f(y + k), f(x + k), f(y + r), f(x), f(y + r)))
    out.append("%s %s %s %s %s %s c" % (f(x - k), f(y + r), f(x - r), f(y + k), f(x - r), f(y)))
    out.append("%s %s %s %s %s %s c" % (f(x - r), f(y - k), f(x - k), f(y - r), f(x), f(y - r)))
    out.append("%s %s %s %s %s %s c" % (f(x + k), f(y - r), f(x + r), f(y - k), f(x + r), f(y)))
    out.append("h f")


def color_ops(rgb):
    c = " ".join(num(v) for v in rgb)
    return "%s RG %s rg" % (c, c)


def model_bbox(m, pad=0.5):
    xs, ys, half = [], [], 0.0
    if m.single is not None:
        xs, ys = [m.single[0]], [m.single[1]]
        half = m.tip_side(m.single[2], m.single[3]) / 2
    for seg in m.segs:
        for x, y in seg.cubic:
            xs.append(x)
            ys.append(y)
        half = max(half, m.tip_side(seg.p0, seg.v0) / 2, m.tip_side(seg.p1, seg.v1) / 2)
    return min(xs) - half - pad, min(ys) - half - pad, max(xs) + half + pad, max(ys) + half + pad


# ---------------------------------------------------------------------------- PDF objects

class _Builder:
    """Shared PDF objects (cached ExtGStates, shadings, functions)."""

    def __init__(self, writer):
        self.w = writer
        self.cache = {}

    def add(self, obj):
        return self.w._add_object(obj)

    def stream(self, data, extra=None):
        s = DecodedStreamObject()
        s.set_data(data if isinstance(data, bytes) else data.encode("latin-1"))
        for k, v in (extra or {}).items():
            s[_name(k)] = v
        return self.add(s.flate_encode())

    def _cached(self, key, make):
        if key not in self.cache:
            self.cache[key] = make()
        return self.cache[key]

    def alpha_gs(self, a):
        a = round(a, 5)
        return self._cached(("alpha", a), lambda: self.add(DictionaryObject({
            _name("/Type"): _name("/ExtGState"), _name("/CA"): FloatObject(a), _name("/ca"): FloatObject(a)})))

    def multiply_gs(self):
        return self._cached("multiply", lambda: self.add(DictionaryObject({
            _name("/Type"): _name("/ExtGState"), _name("/BM"): _name("/Multiply")})))

    def invert_fn(self):
        return self._cached("invert", lambda: self.add(DictionaryObject({
            _name("/FunctionType"): NumberObject(2), _name("/Domain"): ArrayObject([FloatObject(0), FloatObject(1)]),
            _name("/C0"): ArrayObject([FloatObject(1)]), _name("/C1"): ArrayObject([FloatObject(0)]),
            _name("/N"): NumberObject(1)})))

    def stamp_shading(self, tip, alpha):
        """Radial shading of one stamp in a unit tip box: gray = 1 - alpha * T(r)."""
        alpha = round(alpha, 5)
        if tip.kind == "disc":
            key = ("disc", round(tip.radius, 6), alpha)

            def make():
                g = FloatObject(round(1.0 - alpha, 6))
                fn = DictionaryObject({_name("/FunctionType"): NumberObject(2),
                                       _name("/Domain"): ArrayObject([FloatObject(0), FloatObject(1)]),
                                       _name("/C0"): ArrayObject([g]), _name("/C1"): ArrayObject([g]),
                                       _name("/N"): NumberObject(1)})
                return self._shading(fn, tip.radius)
        else:
            key = ("soft", tuple(tip.profile), alpha)

            def make():
                samples = b"".join(struct.pack(">H", int(round((1.0 - alpha * t) * 65535))) for t in tip.profile)
                fn = self.stream(samples, {"/FunctionType": NumberObject(0),
                                           "/Domain": ArrayObject([FloatObject(0), FloatObject(1)]),
                                           "/Range": ArrayObject([FloatObject(0), FloatObject(1)]),
                                           "/Size": ArrayObject([NumberObject(len(tip.profile))]),
                                           "/BitsPerSample": NumberObject(16)})
                return self._shading(fn, 0.5)
        return self._cached(key, make)

    def _shading(self, fn, radius):
        return self.add(DictionaryObject({
            _name("/ShadingType"): NumberObject(3), _name("/ColorSpace"): _name("/DeviceGray"),
            _name("/Coords"): ArrayObject([FloatObject(v) for v in (0, 0, 0, 0, 0, radius)]),
            _name("/Function"): fn,
            _name("/Extend"): ArrayObject([BooleanObject(True), BooleanObject(False)])}))

    def form(self, ops, bbox, resources=None, group_cs=None):
        extra = {"/Type": _name("/XObject"), "/Subtype": _name("/Form"),
                 "/BBox": ArrayObject([FloatObject(round(v, 4)) for v in bbox])}
        grp = DictionaryObject({_name("/Type"): _name("/Group"), _name("/S"): _name("/Transparency")})
        if group_cs:
            grp[_name("/CS")] = _name(group_cs)
        extra["/Group"] = grp
        if resources is not None:
            extra["/Resources"] = resources
        return self.stream("\n".join(ops), extra)


class _Resources:
    """Names used by one content stream (a page or an annotation appearance)."""

    def __init__(self):
        self.xobjects, self.gstates, self.props = DictionaryObject(), DictionaryObject(), DictionaryObject()
        self.n = 0

    def name(self, prefix):
        self.n += 1
        return "/%s%d" % (prefix, self.n)

    def gs(self, ref, prefix="G"):
        for k, v in self.gstates.items():
            if v == ref:
                return k
        nm = self.name(prefix)
        self.gstates[_name(nm)] = ref
        return nm

    def xobject(self, ref):
        nm = self.name("X")
        self.xobjects[_name(nm)] = ref
        return nm

    def dict(self):
        d = DictionaryObject()
        if self.xobjects:
            d[_name("/XObject")] = self.xobjects
        if self.gstates:
            d[_name("/ExtGState")] = self.gstates
        if self.props:
            d[_name("/Properties")] = self.props
        return d


def stroke_ops(b, res, m):
    """Content-stream operators that paint one stroke in page units (y down)."""
    if m.mode == "envelope":
        out = ["1 J 1 j", color_ops(m.rgb)]
        envelope_ops(m, out)
        return out
    if m.mode == "group":
        body = ["1 J 1 j", color_ops(m.rgb)]
        envelope_ops(m, body)
        xo = b.form(body, model_bbox(m))
        return ["q %s gs %s Do Q" % (res.gs(b.alpha_gs(m.alpha)), res.xobject(xo))]
    # stamps: each chunk is a colour fill through a luminosity mask equal to the product of
    # (1 - a * T) over its stamps.  Painting the chunks one after another multiplies their
    # transparencies, so splitting changes nothing but keeps each mask small to rasterise.
    stamps = stamp_centres(m)
    sh = b.stamp_shading(m.tip, m.alpha)
    mres = DictionaryObject({
        _name("/Shading"): DictionaryObject({_name("/S"): sh}),
        _name("/ExtGState"): DictionaryObject({_name("/M"): b.multiply_gs()})})
    fill = " ".join(num(v) for v in m.rgb)
    out = []
    for i in range(0, len(stamps), STAMPS_PER_MASK):
        chunk = stamps[i:i + STAMPS_PER_MASK]
        x0 = min(x - side / 2 for x, y, side, _ in chunk) - 0.1
        y0 = min(y - side / 2 for x, y, side, _ in chunk) - 0.1
        x1 = max(x + side / 2 for x, y, side, _ in chunk) + 0.1
        y1 = max(y + side / 2 for x, y, side, _ in chunk) + 0.1
        f = m.fmt
        rect = "%s %s %s %s re f" % (f(x0), f(y0), f(x1 - x0), f(y1 - y0))
        mask_ops = ["1 g " + rect, "/M gs"]
        for x, y, side, _ in chunk:
            mask_ops.append("q %s 0 0 %s %s %s cm /S sh Q" % (num(side, max(m.digits, 3)), num(side, max(m.digits, 3)),
                                                            f(x), f(y)))
        group = b.form(mask_ops, (x0, y0, x1, y1), mres, group_cs="/DeviceGray")
        smask = b.add(DictionaryObject({
            _name("/Type"): _name("/ExtGState"),
            _name("/SMask"): DictionaryObject({_name("/Type"): _name("/Mask"), _name("/S"): _name("/Luminosity"),
                                               _name("/G"): group, _name("/TR"): b.invert_fn()})}))
        out.append("q %s gs %s rg %s Q" % (res.gs(smask, "M"), fill, rect))
    return out


_RES_OPS = {b"Do": "/XObject", b"Tf": "/Font", b"gs": "/ExtGState", b"sh": "/Shading",
            b"cs": "/ColorSpace", b"CS": "/ColorSpace"}
_PRUNABLE = ("/XObject", "/Font", "/ExtGState", "/Shading", "/Pattern", "/Properties", "/ColorSpace")


def _used_resources(resources, data, reader, depth=0):
    """The part of a resource dictionary that the content stream `data` actually uses.

    Some PDFs (scanned books, some exporters) give every page one shared dictionary that
    lists the images and fonts of the whole document; copying it for a single page would
    copy the whole book.  Form XObjects that share such a dictionary are pruned the same
    way.  Anything unexpected falls back to the full dictionary, which is always correct.
    """
    from pypdf.generic import ContentStream, StreamObject

    res = resources.get_object()
    if not isinstance(res, DictionaryObject):
        return resources
    try:
        ops = ContentStream(_raw_stream(data), reader).operations
    except Exception:
        return resources
    used = {}
    for operands, op in ops:
        cat = _RES_OPS.get(op)
        if cat and operands and isinstance(operands[0], NameObject):
            used.setdefault(cat, set()).add(operands[0])
        elif op in (b"scn", b"SCN") and operands and isinstance(operands[-1], NameObject):
            used.setdefault("/Pattern", set()).add(operands[-1])
        elif op in (b"BDC", b"DP") and len(operands) > 1 and isinstance(operands[1], NameObject):
            used.setdefault("/Properties", set()).add(operands[1])
        elif op == b"INLINE IMAGE" and isinstance(operands, dict):
            cs = operands.get("settings", {}).get("/CS")
            if isinstance(cs, NameObject):
                used.setdefault("/ColorSpace", set()).add(cs)
    out = DictionaryObject()
    for key, value in res.items():
        sub = value.get_object()
        if key not in _PRUNABLE or not isinstance(sub, DictionaryObject):
            out[key] = value                      # /ProcSet and anything unusual: keep as is
            continue
        kept = DictionaryObject()
        for name in used.get(key, ()):
            if name not in sub:
                continue
            item = sub.raw_get(name)
            obj = item.get_object()
            if (key == "/XObject" and depth < 8 and isinstance(obj, StreamObject)
                    and obj.get("/Subtype") == "/Form" and "/Resources" in obj
                    and obj["/Resources"].get_object() is res):
                # a form that shares this document-wide dictionary: copy it with its own share
                form = _raw_stream(obj.get_data())
                for k, v in obj.items():
                    if k not in ("/Length", "/Filter", "/DecodeParms", "/Resources"):
                        form[k] = v
                form[_name("/Resources")] = _used_resources(obj["/Resources"], obj.get_data(), reader, depth + 1)
                item = form
            kept[name] = item
        if kept:
            out[key] = kept
    return out


def _raw_stream(data):
    s = DecodedStreamObject()
    s.set_data(data)
    return s


def _background_xobject(b, reader, index):
    """Wrap a page of an existing PDF as a Form XObject; returns (ref, cm, width, height)."""
    src = reader.pages[index]
    box = src.cropbox
    x0, y0, w, h = float(box.left), float(box.bottom), float(box.width), float(box.height)
    rot = (src.rotation or 0) % 360
    contents = src.get_contents()
    data = contents.get_data() if contents is not None else b""
    extra = {"/Type": _name("/XObject"), "/Subtype": _name("/Form"),
             "/BBox": ArrayObject([FloatObject(v) for v in (x0, y0, x0 + w, y0 + h)])}
    if "/Resources" in src:
        extra["/Resources"] = _used_resources(src["/Resources"], data, reader).clone(b.w)
    if "/Group" in src:
        extra["/Group"] = src["/Group"].clone(b.w)
    ref = b.stream(data, extra)
    cm = {0: (1, 0, 0, 1, -x0, -y0),
          90: (0, -1, 1, 0, -y0, x0 + w),
          180: (-1, 0, 0, -1, x0 + w, y0 + h),
          270: (0, 1, -1, 0, y0 + h, -x0)}[rot]
    W, H = (w, h) if rot in (0, 180) else (h, w)
    return ref, cm, W, H


def _jpeg_size(data):
    """(width, height) from a JPEG's frame header, or None."""
    if not data or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack_from(">H", data, i + 2)[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack_from(">HH", data, i + 5)
            return (w, h) if w and h else None
        i += 2 + seg
    return None


def _page_ratio(note, page, canvas):
    """Height / width of a page without a background PDF.

    pageRatio is stored as height / width in some notes and as width / height in others,
    so the reading that matches the page preview stored in the note is used; without a
    preview, the one that holds all of the ink.
    """
    ratio = page.ratio
    units = float(canvas.width if canvas and canvas.width else 1000)
    maxy = max((p.y for s in canvas.strokes for p in s.points), default=0) if canvas else 0
    if ratio <= 0:
        return max(math.sqrt(2), (maxy + 20.0) / units)
    readings = (ratio, 1.0 / ratio)
    size = None
    if page.thumbnail_file:
        try:
            size = _jpeg_size(note.read(page.thumbnail_file))
        except (KeyError, OSError, struct.error):
            size = None
    if size:
        shown = size[1] / float(size[0])
        return min(readings, key=lambda r: abs(math.log(r / shown)))
    if maxy > units * ratio * 1.02 and maxy <= units / ratio * 1.02:
        return 1.0 / ratio
    return ratio


def _default_page_size(note, page, canvas):
    ratio = _page_ratio(note, page, canvas)
    width = A4_SHORT if ratio >= 1 else A4_LONG
    return width, width * ratio


def _ranges(nums):
    """[1, 2, 3, 5] -> '1-3, 5'"""
    out, run = [], []
    for n in sorted(nums):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(str(run[0]) if len(run) == 1 else "%d-%d" % (run[0], run[-1]))
        run = [n]
    if run:
        out.append(str(run[0]) if len(run) == 1 else "%d-%d" % (run[0], run[-1]))
    return ", ".join(out)


def _argb_rgb(argb):
    v = argb & 0xFFFFFFFF
    return ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0, (v >> 24) & 255


# ---------------------------------------------------------------------------- driver

def convert(note, out_path, opts=None):
    """Convert an opened Note (container.open_hinote) to a PDF at out_path. Returns warnings."""
    opts = opts or Options()
    warnings = list(note.warnings)
    writer = PdfWriter()
    b = _Builder(writer)
    readers, bg_cache = {}, {}
    seen_brushes = set()
    templates = {}            # paper template -> page numbers that use it

    ocg = {}
    if opts.layers and not opts.annotations:
        for key, label in (("ink", "Handwriting"), ("highlighter", "Highlighter")):
            ocg[key] = b.add(DictionaryObject({_name("/Type"): _name("/OCG"),
                                               _name("/Name"): TextStringObject(label)}))

    for page in note.pages:
        try:
            canvas = note.canvas(page)
        except (pencilengine.FormatError, struct.error) as e:
            warnings.append("page %d: ink unreadable, page written without it (%s)" % (page.number, e))
            canvas = None
        if canvas:
            warnings += ["page %d: %s" % (page.number, w) for w in canvas.warnings]
        if page.elements:
            warnings.append("page %d: %d page element(s) (text boxes/images) are not converted yet"
                            % (page.number, len(page.elements)))

        bg = note.background_pdf(page) if opts.background else None
        if bg:
            member, index = bg
            if member not in readers:
                readers[member] = PdfReader(io.BytesIO(note.read(member)))
            if (member, index) not in bg_cache:
                if index >= len(readers[member].pages):
                    warnings.append("page %d: background page %d missing" % (page.number, index))
                    bg = None
                else:
                    bg_cache[(member, index)] = _background_xobject(b, readers[member], index)
        if bg:
            bg_ref, bg_cm, W, H = bg_cache[(member, index)]
        else:
            W, H = _default_page_size(note, page, canvas)
            if opts.background and page.template not in ("", "base1", None):
                templates.setdefault(page.template, []).append(page.number)

        pdf_page = writer.add_blank_page(width=W, height=H)
        units = float(canvas.width) if canvas and canvas.width else 1000.0
        s = W / units
        res = _Resources()
        ops = []

        r, g, bl, a = _argb_rgb(page.color_argb)
        if opts.background and a and (r, g, bl) != (1.0, 1.0, 1.0):
            ops.append("q %s %s %s rg 0 0 %s %s re f Q" % (num(r), num(g), num(bl), num(W), num(H)))
        if bg:
            ops.append("q %s cm %s Do Q" % (" ".join(num(v) for v in bg_cm), res.xobject(bg_ref)))

        models = []
        for st in (canvas.strokes if canvas else []):
            m = StrokeModel(st, opts)
            if not m.empty():
                models.append(m)
                seen_brushes.add(m.brush)
        to_units = "%s 0 0 %s 0 %s cm" % (num(s), num(-s), num(H))

        if opts.annotations:
            annots = ArrayObject()
            for m in models:
                annots.append(_ink_annotation(b, m, s, H, to_units))
            if annots:
                pdf_page[_name("/Annots")] = annots
        else:
            ops.append("q " + to_units)
            cur_layer = None
            for m in models:
                if ocg and m.layer != cur_layer:
                    if cur_layer is not None:
                        ops.append("EMC")
                    tag = "/OC" + m.layer[:2]
                    res.props[_name(tag)] = ocg[m.layer]
                    ops.append("/OC %s BDC" % tag)
                    cur_layer = m.layer
                ops.extend(stroke_ops(b, res, m))
            if cur_layer is not None:
                ops.append("EMC")
            ops.append("Q")

        pdf_page[_name("/Resources")] = res.dict()
        pdf_page[_name("/Contents")] = b.stream("\n".join(ops))

    for tpl, nums in templates.items():
        warnings.append("%s %s: paper template '%s' (lines/dots) is not drawn; ink and page colour are"
                        % ("page" if len(nums) == 1 else "pages", _ranges(nums), tpl))

    unsupported = seen_brushes - {brushes.BRUSH_PEN, brushes.BRUSH_BALLPOINT, brushes.BRUSH_HIGHLIGHTER,
                                  brushes.BRUSH_MARKER}
    for br in sorted(unsupported):
        warnings.append("%s strokes use a textured brush; drawn with its averaged tip profile"
                        % brushes.BRUSH_NAMES.get(br, "brush %d" % br))

    if ocg:
        refs = ArrayObject([ocg["ink"], ocg["highlighter"]])
        writer._root_object[_name("/OCProperties")] = DictionaryObject({
            _name("/OCGs"): refs,
            _name("/D"): DictionaryObject({_name("/Order"): refs, _name("/ON"): refs,
                                           _name("/Name"): TextStringObject("Huawei Notes ink")}),
        })

    writer.add_metadata({"/Title": note.title or os.path.splitext(os.path.basename(note.path))[0],
                         "/Creator": "hinote2pdf (Huawei Notes .hinote converter)",
                         "/Subject": "Converted from Huawei Notes"})
    if opts.embed_source:
        with open(note.path, "rb") as f:
            writer.add_attachment(os.path.basename(note.path), f.read())
    with open(out_path, "wb") as f:
        writer.write(f)
    return warnings


def _ink_annotation(b, m, s, H, to_units):
    """One /Ink annotation per stroke: visible sample points in /InkList, exact look in /AP."""
    ux0, uy0, ux1, uy1 = model_bbox(m, 1.0)
    rect = [ux0 * s, H - uy1 * s, ux1 * s, H - uy0 * s]
    res = _Resources()
    ap_ops = ["q " + to_units] + stroke_ops(b, res, m) + ["Q"]
    ap = b.stream("\n".join(ap_ops), {
        "/Type": _name("/XObject"), "/Subtype": _name("/Form"),
        "/BBox": ArrayObject([FloatObject(round(v, 4)) for v in rect]),
        "/Resources": res.dict()})
    pts = [(x, y) for x, y, _ in pencilengine.visible_samples(m.stroke)]
    ink = ArrayObject([ArrayObject([FloatObject(round(v, 4)) for x, y in pts for v in (x * s, H - y * s)])])
    st = m.stroke
    nominal = m.diameter(1.0, 0.2) if m.tip.kind != "soft" else m.size
    annot = DictionaryObject({
        _name("/Type"): _name("/Annot"), _name("/Subtype"): _name("/Ink"),
        _name("/Rect"): ArrayObject([FloatObject(round(v, 4)) for v in rect]),
        _name("/InkList"): ink,
        _name("/C"): ArrayObject([FloatObject(round(v, 4)) for v in m.rgb]),
        _name("/BS"): DictionaryObject({_name("/W"): FloatObject(round(nominal * s, 4))}),
        _name("/F"): NumberObject(4),
        _name("/NM"): TextStringObject(st.uuid),
        _name("/T"): TextStringObject("Huawei Notes"),
        _name("/Subj"): TextStringObject(brushes.BRUSH_NAMES.get(m.brush, "brush").capitalize()),
        _name("/AP"): DictionaryObject({_name("/N"): ap}),
    })
    date = _pdf_date(st.created_ms)
    if date:
        annot[_name("/M")] = date
        annot[_name("/CreationDate")] = date
    return b.add(annot)
