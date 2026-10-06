"""Draw the app icons used when the clinic installs the app on a phone or computer.

Pure Python (zlib + struct only, no Pillow). Run once from the project root:

    .venv/Scripts/python scripts/make_icons.py

It writes, into static/icons/:
    icon.svg          browser tab icon (any size)
    icon-192.png      home-screen icon
    icon-512.png      splash-screen / large icon
    maskable-512.png  Android "adaptive" icon: full-bleed background, cross kept inside the safe zone

The design is a teal (#0f766e) rounded square with a white medical cross.
"""

import math
import struct
import zlib
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

TEAL = (0x0F, 0x76, 0x6E)
WHITE = (0xFF, 0xFF, 0xFF)

# Shape proportions, as fractions of the icon size.
CORNER_RADIUS = 0.22  # rounded square corners
CROSS_HALF_LENGTH = 0.28  # centre to the tip of each arm
CROSS_HALF_WIDTH = 0.10  # half the thickness of an arm
CROSS_CORNER = 0.03  # slight rounding on the cross
MASKABLE_SCALE = 0.8  # Android may crop to a circle: keep the cross well inside it


# --- Geometry -------------------------------------------------------------------------------

def rounded_box_distance(px, py, cx, cy, half_w, half_h, radius):
    """Signed distance from point (px, py) to a rounded rectangle: negative inside, positive outside."""
    qx = abs(px - cx) - (half_w - radius)
    qy = abs(py - cy) - (half_h - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def coverage(distance):
    """How much of a pixel a shape covers (0..1), from the distance at the pixel centre. Gives smooth edges."""
    return min(max(0.5 - distance, 0.0), 1.0)


def blend(top, bottom, alpha):
    return tuple(round(t * alpha + b * (1 - alpha)) for t, b in zip(top, bottom))


def draw_icon(size, maskable=False):
    """Return the icon as rows of RGBA bytes."""
    centre = size / 2
    scale = MASKABLE_SCALE if maskable else 1.0
    arm_long = CROSS_HALF_LENGTH * size * scale
    arm_wide = CROSS_HALF_WIDTH * size * scale
    cross_radius = CROSS_CORNER * size * scale

    rows = []
    for y in range(size):
        row = bytearray()
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            if maskable:
                background_alpha = 1.0  # full bleed: the phone applies its own mask shape
            else:
                background_alpha = coverage(
                    rounded_box_distance(px, py, centre, centre, centre, centre, CORNER_RADIUS * size)
                )
            cross_distance = min(
                rounded_box_distance(px, py, centre, centre, arm_wide, arm_long, cross_radius),
                rounded_box_distance(px, py, centre, centre, arm_long, arm_wide, cross_radius),
            )
            cross_alpha = coverage(cross_distance)
            if background_alpha == 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            # The cross sits on top of the teal background.
            r, g, b = blend(WHITE, TEAL, cross_alpha)
            row += bytes((r, g, b, round(background_alpha * 255)))
        rows.append(bytes(row))
    return rows


# --- PNG encoding ---------------------------------------------------------------------------

def png_bytes(size, rows):
    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA, no interlace
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 on every row
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def svg_text():
    """The same design as an SVG on a 512 x 512 canvas."""
    s = 512
    long_, wide, corner = CROSS_HALF_LENGTH * s, CROSS_HALF_WIDTH * s, CROSS_CORNER * s
    c = s / 2

    def rect(half_w, half_h):
        return (
            f'<rect x="{c - half_w:g}" y="{c - half_h:g}" width="{2 * half_w:g}" height="{2 * half_h:g}" '
            f'rx="{corner:g}" fill="#ffffff"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}">\n'
        f'  <rect width="{s}" height="{s}" rx="{CORNER_RADIUS * s:g}" fill="#0f766e"/>\n'
        f"  {rect(wide, long_)}\n"
        f"  {rect(long_, wide)}\n"
        f"</svg>\n"
    )


def main():
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    (ICON_DIR / "icon.svg").write_text(svg_text(), encoding="utf-8")
    for filename, size, maskable in [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("maskable-512.png", 512, True),
    ]:
        (ICON_DIR / filename).write_bytes(png_bytes(size, draw_icon(size, maskable)))
        print(f"Wrote {ICON_DIR / filename}")
    print(f"Wrote {ICON_DIR / 'icon.svg'}")


if __name__ == "__main__":
    main()
