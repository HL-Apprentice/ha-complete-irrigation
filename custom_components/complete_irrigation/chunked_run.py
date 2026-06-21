"""Chunked (batch) run planning — split a long run into controller-cap blocks.

v1.25 — Rachio (and similar cloud controllers) cap each zone activation at a
fixed number of minutes (its "manual run minutes", ~60). A run longer than that
cap can't be delivered in one activation: the controller stops the valve at the
cap. Instead of fighting that reactively, we deliver a long run as back-to-back
**blocks**, each no longer than the cap, with a short **gap** between so the
controller's manual-run timer/state resets cleanly before the next block starts
(off → gap → on = a fresh manual run each time).

Pure logic, HA-free. The execution (timers, switch on/off, gaps) lives in
services.py; this module only decides the block plan.
"""

from __future__ import annotations

# Default controller per-activation cap (minutes) = the block size. Set to 58,
# two minutes UNDER Rachio's 60-min manual-run limit, so OUR auto-stop ends each
# block before Rachio's own cutoff fires (no boundary race). Overridable per
# install via the `controller_max_run_minutes` config if a controller differs.
DEFAULT_CONTROLLER_MAX_RUN_MIN = 58
# Default gap between blocks (seconds): long enough for the controller to
# register the off and reset, short enough to barely affect total runtime.
DEFAULT_BLOCK_GAP_SECONDS = 30


def needs_chunking(total_minutes: float, cap_minutes: int = DEFAULT_CONTROLLER_MAX_RUN_MIN) -> bool:
    """True if the run exceeds the controller cap and must be split into blocks."""
    return int(total_minutes) > max(1, int(cap_minutes))


def plan_blocks(
    total_minutes: float, cap_minutes: int = DEFAULT_CONTROLLER_MAX_RUN_MIN
) -> list[int]:
    """Split `total_minutes` into blocks of at most `cap_minutes`, summing to the
    total. A run within the cap is a single block.

    e.g. plan_blocks(300, 60) -> [60, 60, 60, 60, 60]
         plan_blocks(135, 60) -> [60, 60, 15]
         plan_blocks(45,  60) -> [45]
    """
    total = int(total_minutes)
    cap = max(1, int(cap_minutes))
    if total <= 0:
        return []
    blocks: list[int] = []
    remaining = total
    while remaining > 0:
        block = min(remaining, cap)
        blocks.append(block)
        remaining -= block
    return blocks


def total_wall_minutes(blocks: list[int], gap_seconds: int = DEFAULT_BLOCK_GAP_SECONDS) -> float:
    """Wall-clock minutes to deliver `blocks` including the inter-block gaps
    (water time + (n-1) gaps). Used for ETA / display."""
    if not blocks:
        return 0.0
    water = sum(blocks)
    gaps = (len(blocks) - 1) * max(0, gap_seconds) / 60.0
    return water + gaps
