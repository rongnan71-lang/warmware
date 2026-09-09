# -*- coding: utf-8 -*-
"""Build warmware.ico (a simple flame icon) using only the standard library."""
import struct
import zlib


def make_ico(path: str):
    size = 64
    # simple 64x64 RGBA flame: orange/red ball with yellow core
    px = bytearray()
    import math
    cx = cy = (size - 1) / 2.0
    for y in range(size):
        for x in range(size):
            dx = (x - cx) / (size / 2.0)
            dy = (y - cy) / (size / 2.0)
            d = math.hypot(dx, dy)
            # flame shape: blobby
            r = 0.9
            if d < r:
                t = 1.0 - d / r  # 0 center -> 1 edge
                # yellow core -> orange -> deep red
                if d < 0.5:
                    col = (255, 235, 120)
                elif d < 0.75:
                    col = (255, 150, 40)
                else:
                    col = (210, 60, 30)
                # soften edge
                if d > r - 0.08:
                    a = int(255 * (r - d) / 0.08)
                else:
                    a = 255
                px += bytes((col[0], col[1], col[2], a))
            else:
                px += bytes((0, 0, 0, 0))

    # PNG encode (ico entries can embed PNG)
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(
        b"\x00" + px[i:i + size * 4]  # each row prefixed with filter byte 0
        for i in range(0, len(px), size * 4)
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )

    # ICO container (single 64x64 image as PNG)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 64, 64, 0, 0, 1, 32, len(png), 22)
    with open(path, "wb") as f:
        f.write(header + entry + png)
    print(f"icon written: {path}")


if __name__ == "__main__":
    import os
    make_ico(os.path.join(os.path.dirname(__file__), "warmware.ico"))