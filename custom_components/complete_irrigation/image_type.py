"""Image type detection from magic bytes — pure logic, HA-free.

Two jobs, and they must agree, which is why they share one table:

  * VALIDATE uploaded bytes before they're written under ``www/`` and served back
    from ``/local``. A crafted SVG/HTML payload stored as ``.jpg`` is a
    content-sniffing / stored-XSS vector, so only real raster images are allowed.
  * LABEL the ``data:`` URI sent to a vision model with the image's ACTUAL type.
    The identify path used to hard-code ``image/jpeg`` for every stored photo, so
    a PNG was announced to the provider as JPEG — a mismatch some vision APIs
    reject outright.

Lives in its own module (rather than services.py) so it's importable — and
testable — without Home Assistant or voluptuous present, matching the repo's
pure-logic discipline. CI has no HA installed.
"""

from __future__ import annotations

# Longest signature we inspect is WEBP's "RIFF....WEBP" = 12 bytes. Anything
# shorter can't be identified, and a valid-looking prefix alone isn't trustworthy.
_MIN_HEADER = 12


def image_mime(data: bytes) -> str | None:
    """The image's MIME type from its magic bytes, or None if it isn't a known
    raster image. Order: JPEG, PNG, GIF87a/89a, WEBP, BMP."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < _MIN_HEADER:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def looks_like_image(data: bytes) -> bool:
    """True when the bytes are a recognized raster image (the upload guard)."""
    return image_mime(data) is not None
