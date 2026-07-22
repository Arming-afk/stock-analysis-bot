#!/usr/bin/env python
"""Generate the PWA icons. No image library needed — writes PNG bytes directly.

    python tools/make_icons.py

Produces web/icons/icon-192.png and icon-512.png: three ascending bars on the
app's background colour. Safe inside a maskable circle (art stays within the
central 80%).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "web" / "icons"

BG = (11, 18, 32)
BAR = (52, 211, 153)
BAR_DIM = (37, 150, 110)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int) -> None:
    bars = [
        # (x_start, x_end, y_top) as fractions of the canvas
        (0.26, 0.40, 0.58, BAR_DIM),
        (0.43, 0.57, 0.42, BAR),
        (0.60, 0.74, 0.26, BAR),
    ]
    baseline = 0.76
    radius = size * 0.02

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type 0 (None)
        fy = y / size
        for x in range(size):
            fx = x / size
            color = BG
            for x0, x1, top, c in bars:
                if x0 <= fx <= x1 and top <= fy <= baseline:
                    # soften the very top of each bar so it does not read as a hard block
                    if fy - top < radius / size and not (x0 + 0.01 <= fx <= x1 - 0.01):
                        continue
                    color = c
                    break
            rows += bytes(color)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"wrote {path} ({size}x{size}, {len(png):,} bytes)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        write_png(OUT_DIR / f"icon-{size}.png", size)


if __name__ == "__main__":
    main()
