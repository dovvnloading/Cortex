"""Draw the Cortex mark from its geometry and write ``assets/cortex.svg``.

The mark is a single strip: paper on one face, copper on the other. It widens
along its length, makes one half-twist, and lays its wide end over its own
narrow beginning -- a Mobius band that closes like an ouroboros, and whose loop
reads as a speech bubble.

Nothing here is drawn by hand. The strip is the parametric surface

    X(u, s) = ((1 + w cos h) cos u, (1 + w cos h) sin u, w sin h),   w = s a(u)

with ``s`` across the strip in ``[-1, 1]``, ``h = (u + phase) / 2`` (half a
turn of twist per loop), ``u`` running a little past one full turn so the end
overlaps the start, and the half-width ``a(u)`` growing from the tail to the
head. It is rotated into a three-quarter view
and projected orthographically. Each run of ``u`` keeps the colour of the face
the viewer sees there, so the paper turns into copper exactly where the band
goes edge-on.

The SVG carries two drawings and chooses between them with media queries, the
way a hinted typeface swaps outlines at small sizes:

* ``36px`` and below: bolder proportions on a full-bleed plate, so the strip
  survives in a 16px title bar and the plate's edges land on whole pixels;
* above that: the full drawing with a contact shadow and a lit cut edge, plus
  ruled lines on the paper face from ``96px``.

Renderers without CSS show the full drawing only: the small drawing and the
rules are hidden by presentation attributes that the media queries override.

Run ``python tools/cortex_mark.py`` and then ``npm run icons`` from
``frontend/`` to refresh the web icon and ``assets/cortex.ico``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "cortex.svg"

Point = tuple[float, float]
Point3 = tuple[float, float, float]

# Colours follow the app's own palette: the warm plate of the archive cover,
# paper close to the light theme's ground, and the copper accent.
PLATE = ("#2a2421", "#131111")
PAPER = ("#fff6e8", "#f1dcbc", "#d7b088")
COPPER = ("#f6b386", "#e07a48", "#a8481f")
RULE = "#9c6a43"
SHADOW = "#2b0e04"
EDGE_LIGHT = "#ffd9bd"


@dataclass(frozen=True)
class Strip:
    """Geometry of one drawing of the strip on the 256-unit grid."""

    a_tail: float
    a_head: float
    growth: float
    overlap_deg: float
    tilt_deg: float
    spin_deg: float
    phase_deg: float
    scale: float
    centre: Point
    tolerance: float


FULL = Strip(
    a_tail=0.125,
    a_head=0.40,
    growth=1.35,
    overlap_deg=26.0,
    tilt_deg=36.0,
    spin_deg=112.0,
    phase_deg=6.0,
    scale=80.0,
    centre=(128.0, 129.0),
    tolerance=0.08,
)

SMALL = Strip(
    a_tail=0.22,
    a_head=0.44,
    growth=1.1,
    overlap_deg=28.0,
    tilt_deg=32.0,
    spin_deg=112.0,
    phase_deg=6.0,
    scale=96.0,
    centre=(128.0, 129.5),
    tolerance=0.12,
)

SAMPLES = 2400
RULES = 3


class Model:
    """The strip as a surface, rotated and projected onto the icon grid."""

    def __init__(self, strip: Strip) -> None:
        self.strip = strip
        self.span = 2 * math.pi + math.radians(strip.overlap_deg)
        self.phase = math.radians(strip.phase_deg)
        tilt = math.radians(strip.tilt_deg)
        spin = math.radians(strip.spin_deg)
        self._tilt = (math.cos(tilt), math.sin(tilt))
        self._spin = (math.cos(spin), math.sin(spin))
        self._offset = (0.0, 0.0)
        xs, ys = [], []
        for i in range(0, SAMPLES + 1, 8):
            u = self.span * i / SAMPLES
            for s in (-1.0, 1.0):
                x, y = self._project(self.point(u, s))
                xs.append(x)
                ys.append(y)
        # Centre the drawing's bounding box on the requested point.
        cx, cy = strip.centre
        self._offset = (cx - (min(xs) + max(xs)) / 2, cy - (min(ys) + max(ys)) / 2)

    def half_width(self, u: float) -> float:
        t = min(1.0, u / (2 * math.pi))
        return self.strip.a_tail + (self.strip.a_head - self.strip.a_tail) * t**self.strip.growth

    def point(self, u: float, s: float) -> Point3:
        h = (u + self.phase) / 2
        w = s * self.half_width(u)
        r = 1.0 + w * math.cos(h)
        x, y, z = r * math.cos(u), r * math.sin(u), w * math.sin(h)
        c, n = self._spin
        x, y = x * c - y * n, x * n + y * c
        c, n = self._tilt
        y, z = y * c - z * n, y * n + z * c
        return (x, y, z)

    def _project(self, p: Point3) -> Point:
        return (p[0] * self.strip.scale, p[1] * self.strip.scale)

    def screen(self, u: float, s: float) -> Point:
        x, y = self._project(self.point(u, s))
        return (x + self._offset[0], y + self._offset[1])

    def faces_viewer(self, u: float) -> bool:
        """True where the copper face is the one turned toward the viewer."""
        e = 1e-4
        p, pu, ps = self.point(u, 0.0), self.point(u + e, 0.0), self.point(u, e)
        du = (pu[0] - p[0], pu[1] - p[1])
        ds = (ps[0] - p[0], ps[1] - p[1])
        return du[0] * ds[1] - du[1] * ds[0] >= 0


def simplify(points: Sequence[Point], tolerance: float) -> list[Point]:
    """Ramer-Douglas-Peucker: drop points the outline does not need."""
    if len(points) < 3:
        return list(points)
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        x0, y0 = points[i]
        if norm == 0:
            d = math.hypot(x0 - x1, y0 - y1)
        else:
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
        if d > worst:
            worst, index = d, i
    if worst <= tolerance:
        return [points[0], points[-1]]
    left = simplify(points[: index + 1], tolerance)
    return left[:-1] + simplify(points[index:], tolerance)


def path(points: Sequence[Point], *, closed: bool = True) -> str:
    body = "L".join(f"{x:.2f} {y:.2f}" for x, y in points)
    return f"M{body}{'Z' if closed else ''}"


@dataclass(frozen=True)
class Outline:
    paper: str
    copper: str
    rules: str
    cut: tuple[Point, Point]


def outline(strip: Strip) -> Outline:
    model = Model(strip)
    us = [model.span * i / SAMPLES for i in range(SAMPLES + 1)]
    facing = [model.faces_viewer(u) for u in us]
    turns = [i for i in range(1, len(us)) if facing[i] != facing[i - 1]]
    if len(turns) != 1 or facing[0]:
        raise ValueError("expected the paper face first and exactly one turn to copper")
    turn = turns[0]

    def band(start: int, end: int) -> str:
        top = simplify([model.screen(us[i], 1.0) for i in range(start, end + 1)], strip.tolerance)
        bottom = simplify([model.screen(us[i], -1.0) for i in range(end, start - 1, -1)], strip.tolerance)
        return path(top + bottom)

    rules = []
    first, last = int(turn * 0.10), int(turn * 0.94)
    for k in range(1, RULES + 1):
        s = -1 + 2 * k / (RULES + 1)
        line = simplify([model.screen(us[i], s) for i in range(first, last + 1)], strip.tolerance)
        rules.append(path(line, closed=False))
    end = len(us) - 1
    return Outline(
        paper=band(0, turn),
        copper=band(turn, end),
        rules="".join(rules),
        cut=(model.screen(us[end], 1.0), model.screen(us[end], -1.0)),
    )


def gradients() -> str:
    return "".join(
        [
            '<linearGradient id="cx-plate" x1="24" y1="16" x2="232" y2="240" gradientUnits="userSpaceOnUse">'
            f'<stop offset="0" stop-color="{PLATE[0]}"/><stop offset="1" stop-color="{PLATE[1]}"/></linearGradient>',
            '<linearGradient id="cx-paper" x1="52" y1="70" x2="200" y2="120" gradientUnits="userSpaceOnUse">'
            f'<stop offset="0" stop-color="{PAPER[0]}"/><stop offset=".6" stop-color="{PAPER[1]}"/>'
            f'<stop offset="1" stop-color="{PAPER[2]}"/></linearGradient>',
            '<radialGradient id="cx-copper" cx="122" cy="214" r="150" gradientUnits="userSpaceOnUse">'
            f'<stop offset="0" stop-color="{COPPER[0]}"/><stop offset=".5" stop-color="{COPPER[1]}"/>'
            f'<stop offset="1" stop-color="{COPPER[2]}"/></radialGradient>',
            '<filter id="cx-soft" x="-50%" y="-50%" width="200%" height="200%">'
            '<feGaussianBlur stdDeviation="2.2"/></filter>',
        ]
    )


def full_drawing(shape: Outline) -> str:
    (x0, y0), (x1, y1) = shape.cut
    cut = f"M{x0:.2f} {y0:.2f}L{x1:.2f} {y1:.2f}"
    return "".join(
        [
            '<g class="lg">',
            '<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#cx-plate)"/>',
            '<rect x="8.75" y="8.75" width="238.5" height="238.5" rx="55.25" fill="none" '
            'stroke="#fff" stroke-opacity=".06" stroke-width="1.5"/>',
            f'<path d="{shape.paper}" fill="url(#cx-paper)"/>',
            f'<path class="rules" display="none" d="{shape.rules}" fill="none" stroke="{RULE}" '
            'stroke-width=".9" stroke-linecap="round" opacity=".2"/>',
            # The end of the strip lies on its own beginning: a soft contact
            # shadow on the paper, just beyond the cut edge.
            f'<g clip-path="url(#cx-paper-clip)"><path d="{cut}" stroke="{SHADOW}" stroke-width="7" '
            'stroke-linecap="round" opacity=".38" filter="url(#cx-soft)"/></g>',
            f'<path d="{shape.copper}" fill="url(#cx-copper)"/>',
            f'<path d="{cut}" stroke="{EDGE_LIGHT}" stroke-width="1.4" stroke-linecap="round" opacity=".55"/>',
            "</g>",
        ]
    )


def small_drawing(shape: Outline) -> str:
    return "".join(
        [
            '<g class="sm" display="none">',
            '<rect width="256" height="256" rx="58" fill="url(#cx-plate)"/>',
            f'<path d="{shape.paper}" fill="url(#cx-paper)"/>',
            f'<path d="{shape.copper}" fill="url(#cx-copper)"/>',
            "</g>",
        ]
    )


def build() -> str:
    full = outline(FULL)
    small = outline(SMALL)
    style = (
        "<style>"
        ".sm{display:none}"
        "@media (max-width:36px){.sm{display:inline}.lg{display:none}}"
        "@media (min-width:96px){.rules{display:inline}}"
        "</style>"
    )
    return "\n".join(
        [
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256" '
            'role="img" aria-labelledby="cx-title cx-desc">',
            '<title id="cx-title">Cortex</title>',
            '<desc id="cx-desc">One strip, paper on one face and copper on the other, makes a single '
            "half-twist and lays its end over its own beginning.</desc>",
            style,
            f'<defs>{gradients()}<clipPath id="cx-paper-clip"><path d="{full.paper}"/></clipPath></defs>',
            full_drawing(full),
            small_drawing(small),
            "</svg>",
            "",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT, help="where to write the SVG")
    args = parser.parse_args(argv)
    args.output.write_text(build(), encoding="utf-8", newline="\n")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
