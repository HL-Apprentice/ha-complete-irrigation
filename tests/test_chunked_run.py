"""Tests for chunked_run — splitting a long run into controller-cap blocks (v1.25)."""

from __future__ import annotations

from custom_components.complete_irrigation.chunked_run import (
    DEFAULT_BLOCK_GAP_SECONDS,
    DEFAULT_CONTROLLER_MAX_RUN_MIN,
    needs_chunking,
    plan_blocks,
    total_wall_minutes,
)


def test_default_cap_is_rachio_headroom():
    # 58 = Rachio's 60-min manual-run limit minus 2 min, so OUR auto-stop ends
    # each block before Rachio's own cutoff. Locked so it can't drift back to 60.
    assert DEFAULT_CONTROLLER_MAX_RUN_MIN == 58
    # Trees (300 min) with the default cap -> five 58s + a 10, summing to 300.
    assert plan_blocks(300) == [58, 58, 58, 58, 58, 10]
    assert sum(plan_blocks(300)) == 300
    assert all(b <= DEFAULT_CONTROLLER_MAX_RUN_MIN for b in plan_blocks(300))


def test_short_run_is_one_block():
    assert plan_blocks(45, 60) == [45]
    assert plan_blocks(60, 60) == [60]
    assert not needs_chunking(60, 60)
    assert not needs_chunking(45, 60)


def test_long_runs_split_into_cap_blocks():
    assert plan_blocks(135, 60) == [60, 60, 15]  # Shrubs
    assert plan_blocks(240, 60) == [60, 60, 60, 60]  # Citrus
    assert plan_blocks(300, 60) == [60, 60, 60, 60, 60]  # Trees
    assert needs_chunking(135, 60)
    assert needs_chunking(300, 60)


def test_blocks_sum_to_total_and_respect_cap():
    for total in (1, 10, 59, 60, 61, 119, 135, 240, 300, 480):
        for cap in (15, 30, 45, 60, 120):
            blocks = plan_blocks(total, cap)
            assert sum(blocks) == total, (total, cap, blocks)
            assert all(0 < b <= cap for b in blocks), (total, cap, blocks)


def test_zero_or_negative_is_no_blocks():
    assert plan_blocks(0, 60) == []
    assert plan_blocks(-5, 60) == []


def test_cap_floor_is_one():
    # A nonsense cap of 0 must not infinite-loop; treated as 1.
    assert plan_blocks(3, 0) == [1, 1, 1]


def test_total_wall_minutes_includes_gaps():
    # 5 blocks of 60 min with 30s gaps = 300 water + 4*0.5 = 302 min wall.
    blocks = plan_blocks(300, 60)
    assert total_wall_minutes(blocks, DEFAULT_BLOCK_GAP_SECONDS) == 302.0
    # single block has no gaps
    assert total_wall_minutes([45], 30) == 45.0
    assert total_wall_minutes([], 30) == 0.0
