"""Split planning (v1.56) — pure, HA-free."""

from __future__ import annotations

from custom_components.complete_irrigation.schedule_split import (
    SPLIT_CHUNK_DEFAULTS,
    effective_min_chunk,
    fully_fits,
    merge_chunk_defaults,
    plan_split,
)


def test_fits_whole_in_one_gap():
    assert plan_split(30, [40], 5) == [30]
    assert fully_fits(30, [40], 5)


def test_splits_across_two_gaps():
    assert plan_split(30, [20, 20], 5) == [20, 10]
    assert fully_fits(30, [20, 20], 5)


def test_skips_gaps_smaller_than_min_chunk():
    # second gap (3) is below the 5-min floor -> unused, shortfall reported
    assert plan_split(30, [20, 3], 5) == [20, 0]
    assert not fully_fits(30, [20, 3], 5)


def test_does_not_strand_sub_chunk_remainder():
    # naive greedy would place 18 then leave 2 (< 5) stranded; instead leave a
    # full 5-min final chunk.
    assert plan_split(20, [18, 10], 5) == [15, 5]
    assert fully_fits(20, [18, 10], 5)


def test_shortfall_when_gaps_too_small_overall():
    res = plan_split(30, [5, 5], 5)
    assert res == [5, 5] and sum(res) == 10  # only 10 of 30 placed
    assert not fully_fits(30, [5, 5], 5)


def test_min_chunk_floor_respected_everywhere():
    for chunk in plan_split(60, [7, 7, 7, 7, 7], 5):
        assert chunk == 0 or chunk >= 5


def test_zero_and_edge_inputs():
    assert plan_split(0, [10], 5) == [0]
    assert plan_split(10, [], 5) == []
    assert fully_fits(0, [], 5)  # nothing to place


# ── v1.56 per-plant-type chunk defaults ─────────────────────────────


def test_merge_chunk_defaults_over_builtins():
    # partial + garbage config merges over the built-ins without dropping a type
    merged = merge_chunk_defaults({"tree": 30, "grass": "nope", "bogus": 99})
    assert merged["tree"] == 30  # overridden
    assert merged["grass"] == SPLIT_CHUNK_DEFAULTS["grass"]  # garbage ignored
    assert set(merged) == set(SPLIT_CHUNK_DEFAULTS)  # no type lost, no junk added
    assert merge_chunk_defaults(None) == SPLIT_CHUNK_DEFAULTS  # bad config -> builtins


def test_effective_min_chunk_precedence():
    d = merge_chunk_defaults({"tree": 25})
    # a plant-type profile wins and uses the (customizable) type default
    assert effective_min_chunk(None, "tree", d) == 25
    assert effective_min_chunk(99, "tree", d) == 25  # type beats an explicit value
    # no profile -> explicit min_chunk
    assert effective_min_chunk(12, "", d) == 12
    # nothing set -> global default
    assert effective_min_chunk(None, "", d) == 5
    assert effective_min_chunk(None, None, d) == 5
