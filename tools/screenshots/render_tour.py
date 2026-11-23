"""Render the README tour timelines written by record_tour.mjs into GIFs.

    python tools/screenshots/render_tour.py build/tour --out .github/images

``tools/screenshots/tour.ps1`` records every scene and then runs this.

Each scene folder holds 2x screenshots of distinct UI states plus a
``timeline.json`` of what happens between them. This script plays that
timeline: it holds and cross-fades the screenshots, glides a drawn pointer
between targets with an eased, slightly curved path, shows a ripple where it
clicks, a keycap chip when a shortcut is pressed, and a caption bar under the
window. Frames are rendered from the 2x shots with Lanczos resampling, so text
stays sharp at the output size.

Encoding uses ffmpeg's two-pass GIF palette: one 256-colour palette for the
whole scene (no per-frame palettes, so unchanged pixels stay identical from
frame to frame and the GIF only stores what changed) and no dithering, which
keeps text and flat UI surfaces clean instead of speckled.

ffmpeg is taken from ``--ffmpeg``, then ``imageio-ffmpeg`` if it is installed,
then ``ffmpeg`` on PATH. It must be 4.x or newer (palettegen/paletteuse).
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

STEP_MS = 30  # animation frame length; 33 fps where something moves
LARGE_CHANGE = 0.45  # share of the frame above which a cross-fade is shortened
CAPTION_HEIGHT = 64

BAR_BG = (17, 17, 19)
BAR_LINE = (44, 44, 48)
ACCENT = (230, 112, 62)
TEXT = (236, 233, 228)
MUTED = (120, 118, 114)

FONT_DIR = Path("C:/Windows/Fonts")


def _font(names: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in names:
        path = FONT_DIR / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def ease(t: float) -> float:
    """Cubic ease-in-out."""
    t = min(max(t, 0.0), 1.0)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


MIN_FRAME_MS = 20


def _round_ms(ms: float) -> int:
    # GIF delays are centiseconds, and browsers slow any frame shorter than
    # two of them down to a tenth of a second.
    return max(MIN_FRAME_MS, int(round(ms / 10.0)) * 10)


# --- Pointer and overlays ---------------------------------------------------------

def make_pointer(height: int) -> tuple[Image.Image, tuple[int, int]]:
    """A classic arrow pointer, white with a dark outline and a soft shadow."""
    k = 8  # supersample
    unit = height / 21.0
    shape = [(0, 0), (0, 16.5), (4.1, 12.9), (6.9, 19.4), (9.6, 18.3), (6.9, 11.9), (12.2, 11.9)]
    pad = 6
    w = int(13 * unit) + pad * 2
    h = int(21 * unit) + pad * 2
    big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    pts = [((x * unit + pad) * k, (y * unit + pad) * k) for x, y in shape]
    draw.polygon(pts, fill=(255, 255, 255, 255), outline=(16, 16, 18, 255), width=int(1.6 * k))
    sprite = big.resize((w, h), Image.Resampling.LANCZOS)
    shadow = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
    alpha = sprite.getchannel("A").point(lambda v: int(v * 0.45))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2.2))
    out = Image.new("RGBA", sprite.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (1, 2))
    out.alpha_composite(sprite)
    return out, (pad, pad)


@dataclass
class Overlay:
    kind: str
    start: int
    length: int
    data: dict = field(default_factory=dict)

    def active(self, t: int) -> bool:
        return self.start <= t < self.start + self.length


class Scene:
    def __init__(self, folder: Path, width: int | None, frame_dir: Path) -> None:
        self.folder = folder
        # Frames go to disk as they are made: a 30 s scene is hundreds of
        # full-size frames, far too many to hold in memory.
        self.frame_dir = frame_dir
        self.timeline = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
        self.vw, self.vh = self.timeline["viewport"]
        self.scale = self.timeline["scale"]
        self.out_w = int(width or self.timeline.get("output_width", 1600))
        first_camera = next(
            (op["rect"] for op in self.timeline["ops"] if op["op"] == "camera"),
            [0, 0, self.vw, self.vh],
        )
        self.aspect = first_camera[3] / first_camera[2]
        self.out_h = int(round(self.out_w * self.aspect))
        self.camera = list(map(float, first_camera))
        self.shots: dict[str, Image.Image] = {}  # a few decoded 2x shots, newest last
        self.based: dict[tuple, Image.Image] = {}
        self.pointer, self.pointer_anchor = make_pointer(int(round(22 * self.out_w / self.vw)))
        self.cursor = [self.vw * 0.79, self.vh * 0.70]
        self.cursor_visible = True
        # A shot's file name, or ("blend", from, to, alpha) during a fade.
        self.current: str | tuple[str, str, str, float] | None = None
        self.caption = ("", "")
        self.captions = self._caption_list()
        self.caption_index = -1
        self.overlays: list[Overlay] = []
        self.t = 0
        self.frames: list[tuple[str, int]] = []  # (file name, ms) per distinct frame
        self.last_frame: Image.Image | None = None
        self.label_font = _font(("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), 17)
        self.text_font = _font(("segoeui.ttf", "arial.ttf"), 24)
        self.key_font = _font(("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"), 22)

    def _caption_list(self) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        for op in self.timeline["ops"]:
            if op["op"] == "caption":
                value = (op["label"], op["text"])
                if not seen or seen[-1] != value:
                    seen.append(value)
        return seen

    # --- Pixels ------------------------------------------------------------------

    def shot(self, name: str) -> Image.Image:
        if name not in self.shots:
            if len(self.shots) >= 8:
                self.shots.pop(next(iter(self.shots)))
            self.shots[name] = Image.open(self.folder / name).convert("RGB")
        return self.shots[name]

    def framed(self, name: str) -> Image.Image:
        key = (name, tuple(round(v, 2) for v in self.camera))
        if key not in self.based:
            x, y, w, h = (v * self.scale for v in self.camera)
            image = self.shot(name).resize(
                (self.out_w, self.out_h), Image.Resampling.LANCZOS, box=(x, y, x + w, y + h)
            )
            if len(self.based) > 48:
                self.based.pop(next(iter(self.based)))
            self.based[key] = image
        return self.based[key]

    def base(self) -> Image.Image:
        if self.current is None:
            raise ValueError(f"{self.folder.name}: the timeline waits before showing any shot")
        if isinstance(self.current, tuple):
            _, a, b, alpha = self.current
            return Image.blend(self.framed(a), self.framed(b), alpha)
        return self.framed(self.current)

    def to_out(self, point: list[float]) -> tuple[float, float]:
        x, y, w, h = self.camera
        return ((point[0] - x) * self.out_w / w, (point[1] - y) * self.out_h / h)

    def render(self) -> Image.Image:
        frame = self.base().convert("RGBA")
        cx, cy = self.to_out(self.cursor)
        for overlay in self.overlays:
            if not overlay.active(self.t):
                continue
            progress = (self.t - overlay.start) / overlay.length
            if overlay.kind == "ripple":
                self._ripple(frame, self.to_out(overlay.data["at"]), progress)
            elif overlay.kind == "keys":
                self._keys(frame, overlay.data, progress, overlay.length)
        if self.cursor_visible:
            ax, ay = self.pointer_anchor
            frame.alpha_composite(self.pointer, (int(round(cx - ax)), int(round(cy - ay))))
        canvas = Image.new("RGB", (self.out_w, self.out_h + CAPTION_HEIGHT), BAR_BG)
        canvas.paste(frame.convert("RGB"), (0, 0))
        self._caption_bar(canvas)
        return canvas

    def _ripple(self, frame: Image.Image, at: tuple[float, float], progress: float) -> None:
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        radius = 6 + 22 * ease(progress)
        alpha = int(200 * (1 - progress) ** 1.4)
        x, y = at
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     outline=ACCENT + (alpha,), width=3)
        draw.ellipse((x - radius * 0.55, y - radius * 0.55, x + radius * 0.55, y + radius * 0.55),
                     fill=ACCENT + (int(alpha * 0.35),))
        frame.alpha_composite(layer)

    def _keys(self, frame: Image.Image, data: dict, progress: float, length: int) -> None:
        fade_ms = 150
        elapsed = progress * length
        opacity = min(1.0, elapsed / fade_ms, (length - elapsed) / fade_ms)
        if opacity <= 0:
            return
        keys = data["text"].split()
        draw = ImageDraw.Draw(frame)
        sizes = [draw.textbbox((0, 0), key, font=self.key_font) for key in keys]
        widths = [max(44, (box[2] - box[0]) + 30) for box in sizes]
        plus_w = 26
        total = sum(widths) + plus_w * (len(keys) - 1) + 36
        height = 64
        x0 = (self.out_w - total) // 2
        # Centred on a page y the scene chose, clear of what it is showing.
        centre_y = data.get("y") or self.vh * 0.78
        y0 = int(self.to_out([0, centre_y])[1] - height / 2)
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        a = lambda v: int(v * opacity)  # noqa: E731
        d.rounded_rectangle((x0, y0, x0 + total, y0 + height), radius=16,
                            fill=(12, 12, 14, a(225)), outline=(70, 70, 76, a(255)), width=1)
        x = x0 + 18
        for index, (key, width) in enumerate(zip(keys, widths, strict=True)):
            d.rounded_rectangle((x, y0 + 12, x + width, y0 + height - 12), radius=8,
                                fill=(44, 44, 48, a(255)), outline=(96, 96, 102, a(255)), width=1)
            d.rounded_rectangle((x, y0 + height - 16, x + width, y0 + height - 12), radius=4,
                                fill=(30, 30, 33, a(255)))
            box = sizes[index]
            tx = x + (width - (box[2] - box[0])) / 2 - box[0]
            ty = y0 + (height - (box[3] - box[1])) / 2 - box[1] - 2
            d.text((tx, ty), key, font=self.key_font, fill=TEXT + (a(255),))
            x += width
            if index < len(keys) - 1:
                pbox = d.textbbox((0, 0), "+", font=self.key_font)
                d.text((x + (plus_w - (pbox[2] - pbox[0])) / 2 - pbox[0], ty), "+",
                       font=self.key_font, fill=MUTED + (a(255),))
                x += plus_w
        frame.alpha_composite(layer)

    def _caption_bar(self, canvas: Image.Image) -> None:
        draw = ImageDraw.Draw(canvas)
        top = self.out_h
        draw.line((0, top, self.out_w, top), fill=BAR_LINE, width=1)
        count = max(1, len(self.captions))
        gap = 6
        seg = (self.out_w - gap * (count + 1)) / count
        for index in range(count):
            x = gap + index * (seg + gap)
            color = ACCENT if index <= self.caption_index else (52, 52, 56)
            draw.rectangle((x, top + 1, x + seg, top + 3), fill=color)
        label, text = self.caption
        if not text:
            return
        mid = top + CAPTION_HEIGHT / 2 + 2
        label = label.upper()
        lbox = draw.textbbox((0, 0), label, font=self.label_font)
        lx = 28
        draw.text((lx - lbox[0], mid - (lbox[3] + lbox[1]) / 2), label, font=self.label_font, fill=ACCENT)
        sep = lx + (lbox[2] - lbox[0]) + 22
        draw.line((sep, mid - 14, sep, mid + 14), fill=BAR_LINE, width=2)
        tbox = draw.textbbox((0, 0), text, font=self.text_font)
        draw.text((sep + 22 - tbox[0], mid - (tbox[3] + tbox[1]) / 2), text, font=self.text_font, fill=TEXT)

    # --- Time --------------------------------------------------------------------

    def animating(self) -> bool:
        return any(overlay.active(self.t) for overlay in self.overlays)

    def emit(self, ms: float) -> None:
        remaining = _round_ms(ms)
        while remaining > 0:
            step = min(STEP_MS, remaining) if self.animating() else remaining
            if remaining - step < MIN_FRAME_MS:
                step = remaining
            self._append(self.render(), step)
            self.t += step
            remaining -= step
        self.overlays = [o for o in self.overlays if self.t < o.start + o.length]

    def _append(self, image: Image.Image, ms: int) -> None:
        if self.last_frame is not None and ImageChops.difference(self.last_frame, image).getbbox() is None:
            name, duration = self.frames[-1]
            self.frames[-1] = (name, duration + ms)
            return
        name = f"f{len(self.frames):05d}.png"
        image.save(self.frame_dir / name, compress_level=1)
        self.frames.append((name, ms))
        self.last_frame = image

    def _changed_share(self, a: str, b: str) -> float:
        box = ImageChops.difference(self.framed(a), self.framed(b)).getbbox()
        if box is None:
            return 0.0
        return (box[2] - box[0]) * (box[3] - box[1]) / (self.out_w * self.out_h)

    def steps(self, ms: float) -> int:
        return max(1, int(round(ms / STEP_MS)))

    def play(self) -> None:
        for op in self.timeline["ops"]:
            kind = op["op"]
            if kind == "show":
                self.current = op["shot"]
            elif kind == "wait":
                self.emit(op["ms"])
            elif kind == "fade":
                if self.current is None or self.current == op["shot"]:
                    self.current = op["shot"]
                    continue
                start = self.current if isinstance(self.current, str) else self.current[2]
                n = self.steps(op["ms"])
                if self._changed_share(start, op["shot"]) > LARGE_CHANGE:
                    # A modal backdrop, a scroll or a theme change repaints
                    # nearly every pixel, and each in-between frame then
                    # costs as much as a whole screenshot. Two are enough to
                    # read as a transition.
                    n = min(n, 3)
                for i in range(1, n):
                    self.current = ("blend", start, op["shot"], ease(i / n))
                    self.emit(STEP_MS)
                self.current = op["shot"]
            elif kind == "move":
                self._move(op["to"], op["ms"])
            elif kind == "press":
                self.overlays.append(Overlay("ripple", self.t, 420, {"at": list(self.cursor)}))
                self.emit(90)
            elif kind == "keys":
                self.overlays.append(Overlay(
                    "keys", self.t, int(op.get("ms", 900)),
                    {"text": op["text"], "y": op.get("y")},
                ))
            elif kind == "caption":
                value = (op["label"], op["text"])
                if value != self.caption:
                    self.caption = value
                    self.caption_index = min(self.caption_index + 1, len(self.captions) - 1)
            elif kind == "cursor":
                self.cursor_visible = bool(op["visible"])
            elif kind == "camera":
                self._camera(op["rect"], op.get("ms", 0))
        self.emit(MIN_FRAME_MS)

    def _move(self, to: list[float], ms: float) -> None:
        start = list(self.cursor)
        dx, dy = to[0] - start[0], to[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance < 1:
            return
        # Bow the path a little, the way a hand moves a mouse.
        bow = min(60.0, distance * 0.12)
        control = (start[0] + dx / 2 - dy / distance * bow, start[1] + dy / 2 + dx / distance * bow)
        n = self.steps(ms)
        for i in range(1, n + 1):
            t = ease(i / n)
            x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t * t * to[0]
            y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t * t * to[1]
            self.cursor = [x, y]
            self.emit(STEP_MS)
        self.cursor = list(to)

    def _camera(self, rect: list[float], ms: float) -> None:
        target = list(map(float, rect))
        target[3] = target[2] * self.aspect  # one aspect ratio per GIF
        if not ms:
            self.camera = target
            return
        start = list(self.camera)
        n = self.steps(ms)
        for i in range(1, n + 1):
            t = ease(i / n)
            self.camera = [a + (b - a) * t for a, b in zip(start, target, strict=True)]
            self.emit(STEP_MS)
        self.camera = target


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        # Optional, so imported by name: it is not a Cortex dependency.
        return str(importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 -- any failure falls back to PATH
        found = shutil.which("ffmpeg")
        if not found:
            raise SystemExit("ffmpeg not found. Install imageio-ffmpeg or pass --ffmpeg.") from None
        return found


def encode(frames: list[tuple[str, int]], folder: Path, target: Path, ffmpeg: str, dither: str) -> None:
    lines = []
    for name, ms in frames:
        # Each image is its own input to the concat demuxer, and an image
        # input defaults to a 25 fps time base: durations would snap to
        # 40 ms steps, and some frames to 10 ms, which browsers play as
        # 100 ms. A 100 fps time base keeps every centisecond exact.
        lines.append(f"file '{name}'\noption framerate 100\nduration {ms / 1000:.3f}")
    lines.append(f"file '{frames[-1][0]}'\noption framerate 100")
    (folder / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt"]
    # Two passes rather than one split graph: a single graph has to hold
    # every frame in memory until the palette is known.
    subprocess.run(
        [*base, "-vf", "palettegen=max_colors=256:stats_mode=full:reserve_transparent=1",
         "-frames:v", "1", "-update", "1", "palette.png"],
        cwd=folder, check=True,
    )
    subprocess.run(
        [*base, "-i", "palette.png",
         "-lavfi", f"[0:v][1:v]paletteuse=dither={dither}:diff_mode=rectangle",
         "-fps_mode", "passthrough", "-loop", "0", str(target.resolve())],
        cwd=folder, check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Folder that record_tour.mjs wrote (one subfolder per scene).")
    parser.add_argument("--out", type=Path, required=True, help="Folder to write <scene>.gif into.")
    parser.add_argument("--scene", action="append", help="Only render these scenes.")
    parser.add_argument("--width", type=int, help="Override each scene's output width.")
    parser.add_argument("--dither", default="none", help="paletteuse dither mode (default: none).")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--prefix", default="tour-", help="File name prefix for the GIFs.")
    args = parser.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    args.out.mkdir(parents=True, exist_ok=True)
    folders = sorted(p for p in args.source.iterdir() if (p / "timeline.json").is_file())
    for folder in folders:
        if args.scene and folder.name not in args.scene:
            continue
        target = args.out / f"{args.prefix}{folder.name}.gif"
        with tempfile.TemporaryDirectory(prefix="cortex-gif-") as temp:
            scene = Scene(folder, args.width, Path(temp))
            scene.play()
            encode(scene.frames, Path(temp), target, ffmpeg, args.dither)
        total = sum(ms for _, ms in scene.frames) / 1000
        print(f"{target.name}: {scene.out_w}x{scene.out_h + CAPTION_HEIGHT}, "
              f"{len(scene.frames)} frames, {total:.1f}s, {target.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
