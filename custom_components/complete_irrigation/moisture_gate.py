"""MoistureGate — pure-logic 4-band moisture controller.

Bands (low to high moisture):
  URGENT    — current < min       → extend runtime
  NORMAL    — min ≤ current < target → base runtime
  LIGHT     — target ≤ current < max → reduced runtime
  SATURATED — current ≥ max       → skip
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

BAND_URGENT = "urgent"
BAND_NORMAL = "normal"
BAND_LIGHT = "light"
BAND_SATURATED = "saturated"

COMBINE_AVERAGE = "average"
COMBINE_LOWEST = "lowest"
COMBINE_HIGHEST = "highest"
COMBINE_PRIMARY = "primary"

_LIGHT_MULT = 0.5
_URGENT_MULT = 1.5


@dataclass(frozen=True)
class MoistureDecision:
    band: str
    runtime_minutes: int
    skip: bool
    reason: str


def evaluate_moisture(
    current: float,
    min_pct: float,
    target: float,
    max_pct: float,
    base_minutes: int,
) -> MoistureDecision:
    """Decide runtime adjustment based on which band the current
    moisture reading falls into."""
    if current >= max_pct:
        return MoistureDecision(
            band=BAND_SATURATED,
            runtime_minutes=0,
            skip=True,
            reason=f"skipped: moisture {current:.0f}% ≥ max {max_pct:.0f}% (saturated)",
        )
    if current >= target:
        minutes = max(1, round(base_minutes * _LIGHT_MULT))
        return MoistureDecision(
            band=BAND_LIGHT,
            runtime_minutes=minutes,
            skip=False,
            reason=f"light run: moisture {current:.0f}% ≥ target {target:.0f}% (light)",
        )
    if current >= min_pct:
        return MoistureDecision(
            band=BAND_NORMAL,
            runtime_minutes=base_minutes,
            skip=False,
            reason=f"normal run: moisture {current:.0f}% near target {target:.0f}%",
        )
    minutes = max(1, round(base_minutes * _URGENT_MULT))
    return MoistureDecision(
        band=BAND_URGENT,
        runtime_minutes=minutes,
        skip=False,
        reason=(
            f"urgent: moisture {current:.0f}% below min {min_pct:.0f}% "
            f"— runtime extended to {minutes} min"
        ),
    )


def combine_moisture(readings: Iterable[float], mode: str) -> float | None:
    """Combine multiple sensor readings into a single value per `mode`."""
    values = list(readings)
    if not values:
        return None
    if mode == COMBINE_AVERAGE:
        return sum(values) / len(values)
    if mode == COMBINE_LOWEST:
        return min(values)
    if mode == COMBINE_HIGHEST:
        return max(values)
    if mode == COMBINE_PRIMARY:
        return values[0]
    raise ValueError(f"unknown combine mode: {mode}")
