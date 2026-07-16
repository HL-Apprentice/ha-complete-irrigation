"""Custom aerial export template (v1.42) — validation + substitution."""

from __future__ import annotations

from custom_components.complete_irrigation.yard_map import (
    Bbox,
    esri_export_url,
    export_url,
    format_export_template,
    valid_export_template,
)

BBOX = Bbox(-111.50000000, 33.20000000, -111.49935567, 33.20053899)

# A real county-assessor orthophoto service (the motivating case).
MC = (
    "https://gis.example.gov/arcgis/rest/services/Imagery/Aerials2026/MapServer/export"
    "?bbox={bbox}&bboxSR=4326&imageSR=4326&size={width},{height}&format=jpg&f=image"
)


# ── validation ──────────────────────────────────────────────────────


def test_accepts_bbox_token_template():
    assert valid_export_template(MC)


def test_accepts_individual_edge_tokens():
    t = "https://x/y?w={west}&s={south}&e={east}&n={north}&px={width}&py={height}"
    assert valid_export_template(t)


def test_rejects_missing_size_tokens():
    assert not valid_export_template("https://x/y?bbox={bbox}")


def test_rejects_missing_bbox():
    assert not valid_export_template("https://x/y?size={width},{height}")


def test_rejects_partial_edge_set():
    # west+south only — would fetch the wrong ground area
    assert not valid_export_template("https://x/y?w={west}&s={south}&px={width}&py={height}")


def test_rejects_non_http_scheme():
    assert not valid_export_template("file:///etc/passwd?bbox={bbox}&s={width},{height}")
    assert not valid_export_template("ftp://x/y?bbox={bbox}&s={width}x{height}")


def test_rejects_empty_and_non_string():
    assert not valid_export_template("")
    assert not valid_export_template("   ")
    assert not valid_export_template(None)
    assert not valid_export_template(123)


# ── substitution ────────────────────────────────────────────────────


def test_substitutes_bbox_and_size():
    url = format_export_template(MC, BBOX, 1536, 1285)
    assert "bbox=-111.50000000,33.20000000,-111.49935567,33.20053899" in url
    assert "size=1536,1285" in url
    assert "{" not in url  # every token consumed


def test_substitutes_individual_edges():
    t = "https://x/y?w={west}&s={south}&e={east}&n={north}&px={width}&py={height}"
    url = format_export_template(t, BBOX, 800, 600)
    assert "w=-111.50000000" in url and "n=33.20053899" in url
    assert "px=800" in url and "py=600" in url


def test_format_string_injection_is_inert():
    # Substitution is str.replace, NOT str.format — a format-style attribute probe
    # must be left untouched rather than evaluated or raising.
    t = "https://x/y?bbox={bbox}&size={width},{height}&evil={width.__class__.__mro__}"
    url = format_export_template(t, BBOX, 10, 20)
    assert "{width.__class__.__mro__}" in url  # inert, not expanded
    assert "size=10,20" in url


def test_unknown_token_left_alone_not_raising():
    t = "https://x/y?bbox={bbox}&size={width},{height}&z={nope}"
    url = format_export_template(t, BBOX, 10, 20)
    assert "{nope}" in url


# ── export_url dispatch ─────────────────────────────────────────────


def test_export_url_uses_esri_when_no_template():
    assert export_url(BBOX, 200, 167) == esri_export_url(BBOX, 200, 167)
    assert "arcgisonline.com" in export_url(BBOX, 200, 167)


def test_export_url_uses_template_when_set():
    url = export_url(BBOX, 1536, 1285, MC)
    assert url.startswith("https://gis.example.gov/")
    assert "arcgisonline.com" not in url


def test_export_url_blank_template_falls_back_to_esri():
    assert "arcgisonline.com" in export_url(BBOX, 200, 167, "   ")
