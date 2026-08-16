"""Custom aerial export template (v1.42) — validation + substitution."""

from __future__ import annotations

from custom_components.complete_irrigation.yard_map import (
    Bbox,
    esri_export_url,
    export_host_allowed,
    export_url,
    format_export_template,
    normalize_export_template,
    valid_export_template,
)

# Synthetic 60 m x 60 m box on a round base point. Deliberately NOT a real
# parcel: an 8-decimal lat/lon pair identifies a specific house, and this
# file ships to everyone who installs the integration.
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


# ── SSRF host guard (v1.52.1) ────────────────────────────────────────


def test_export_host_allows_public_and_esri():
    assert export_host_allowed(MC)
    assert export_host_allowed("https://services.arcgisonline.com/a?bbox={bbox}")


def test_export_host_blocks_loopback_and_metadata():
    # loopback (probing HA's own API) and cloud metadata (169.254.169.254)
    assert not export_host_allowed("http://127.0.0.1:8123/{bbox}?w={width}&h={height}")
    assert not export_host_allowed("https://[::1]/{bbox}")
    assert not export_host_allowed("http://169.254.169.254/latest/meta-data/?b={bbox}")
    assert not export_host_allowed("http://localhost/{bbox}")
    assert not export_host_allowed("http://metadata.google.internal/{bbox}")


def test_export_host_allows_private_lan_for_self_hosters():
    # A self-hosted GIS/tile server on the LAN is a legitimate use — not blocked.
    assert export_host_allowed("http://192.168.1.50:8080/export?bbox={bbox}&size={width},{height}")
    assert export_host_allowed("https://gis.internal.example/export?bbox={bbox}")


def test_export_host_rejects_junk():
    for bad in (None, "", "   ", 123, "not-a-url", "ftp://host/{bbox}"):
        assert export_host_allowed(bad) is False


# ── frame nudge (v1.58.1) ───────────────────────────────────────────


def test_offset_center_north_and_east():
    from custom_components.complete_irrigation.yard_map import offset_center

    lat, lon = 33.30, -111.84  # Chandler-ish
    lat2, lon2 = offset_center(lat, lon, 3.0, 0.0)  # 3 m north
    assert abs((lat2 - lat) * 111_320.0 - 3.0) < 0.01  # ~3 m in latitude
    assert lon2 == lon
    lat3, lon3 = offset_center(lat, lon, 0.0, -10.0)  # 10 m west
    assert lat3 == lat
    import math

    east_m = (lon3 - lon) * 111_320.0 * math.cos(math.radians(lat))
    assert abs(east_m + 10.0) < 0.01


def test_offset_center_zero_and_none_are_identity():
    from custom_components.complete_irrigation.yard_map import offset_center

    assert offset_center(33.3, -111.8, 0.0, 0.0) == (33.3, -111.8)
    assert offset_center(33.3, -111.8, None, None) == (33.3, -111.8)


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


# ── percent-encoded braces (v1.42.1) ────────────────────────────────
# `{` / `}` are illegal in a URL (RFC 3986), so a template copied from a browser
# address bar or a linkified URL arrives with them encoded. Verbatim capture of
# the real-world failure that motivated this:
ENCODED = (
    "https://gis.example.gov/arcgis/rest/services/Imagery/Aerials2026/MapServer/export"
    "?bbox=%7Bbbox%7D&bboxSR=4326&imageSR=4326&size=%7Bwidth%7D,%7Bheight%7D"
    "&format=jpg&f=image"
)


def test_percent_encoded_braces_are_accepted():
    assert valid_export_template(ENCODED)


def test_percent_encoded_braces_normalize_to_real_tokens():
    assert normalize_export_template(ENCODED) == MC


def test_lowercase_percent_encoding_also_accepted():
    lower = ENCODED.replace("%7B", "%7b").replace("%7D", "%7d")
    assert valid_export_template(lower)
    assert normalize_export_template(lower) == MC


def test_encoded_template_substitutes_correctly():
    url = format_export_template(ENCODED, BBOX, 1536, 1285)
    assert "bbox=-111.50000000,33.20000000,-111.49935567,33.20053899" in url
    assert "size=1536,1285" in url
    assert "%7B" not in url and "{" not in url


def test_encoded_and_raw_produce_identical_urls():
    assert format_export_template(ENCODED, BBOX, 800, 600) == format_export_template(
        MC, BBOX, 800, 600
    )


def test_normalize_does_not_blanket_urldecode():
    # Only the braces are decoded — a %26 that is meant to stay DATA must survive,
    # or a blanket unquote would turn it into a parameter separator.
    t = "https://x/y?bbox={bbox}&size={width},{height}&q=a%26b%20c"
    assert normalize_export_template(t) == t
    assert "%26" in format_export_template(t, BBOX, 1, 1)


def test_normalize_trims_and_handles_non_string():
    assert normalize_export_template("  https://x?bbox={bbox}&s={width},{height}  ").startswith(
        "https://"
    )
    assert normalize_export_template(None) == ""
    assert normalize_export_template(42) == ""
