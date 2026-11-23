"""The Cortex mark is drawn from geometry; keep the shipped files in step with it."""

from __future__ import annotations

from pathlib import Path
import struct

from tools import cortex_mark


ROOT = Path(__file__).resolve().parents[1]

# Every size the Windows shell asks for at 100-400% scale: title bar and
# system tray, taskbar and search, and Start pins.
SHELL_SIZES = {16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 128, 256}


def test_committed_mark_is_the_generated_mark():
    committed = (ROOT / "assets" / "cortex.svg").read_text(encoding="utf-8")

    assert committed == cortex_mark.build(), (
        "assets/cortex.svg is out of date; run `python tools/cortex_mark.py` "
        "and then `npm run icons` from frontend/."
    )


def test_mark_shows_only_the_full_drawing_to_renderers_without_css():
    svg = cortex_mark.build()

    # The small drawing and the ruled lines are hidden by presentation
    # attributes; only the media queries may reveal them. A renderer that
    # ignores <style> must not paint both drawings on top of each other.
    assert '<g class="sm" display="none">' in svg
    assert '<path class="rules" display="none"' in svg
    assert "@media (max-width:36px){.sm{display:inline}.lg{display:none}}" in svg


def test_windows_icon_has_an_exact_png_frame_for_every_shell_size():
    data = (ROOT / "assets" / "cortex.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)

    assert (reserved, kind) == (0, 1)
    sizes = set()
    for index in range(count):
        width, height, _colors, _zero, _planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        frame = data[offset : offset + length]
        assert frame.startswith(b"\x89PNG\r\n\x1a\n")
        png_width, png_height = struct.unpack(">II", frame[16:24])
        # A directory entry of 0 means 256 pixels.
        assert (png_width, png_height) == (width or 256, height or 256)
        assert bits == 32
        sizes.add(png_width)
    assert sizes == SHELL_SIZES
