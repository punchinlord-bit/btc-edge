"""Generate BTC-Edge brand assets: app icon + favicon matching the banner scheme
(dark background + neon-green #76d630 accent)."""

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ACCENT = (0x76, 0xD6, 0x30)          # neon green from the banner
ACCENT_DIM = (0x4a, 0x8a, 0x1e)      # darker green for depth
TOP = (0x11, 0x16, 0x1f)             # dark navy (top of gradient)
BOTTOM = (0x00, 0x00, 0x00)          # black (bottom of gradient)
FONT_PATHS = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

OUT = Path("src/btc_edge/web/static")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
        g = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
        b = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return img


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _btc_glyph(size: int) -> Image.Image:
    """Render the Bitcoin glyph in the accent green with a soft glow."""
    font_size = int(size * 0.62)
    font = _load_font(font_size)
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(glyph)
    bbox = d.textbbox((0, 0), "\u20bf", font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    d.text((x, y), "\u20bf", font=font, fill=(*ACCENT, 255))

    # Soft glow: blurred copy composited beneath the sharp glyph.
    glow = glyph.filter(ImageFilter.GaussianBlur(radius=size * 0.04))
    glow = Image.eval(glow, lambda a: int(a * 0.7))
    glow.alpha_composite(glyph)
    return glow


def _build_icon(size: int) -> Image.Image:
    radius = int(size * 0.22)
    base = _gradient(size)
    mask = _rounded_mask(size, radius)

    # Subtle inner accent ring.
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    border = max(2, int(size * 0.012))
    rd.rounded_rectangle(
        [border, border, size - border - 1, size - border - 1],
        radius=radius, outline=(*ACCENT, 140), width=border,
    )

    glyph = _btc_glyph(size)

    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)
    icon = Image.alpha_composite(icon, glyph)
    icon = Image.alpha_composite(icon, ring)

    # Apply the rounded mask so transparent corners are preserved.
    final = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    final.paste(icon, (0, 0), mask)
    return final


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # High-res app icon / favicon PNG.
    png = _build_icon(512)
    png.save(OUT / "favicon.png")
    png.save(OUT / "app-icon.png")

    # favicon.ico (legacy fallback) at a single clean size; favicon.png (above)
    # serves high-DPI browsers via <link rel="icon" type="image/png">.
    _build_icon(64).convert("RGBA").save(OUT / "favicon.ico", format="ICO")

    # Sync the canonical root banner into the served static directory (byte-exact).
    banner_src = Path("banner.png")
    if banner_src.exists():
        shutil.copy2(banner_src, OUT / "banner.png")

    print("Generated:", [p.name for p in sorted(OUT.iterdir())])


if __name__ == "__main__":
    main()
