"""Brush model: how each Huawei Notes pen type is drawn so the PDF looks like the note.

Every brush is drawn by stamping a round tip along the stroke path:

* Stamp spacing: chord distance 0.1 * size * mean(relative size of both stamps) + 0.04
  page units, i.e. ~10 overlapping stamps per tip diameter.
* Tip box side = size * relative_size page units. The pen and pencil scale it with
  pressure; every other brush uses 1.
* Inside the box sits a hard disc when the brush is opaque and a feathered disc otherwise,
  in three widths (78, 90 and 100 % of a 128-step box) for size <= 5, <= 7 and above.
* Each stamp paints colour with the stored per-stamp alpha (Stroke.stamp_alpha), composited
  "over", so translucent strokes build up toward their opacity and get soft edges.
* The highlighter instead uses flat discs of diameter `size`, painted once per stroke, at
  its stored opacity.

Widths are in page units (page width = 1000).
"""

import math

BRUSH_PEN, BRUSH_BALLPOINT, BRUSH_PENCIL, BRUSH_MARKER, BRUSH_HIGHLIGHTER = 1, 2, 3, 4, 5
BRUSH_NAMES = {1: "pen", 2: "ballpoint", 3: "pencil", 4: "marker", 5: "highlighter",
               11: "painting pencil", 12: "painting pen", 13: "painting ink"}

# stored brush id -> brush type used for drawing (ids 6..10 are drawn as ballpoint)
_STORED_TO_TYPE = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 11: 11, 12: 12, 13: 13}


def brush_type(stored_id):
    return _STORED_TO_TYPE.get(stored_id, BRUSH_BALLPOINT)


def relative_size(brush, pressure, speed, size):
    """Pressure response: a sigmoid for pen and pencil, 1 for everything else."""
    if brush not in (BRUSH_PEN, BRUSH_PENCIL):
        return 1.0
    r = 1.07 / (1.0 + math.exp(2.3 - 5.0 * pressure)) * math.exp(-0.67 * (speed - 0.2))
    return max(min(r, 1.0), 0.25 / size if size > 0 else 0.0)


def stamp_spacing(size, rel_a, rel_b):
    return 0.1 * size * (rel_a + rel_b) * 0.5 + 0.04


# Radial alpha profiles of the soft tips, sampled at r = 0, 1/64, ..., 1/2 of the tip box side
# (r = 1/2 is the box edge).  "smooth" tips are hard discs; only their radius matters.
SMOOTH_RADIUS = {78: 39 / 128, 90: 45 / 128, 100: 50 / 128}
_ONES = [1.0] * 17
SOFT_PROFILE = {
    78: _ONES + [0.999, 0.974, 0.876, 0.685, 0.425, 0.198, 0.063, 0.008] + [0.0] * 8,
    90: _ONES + [1.0, 1.0, 1.0, 0.995, 0.930, 0.789, 0.590, 0.355, 0.168, 0.053, 0.006] + [0.0] * 5,
    100: _ONES + [1.0] * 6 + [0.993, 0.928, 0.813, 0.642, 0.441, 0.251, 0.117, 0.037, 0.004, 0.0],
}
# pencil tip, averaged around the centre (its grain itself cannot be expressed as a vector path)
PENCIL_PROFILE = [0.075, 0.098, 0.155, 0.213, 0.251, 0.271, 0.281, 0.293, 0.303, 0.324, 0.341, 0.361,
                  0.359, 0.370, 0.370, 0.374, 0.378, 0.377, 0.342, 0.291, 0.227, 0.160, 0.103, 0.069,
                  0.041, 0.028, 0.017, 0.007, 0.002, 0.0, 0.0, 0.0, 0.0]


class Tip:
    """How one stroke is stamped."""

    def __init__(self, kind, radius=None, profile=None):
        self.kind = kind          # "disc" (hard edge), "soft" (radial profile) or "flat" (highlighter)
        self.radius = radius      # disc radius as a fraction of the tip box side
        self.profile = profile    # radial alpha samples for "soft"


def tip_for(brush, size, opacity):
    if brush == BRUSH_HIGHLIGHTER:
        return Tip("flat", radius=0.5)
    if brush == BRUSH_PENCIL:
        return Tip("soft", profile=PENCIL_PROFILE)
    if brush == BRUSH_MARKER:
        return Tip("soft", profile=SOFT_PROFILE[100])
    width = 100 if size > 7 else (90 if size > 5 else 78)
    if opacity > 0.99:
        return Tip("disc", radius=SMOOTH_RADIUS[width])
    return Tip("soft", profile=SOFT_PROFILE[width])


def stamp_alpha(brush, stored_c3, pressure):
    """Per-stamp alpha: the stored colour alpha (pencil also scales it by 0.2 + 2 * pressure)."""
    if brush == BRUSH_PENCIL:
        return min(stored_c3 * (0.2 + 2.0 * pressure), 1.0)
    return stored_c3
