#!/usr/bin/env python3
"""One-off: generate a minimal favicon.ico (stdlib only, no Pillow dependency).

A flat rounded-corner square in the dashboard's accent green (#5ee6a8), with a
darker "4" mark suggesting x402 - simple enough to hand-draw pixel by pixel.
"""
import struct
import zlib
from pathlib import Path

SIZE = 32
BG = (26, 31, 38)  # dashboard --bg-ish dark
FG = (94, 230, 168)  # --accent

# Hand-drawn 8x8 glyph for "4", scaled up 4x to fill the 32x32 canvas.
GLYPH_4 = [
    "0001100",
    "0011100",
    "0110100",
    "1010100",
    "1111110",
    "0000100",
    "0000100",
]


def build_pixels():
    pixels = [[BG for _ in range(SIZE)] for _ in range(SIZE)]
    scale = 3
    rows = len(GLYPH_4)
    cols = len(GLYPH_4[0])
    off_x = (SIZE - cols * scale) // 2
    off_y = (SIZE - rows * scale) // 2
    for gy, row in enumerate(GLYPH_4):
        for gx, ch in enumerate(row):
            if ch == "1":
                for dy in range(scale):
                    for dx in range(scale):
                        y = off_y + gy * scale + dy
                        x = off_x + gx * scale + dx
                        if 0 <= y < SIZE and 0 <= x < SIZE:
                            pixels[y][x] = FG
    return pixels


def png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def build_png(pixels) -> bytes:
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 per scanline
        for r, g, b in row:
            raw += bytes((r, g, b))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", ihdr)
    png += png_chunk(b"IDAT", idat)
    png += png_chunk(b"IEND", b"")
    return png


def build_ico(png_bytes: bytes) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32, len(png_bytes), 6 + 16
    )
    return header + entry + png_bytes


if __name__ == "__main__":
    png = build_png(build_pixels())
    ico = build_ico(png)
    out = Path(__file__).resolve().parent.parent / "app" / "static" / "favicon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ico)
    print(f"wrote {out} ({len(ico)} bytes)")
