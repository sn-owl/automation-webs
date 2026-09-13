#!/usr/bin/env python3
"""
generate_icons.py
SVG 기반 아이콘을 PNG로 생성합니다.
pip install cairosvg 필요 (없으면 placeholder PNG 생성)
"""

import os

os.makedirs("icons", exist_ok=True)

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">
  <rect width="48" height="48" rx="10" fill="#1a1d27"/>
  <rect x="8" y="12" width="32" height="4" rx="2" fill="#e53e3e"/>
  <rect x="8" y="20" width="24" height="3" rx="1.5" fill="#4a5568"/>
  <rect x="8" y="27" width="28" height="3" rx="1.5" fill="#4a5568"/>
  <rect x="8" y="34" width="20" height="3" rx="1.5" fill="#4a5568"/>
</svg>"""

try:
    import cairosvg
    for size in [16, 48, 128]:
        cairosvg.svg2png(
            bytestring=SVG.encode(),
            write_to=f"icons/icon{size}.png",
            output_width=size,
            output_height=size
        )
    print("✓ 아이콘 생성 완료 (cairosvg)")
except ImportError:
    # cairosvg 없으면 최소 PNG 헤더 생성 (placeholder)
    import struct, zlib

    def make_png(size):
        def chunk(name, data):
            c = struct.pack(">I", len(data)) + name + data
            return c + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff)

        header = b"\x89PNG\r\n\x1a\n"
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))

        raw = b""
        for y in range(size):
            raw += b"\x00"
            for x in range(size):
                # 간단한 빨간 사각형
                r = 229 if y < size // 5 else 26
                g = 62 if y < size // 5 else 29
                b = 62 if y < size // 5 else 39
                raw += bytes([r, g, b])

        compressed = zlib.compress(raw)
        idat = chunk(b"IDAT", compressed)
        iend = chunk(b"IEND", b"")
        return header + ihdr + idat + iend

    for size in [16, 48, 128]:
        with open(f"icons/icon{size}.png", "wb") as f:
            f.write(make_png(size))
    print("✓ 아이콘 생성 완료 (placeholder)")
