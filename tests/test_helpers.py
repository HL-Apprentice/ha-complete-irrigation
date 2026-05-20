"""Tests for the controller detection helper.

Per the tdd skill: tests verify behavior through public interfaces, not
implementation details. The helper is a pure function — given a list of
installed HA integration domains plus a mapping of known irrigation
domains, return the irrigation matches in deterministic order.
"""
from __future__ import annotations

import pytest

from custom_components.complete_irrigation.helpers import (
    detect_irrigation_integrations,
)
from custom_components.complete_irrigation.const import KNOWN_IRRIGATION_DOMAINS


# ─── happy paths ─────────────────────────────────────────────────────

def test_no_integrations_installed_returns_empty():
    assert detect_irrigation_integrations(installed_domains=[]) == []


def test_only_non_irrigation_integrations_returns_empty():
    assert (
        detect_irrigation_integrations(
            installed_domains=["zha", "mqtt", "weatherflow", "calendar"]
        )
        == []
    )


def test_single_rachio_installation_returns_rachio():
    result = detect_irrigation_integrations(installed_domains=["rachio"])
    assert result == [("rachio", "Rachio (official)")]


def test_multiple_irrigation_integrations_all_returned():
    result = detect_irrigation_integrations(
        installed_domains=["rachio", "hydrawise", "rainmachine", "mqtt", "zha"]
    )
    domains = [d for d, _ in result]
    assert set(domains) == {"rachio", "hydrawise", "rainmachine"}
    assert len(result) == 3


# ─── sorting + determinism ───────────────────────────────────────────

def test_result_order_is_stable_alphabetical_by_domain():
    """Same input → same output, always — order matters for the UI list."""
    result = detect_irrigation_integrations(
        installed_domains=["rainmachine", "rachio", "hydrawise"]
    )
    domains = [d for d, _ in result]
    assert domains == sorted(domains), f"expected alphabetical, got {domains}"


# ─── deduplication ───────────────────────────────────────────────────

def test_duplicate_installed_domains_deduplicated():
    """HA could in theory report a domain twice (multiple config entries
    for the same domain — e.g. two Rachio controllers). The helper should
    still return that integration once."""
    result = detect_irrigation_integrations(
        installed_domains=["rachio", "rachio", "rachio"]
    )
    assert result == [("rachio", "Rachio (official)")]


# ─── custom mapping (for extensibility) ──────────────────────────────

def test_custom_known_mapping_overrides_default():
    """The helper accepts a custom known-domains mapping so the project
    can ship a v1.1 mapping JSON without changing the call sites."""
    result = detect_irrigation_integrations(
        installed_domains=["foo_irrigation", "bar"],
        known_domains={"foo_irrigation": "Foo Brand", "qux": "Qux Brand"},
    )
    assert result == [("foo_irrigation", "Foo Brand")]


def test_default_known_mapping_includes_expected_brands():
    """Smoke check: the shipped mapping has the brands we promised in PRD."""
    for required in ("rachio", "rachio_local", "hydrawise", "rainmachine",
                     "bhyve", "opensprinkler"):
        assert required in KNOWN_IRRIGATION_DOMAINS, f"missing brand: {required}"


# ─── input types ─────────────────────────────────────────────────────

def test_accepts_iterable_not_just_list():
    """`set` and generators should work too — the helper takes any iterable."""
    result_from_set = detect_irrigation_integrations(
        installed_domains={"rachio", "hydrawise"}
    )
    result_from_gen = detect_irrigation_integrations(
        installed_domains=(d for d in ["rachio", "hydrawise"])
    )
    assert {d for d, _ in result_from_set} == {"rachio", "hydrawise"}
    assert {d for d, _ in result_from_gen} == {"rachio", "hydrawise"}


# ─── value structure ─────────────────────────────────────────────────

def test_returns_tuples_of_domain_and_display_name():
    result = detect_irrigation_integrations(installed_domains=["rachio"])
    domain, display = result[0]
    assert isinstance(domain, str)
    assert isinstance(display, str)
    assert display != domain  # display is human-friendly, not the technical id
