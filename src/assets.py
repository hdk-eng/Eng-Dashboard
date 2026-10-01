from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


def _trim_uniform_border(img: Image.Image, tolerance: int = 18) -> Image.Image:
    """Trim mostly-uniform outer margin while preserving the logo artwork."""
    rgba = img.convert("RGBA")
    if rgba.getbbox() is None:
        return rgba

    # Prefer transparency when available.
    alpha = rgba.getchannel("A")
    alpha_bbox = alpha.point(lambda a: 255 if a > 8 else 0).getbbox()
    if alpha_bbox and alpha_bbox != (0, 0, rgba.width, rgba.height):
        return rgba.crop(alpha_bbox)

    rgb = rgba.convert("RGB")
    corners = [rgb.getpixel((0, 0)), rgb.getpixel((rgb.width - 1, 0)), rgb.getpixel((0, rgb.height - 1)), rgb.getpixel((rgb.width - 1, rgb.height - 1))]
    bg = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
    bg_img = Image.new("RGB", rgb.size, bg)
    diff = ImageChops.difference(rgb, bg_img).convert("L")
    # Make thresholded mask; allow compression artifacts / anti-aliased edges.
    mask = diff.point(lambda v: 255 if v > tolerance else 0)
    bbox = mask.getbbox()
    if not bbox:
        return rgba

    # Small breathing room around visible artwork.
    l, t, r, b = bbox
    pad = max(2, int(min(r - l, b - t) * 0.035))
    l = max(0, l - pad); t = max(0, t - pad); r = min(rgba.width, r + pad); b = min(rgba.height, b + pad)
    return rgba.crop((l, t, r, b))


def normalize_logo_bytes(raw: bytes, canvas_size: tuple[int, int] = (300, 124)) -> bytes:
    """Return a visually normalized PNG on a common white canvas.

    The algorithm crops empty/uniform margins, keeps aspect ratio, then applies
    aspect-aware target bounds so square/tall and wide logos have comparable
    visual weight without distortion.
    """
    with Image.open(io.BytesIO(raw)) as src:
        src.load()
        logo = _trim_uniform_border(src)

    w, h = logo.size
    if w <= 0 or h <= 0:
        raise ValueError("Logo tidak memiliki area gambar yang valid.")
    ratio = w / h

    # Aspect-aware target box: same canvas, different bounds to equalize visual weight.
    if ratio >= 3.2:
        max_w, max_h = 246, 66
    elif ratio >= 1.8:
        max_w, max_h = 228, 76
    elif ratio >= 1.15:
        max_w, max_h = 190, 86
    elif ratio >= 0.75:
        max_w, max_h = 112, 94
    else:
        max_w, max_h = 84, 98

    scale = min(max_w / w, max_h / h)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    logo = logo.resize(new_size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", canvas_size, (255, 255, 255, 255))
    x = (canvas_size[0] - logo.width) // 2
    y = (canvas_size[1] - logo.height) // 2
    if logo.mode != "RGBA":
        logo = logo.convert("RGBA")
    canvas.alpha_composite(logo, (x, y))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def normalized_logo_data_uri(path_text: str) -> str:
    path = Path(path_text)
    raw = normalize_logo_bytes(path.read_bytes())
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
