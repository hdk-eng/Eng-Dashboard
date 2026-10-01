from __future__ import annotations

import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw

from src.assets import normalize_logo_bytes


def make_logo(size, box):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill="black")
    bio = io.BytesIO(); img.save(bio, format="PNG")
    return bio.getvalue()


def test_logo_normalization_wide_and_square_same_canvas():
    wide = normalize_logo_bytes(make_logo((600, 200), (80, 55, 520, 145)))
    square = normalize_logo_bytes(make_logo((400, 400), (90, 90, 310, 310)))
    for payload in (wide, square):
        with Image.open(io.BytesIO(payload)) as img:
            assert img.size == (300, 124)
            assert img.format == "PNG"
