"""Split planning — pure logic, HA-free (v1.56).

When a non-essential run (e.g. a bird-bath fill) can't fit as one block around the
essential runs, it can be broken into chunks that drop into the free gaps. This
module computes that split deterministically: given the run's length, the free-gap
sizes (chronological), and a minimum chunk floor, it returns how many minutes to
place in each gap.

It's used two ways: as a deterministic suggestion, and to BOUND an LLM's split
proposal (the LLM can't invent a slice smaller than the floor or overfill a gap).
No dropping — a shortfall (sum < total) simply means the gaps at this floor can't
hold the whole run, which the caller surfaces rather than silently losing water.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MIN_CHUNK_MINUTES = 5

# v1.56 — per-plant-type default split-chunk floors (minutes). Trees want long,
# uninterrupted soaks so they rarely split; grass tolerates small frequent slices.
# Keys mirror care_plan_preset. Customizable per install via config
# ("split_chunk_defaults"); a schedule tagged with a type inherits its value, so
# changing "tree" here retunes every tree schedule at once.
SPLIT_PROFILES = ("tree", "shrub", "grass", "flower", "cactus_succulent")
SPLIT_CHUNK_DEFAULTS: dict[str, int] = {
    "tree": 20,
    "shrub": 15,
    "grass": 5,
    "flower": 8,
    "cactus_succulent": 10,
}


def merge_chunk_defaults(config_value: Any) -> dict[str, int]:
    """Merge a stored ``split_chunk_defaults`` config dict over the built-ins, so a
    partial/garbage stored value can never drop a type. Each value is a positive int."""
    out = dict(SPLIT_CHUNK_DEFAULTS)
    if isinstance(config_value, dict):
        for k, v in config_value.items():
            if k in out:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if iv >= 1:
                    out[k] = iv
    return out


def effective_min_chunk(
    min_chunk_minutes: int | None, split_profile: str | None, defaults: dict[str, int]
) -> int:
    """The split floor to enforce for a schedule. A plant-type profile (tree/shrub/…)
    resolves to that type's configurable default; otherwise the schedule's own
    explicit min_chunk_minutes; otherwise the global default. Never below 1."""
    if split_profile and split_profile in defaults:
        return max(1, int(defaults[split_profile]))
    if min_chunk_minutes:
        return max(1, int(min_chunk_minutes))
    return DEFAULT_MIN_CHUNK_MINUTES


def plan_split(total_minutes: int, gaps: list[int], min_chunk: int) -> list[int]:
    """Distribute ``total_minutes`` across ``gaps`` (free-window sizes, in order).

    Each placed chunk is ``>= min_chunk`` and ``<= its gap``. Greedy, order
    preserving. Returns a list parallel to ``gaps`` (minutes placed in each, 0 if
    unused); ``sum(result) <= total_minutes``. A shortfall means the gaps can't
    hold the whole run at this floor. Never strands a leftover smaller than one
    chunk when a full final chunk can be preserved instead.
    """
    remaining = max(0, int(total_minutes))
    mc = max(1, int(min_chunk))
    out: list[int] = []
    for gap in gaps:
        g = max(0, int(gap))
        if remaining < mc or g < mc:
            out.append(0)
            continue
        c = min(g, remaining)
        # Avoid stranding a sub-chunk remainder: if placing c would leave
        # 0 < leftover < mc, and there's room to leave a FULL chunk instead
        # (remaining >= 2*mc), shave c so the leftover is exactly one chunk.
        if 0 < remaining - c < mc and remaining - mc >= mc:
            c = remaining - mc
        out.append(c)
        remaining -= c
    return out


def fully_fits(total_minutes: int, gaps: list[int], min_chunk: int) -> bool:
    """True when the whole run can be placed across the gaps at this floor."""
    return sum(plan_split(total_minutes, gaps, min_chunk)) >= max(0, int(total_minutes))
