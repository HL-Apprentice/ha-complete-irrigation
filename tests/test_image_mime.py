"""Image magic-byte detection (upload validation + vision data: URI labelling).

Imports the PURE image_type module, not services: services pulls in voluptuous /
Home Assistant, which CI deliberately does not install — importing it here made
the whole module fail collection rather than skip.
"""

from __future__ import annotations

import base64

from custom_components.complete_irrigation.image_type import (
    image_mime as _image_mime,
)
from custom_components.complete_irrigation.image_type import (
    looks_like_image as _looks_like_image,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
BMP = b"BM" + b"\x00" * 16


def test_detects_each_supported_type():
    assert _image_mime(JPEG) == "image/jpeg"
    assert _image_mime(PNG) == "image/png"
    assert _image_mime(GIF) == "image/gif"
    assert _image_mime(WEBP) == "image/webp"
    assert _image_mime(BMP) == "image/bmp"


def test_png_is_accepted_and_labelled_png_not_jpeg():
    # Regression: identify used to announce every stored image as image/jpeg,
    # which some vision providers reject when the bytes are actually PNG.
    assert _looks_like_image(PNG)
    assert _image_mime(PNG) == "image/png"


def test_rejects_non_images():
    assert _image_mime(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert _image_mime(b"<!DOCTYPE html><html></html>") is None
    assert not _looks_like_image(b"not an image at all")


def test_rejects_too_short():
    assert _image_mime(b"\xff\xd8\xff") is None  # valid prefix, too short to trust
    assert _image_mime(b"") is None


def test_whitespace_tolerant_base64_roundtrip():
    # CLI `base64 file.png` wraps at 76 cols; validate=True rejects the newlines,
    # so the service strips whitespace before decoding.
    wrapped = base64.encodebytes(PNG).decode()  # includes newlines
    assert "\n" in wrapped
    stripped = "".join(wrapped.split())
    assert base64.b64decode(stripped, validate=True) == PNG
    assert _image_mime(base64.b64decode(stripped, validate=True)) == "image/png"
