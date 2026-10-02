"""不依赖第三方库的极简 PNG 生成器（用于样例现场照片）。"""
from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def make_paint_png(width: int = 480, height: int = 360,
                   base=(225, 222, 214), stripe=(255, 140, 0),
                   pattern: str = "grid") -> bytes:
    """生成纯色 + 简单图案的 PNG，占位作为现场照片。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0
        for x in range(width):
            r, g, b = base
            if pattern == "grid":
                if x % 48 < 2 or y % 48 < 2:
                    r, g, b = (max(v - 60, 0) for v in base)
            elif pattern == "crack":
                if abs((x - width // 2) - (y - height // 2) * 2) < 3 \
                        or abs((x - width // 3) - y) < 2:
                    r, g, b = 60, 60, 60
            elif pattern == "band":
                if height // 2 - 14 < y < height // 2 + 14:
                    r, g, b = stripe
            elif pattern == "dots":
                if ((x - 80) ** 2 + (y - 90) ** 2) ** 0.5 < 26 \
                        or ((x - 330) ** 2 + (y - 240) ** 2) ** 0.5 < 34:
                    r, g, b = stripe
            rows += bytes((r, g, b))
    comp = zlib.compress(bytes(rows), 9)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # RGB
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", comp) + _chunk(b"IEND", b"")
