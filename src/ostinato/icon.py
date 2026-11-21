# SPDX-License-Identifier: GPL-3.0-only
"""Ostinato's icon, drawn (no image files): a start-repeat sign, as in the name, on the UI's accent colour.

    draw(size, status)   a PIL image; status None, or "ok" / "wait" / "off": a dot in the corner (the tray)
    python -m ostinato.icon out.ico   the Windows icon file (all the usual sizes), for the bundle and the installer
"""
from __future__ import annotations

import sys

ACCENT = (210, 85, 58)  # --accent of the UI
STATUS = {"ok": (47, 158, 85), "wait": (201, 138, 18), "off": (140, 138, 133)}


def draw(size: int = 64, status: str | None = None):
    from PIL import Image, ImageDraw

    s = size * 4  # (drawn large, then scaled down: smooth edges)
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s * 0.22, fill=ACCENT + (255,))
    white = (255, 255, 255, 255)
    top, bottom = s * 0.2, s * 0.8
    d.rectangle([s * 0.2, top, s * 0.32, bottom], fill=white)  # the thick bar
    d.rectangle([s * 0.4, top, s * 0.45, bottom], fill=white)  # the thin bar
    r = s * 0.065
    for cy in (s * 0.38, s * 0.58):  # the two dots
        d.ellipse([s * 0.6 - r, cy - r, s * 0.6 + r, cy + r], fill=white)
    if status in STATUS:
        R = s * 0.15
        cx, cy = s - R - s * 0.04, s - R - s * 0.04
        d.ellipse([cx - R - s * 0.04, cy - R - s * 0.04, cx + R + s * 0.04, cy + R + s * 0.04], fill=white)
        d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=STATUS[status] + (255,))
    return im.resize((size, size), Image.LANCZOS)


def write_ico(path: str) -> None:
    draw(256).save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    write_ico(sys.argv[1] if len(sys.argv) > 1 else "ostinato.ico")
