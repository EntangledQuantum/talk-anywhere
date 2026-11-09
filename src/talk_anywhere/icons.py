"""Draw the tray and launcher icons. No third-party image library."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

from talk_anywhere.paths import icon_dir

Color = tuple[int, int, int]

IDLE_BG: Color = (42, 36, 32)
IDLE_FG: Color = (230, 162, 60)
LIVE_BG: Color = (230, 162, 60)
LIVE_FG: Color = (32, 26, 22)
ERROR_BG: Color = (140, 47, 39)
ERROR_FG: Color = (246, 236, 226)


def _png(path: Path, size: int, pixels: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(pixels[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def _clamp(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def _circle_alpha(x: float, y: float, cx: float, cy: float, radius: float) -> float:
    return _clamp(radius + 0.55 - math.hypot(x - cx, y - cy))


def _capsule_alpha(x: float, y: float, cx: float, y0: float, y1: float, radius: float) -> float:
    nearest_y = min(max(y, y0), y1)
    return _clamp(radius + 0.55 - math.hypot(x - cx, y - nearest_y))


def _bars(size: int) -> list[tuple[float, float, float, float]]:
    count = 4
    width = size * 0.078
    gap = size * 0.050
    heights = (0.30, 0.58, 0.40, 0.72)
    total = count * width + (count - 1) * gap
    x = (size - total) / 2 + width / 2
    bars = []
    for height in heights:
        pixel_h = height * size
        y0 = (size - pixel_h) / 2 + width / 2
        y1 = (size + pixel_h) / 2 - width / 2
        if y1 < y0:
            y0 = y1 = size / 2
        bars.append((x, y0, y1, width / 2))
        x += width + gap
    return bars


def render(size: int, background: Color, foreground: Color) -> bytes:
    pixels = bytearray(size * size * 4)
    cx = cy = size / 2
    radius = size * 0.40
    bars = _bars(size)
    for py in range(size):
        for px in range(size):
            sample_x = px + 0.5
            sample_y = py + 0.5
            cover = _circle_alpha(sample_x, sample_y, cx, cy, radius)
            if cover <= 0:
                continue
            bar = 0.0
            for bar_x, y0, y1, bar_r in bars:
                bar = max(bar, _capsule_alpha(sample_x, sample_y, bar_x, y0, y1, bar_r))
            red = int(background[0] + (foreground[0] - background[0]) * bar)
            green = int(background[1] + (foreground[1] - background[1]) * bar)
            blue = int(background[2] + (foreground[2] - background[2]) * bar)
            offset = (py * size + px) * 4
            pixels[offset] = red
            pixels[offset + 1] = green
            pixels[offset + 2] = blue
            pixels[offset + 3] = int(255 * cover)
    return bytes(pixels)


def write_icons(directory: Path | None = None) -> dict[str, Path]:
    directory = directory or icon_dir()
    directory.mkdir(parents=True, exist_ok=True)
    specs = {
        "idle.png": (128, IDLE_BG, IDLE_FG),
        "listening.png": (128, LIVE_BG, LIVE_FG),
        "error.png": (128, ERROR_BG, ERROR_FG),
        "app.png": (256, IDLE_BG, IDLE_FG),
    }
    written: dict[str, Path] = {}
    for name, (size, background, foreground) in specs.items():
        path = directory / name
        _png(path, size, render(size, background, foreground))
        written[name] = path
    return written


def ensure_icons() -> dict[str, Path]:
    directory = icon_dir()
    needed = ("idle.png", "listening.png", "error.png", "app.png")
    if all((directory / name).is_file() for name in needed):
        return {name: directory / name for name in needed}
    return write_icons(directory)
