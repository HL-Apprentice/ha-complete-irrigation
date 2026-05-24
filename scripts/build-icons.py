#!/usr/bin/env python3
"""Generate the integration's icon assets from the canonical SVG.

  Source:  assets/icon.svg           (hand-editable vector)
  Outputs: assets/icon.png            — 256×256, transparent, for HACS branding
           assets/icon@2x.png         — 512×512, transparent, for HACS @2x
           assets/social-preview.png  — 1280×640, dark backdrop, for GitHub OG

Run from the repo root:  python3 scripts/build-icons.py

Pipeline:
  1. cairosvg rasterizes assets/icon.svg at the requested pixel size.
     Honors the SVG's bezier curves, gradients, and opacities exactly
     (the prior Python-polygon approximation produced visible seams
     where the top wedge met the bottom circle — v1.17.6 swapped to
     cairosvg so the rendered icon matches the SVG design).
  2. The social preview composites the rendered icon onto a dark
     gradient backdrop with the integration name + tagline.

cairosvg is in `pip install cairosvg`. If you don't have it, the
script prints install instructions and exits without overwriting any
existing PNGs.
"""

from __future__ import annotations

# macOS SIP strips DYLD_LIBRARY_PATH for /usr/bin/python3, so cairocffi
# can't find Homebrew's libcairo through normal lib-search paths. Pre-
# load the dylib explicitly via ctypes BEFORE importing cairosvg.
import ctypes
import io
import sys
from pathlib import Path

for cairo_path in (
    "/opt/homebrew/lib/libcairo.2.dylib",  # Apple Silicon Homebrew
    "/usr/local/lib/libcairo.2.dylib",  # Intel Homebrew
    "/opt/local/lib/libcairo.2.dylib",  # MacPorts
):
    try:
        ctypes.CDLL(cairo_path, ctypes.RTLD_GLOBAL)
        break
    except OSError:
        continue

try:
    import cairosvg
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:
    print(f"Missing dependency ({e.name}). Install with:")
    print("    pip3 install cairosvg pillow")
    print("On macOS, also: brew install cairo")
    sys.exit(1)

REPO = Path(__file__).parent.parent
SVG_SOURCE = REPO / "assets" / "icon.svg"
ASSETS = REPO / "assets"

# Social preview backdrop palette
BG_TOP = (15, 24, 32)
BG_BOTTOM = (28, 38, 48)
TEXT_PRIMARY = (255, 255, 255)
TEXT_SECONDARY = (176, 196, 212)
TEXT_ACCENT = (127, 205, 240)


def _render_svg_to_png(svg_path: Path, size: int) -> Image.Image:
    """Rasterize an SVG to a square RGBA PIL image at `size` pixels.

    cairosvg preserves the SVG's transparency, gradients, and curves
    exactly — no shape approximation."""
    png_bytes = cairosvg.svg2png(
        url=str(svg_path),
        output_width=size,
        output_height=size,
    )
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Best-available system font for the social-preview text."""
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _build_social_preview(icon: Image.Image) -> Image.Image:
    """1280×640 dark-backdrop banner with the icon + title text."""
    width, height = 1280, 640
    canvas = Image.new("RGBA", (width, height), (*BG_TOP, 255))
    # Vertical background gradient
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        t = y / (height - 1)
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=(*color, 255))

    # Paste icon scaled to 440px, vertically centered, left-padded
    icon_size = 440
    icon_scaled = icon.resize((icon_size, icon_size), Image.LANCZOS)
    canvas.paste(icon_scaled, (100, (height - icon_size) // 2), icon_scaled)

    # Title text on the right. Auto-size title down if it would overflow.
    title_text = "Complete Irrigation"
    title_size = 80
    title_font = _load_font(title_size)
    text_left = 600
    max_width = width - text_left - 60
    while title_font.getlength(title_text) > max_width and title_size > 40:
        title_size -= 2
        title_font = _load_font(title_size)

    sub_font = _load_font(34)
    tag_font = _load_font(22)

    draw.text((text_left, 232), title_text, fill=(*TEXT_PRIMARY, 255), font=title_font)
    draw.text(
        (text_left, 322),
        "Home Assistant custom integration",
        fill=(*TEXT_SECONDARY, 255),
        font=sub_font,
    )
    draw.text(
        (text_left, 400),
        "Schedules  ·  Run history  ·  Weather gates  ·  Moisture-aware",
        fill=(*TEXT_ACCENT, 255),
        font=tag_font,
    )
    return canvas.convert("RGB")  # PNG with no alpha for social preview


def main() -> None:
    if not SVG_SOURCE.exists():
        print(f"missing {SVG_SOURCE}")
        sys.exit(1)
    ASSETS.mkdir(parents=True, exist_ok=True)

    # Render the icon at 2x first (sharper source for the downscale +
    # social preview composite).
    icon_512 = _render_svg_to_png(SVG_SOURCE, 512)
    icon_512.save(ASSETS / "icon@2x.png", "PNG", optimize=True)

    icon_256 = _render_svg_to_png(SVG_SOURCE, 256)
    icon_256.save(ASSETS / "icon.png", "PNG", optimize=True)

    social = _build_social_preview(icon_512)
    social.save(ASSETS / "social-preview.png", "PNG", optimize=True)

    print(f"wrote {ASSETS / 'icon.png'} (256×256)")
    print(f"wrote {ASSETS / 'icon@2x.png'} (512×512)")
    print(f"wrote {ASSETS / 'social-preview.png'} (1280×640)")


if __name__ == "__main__":
    main()
