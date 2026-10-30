"""Decode an image and prepare the small JPEG used by the works page."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

COVER_MAX_SIDE = 640
COVER_JPEG_QUALITY = 82


@dataclass(frozen=True)
class CoverImage:
    data: bytes
    width: int
    height: int


def _opaque_rgb(image: Image.Image) -> Image.Image:
    if "A" not in image.getbands() and "transparency" not in image.info:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def normalize_cover(source: Path) -> CoverImage:
    with Image.open(source) as image:
        image.verify()
    with Image.open(source) as image:
        image.load()
        oriented = ImageOps.exif_transpose(image)
        oriented.thumbnail((COVER_MAX_SIDE, COVER_MAX_SIDE), Image.Resampling.LANCZOS)
        rgb = _opaque_rgb(oriented)
        encoded = BytesIO()
        rgb.save(encoded, "JPEG", quality=COVER_JPEG_QUALITY, optimize=True, progressive=True)
        return CoverImage(encoded.getvalue(), rgb.width, rgb.height)
