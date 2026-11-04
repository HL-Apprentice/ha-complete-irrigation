"""Per-zone-type rain lockout - pure policy, no Home Assistant imports.

Event rainfall R is a 72-hour total. Tempest history and Open-Meteo
(``past_days=3``) are blended, then turned into a lockout length per zone
type. Daily ETo scales that length; the existing heat ceiling still caps it.

Zone types are read off data the integration already stores. Nothing here
writes a schedule or flips ``enabled``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .category_match import suggest_category
from .weather_gate import RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN, rain_to_inches

# Holding types get a multi-day lockout from the rain table.
ZONE_DEEP_DRIP = "deep_drip"
ZONE_FLOWERS_GARDEN = "flowers_garden"
ZONE_TURF = "turf"
# Birdbath: no residual lockout. It skips only while rain is actually falling.
ZONE_BIRDBATH = "birdbath"
# Germination / new-planting establishment: never locked out.
ZONE_GERMINATION = "germination"

HOLDING_ZONE_TYPES = (ZONE_DEEP_DRIP, ZONE_FLOWERS_GARDEN, ZONE_TURF)
ALL_ZONE_TYPES = (*HOLDING_ZONE_TYPES, ZONE_BIRDBATH, ZONE_GERMINATION)

# Agreement band: the two 72h totals "agree" when the gap is within
# max(0.15 in, 40% of the larger reading). Otherwise use the lower value
# plus 25% of the gap and raise RAIN_SOURCE_DISAGREE.
RAIN_AGREE_ABS_IN = 0.15
RAIN_AGREE_REL = 0.40
RAIN_DISAGREE_BLEND = 0.25
FLAG_RAIN_SOURCE_DISAGREE = "RAIN_SOURCE_DISAGREE"

# scale = clamp(0.12 / daily_ETo, 0.5, 1.5). 0.12 in/day is the 1.0x reference
# (a mild day). Hotter demand shortens the lockout; cool weather lengthens it,
# and the clamp keeps either extreme from running away.
ETO_SCALE_REF_IN = 0.12
ETO_SCALE_MIN = 0.5
ETO_SCALE_MAX = 1.5

FORECAST_EXTEND_MIN_IN = 0.25
FORECAST_EXTEND_MIN_PROB = 60.0
FORECAST_EXTEND_DAYS = 1

# >2 in: flowers+garden table value is 4 days, but a vegetable-garden zone
# sharing that clock is capped at 2 so greens are not held for a week.
GARDEN_HEAVY_RAIN_CAP_DAYS = 2
HEAVY_RAIN_IN = 2.0

# Table starts here even if the user sets the config floor to 0.
TABLE_MIN_INCHES = 0.10

# Birdbath hold while rain is falling. Refreshed by the coordinator; cleared
# as soon as rain stops. Not a multi-day lockout.
ACTIVE_RAIN_HOLD = timedelta(minutes=20)
ACTIVE_RAIN_REFRESH_WITHIN = timedelta(minutes=10)

# Hourly precip above this counts as "raining now" (Open-Meteo current hour).
ACTIVE_RAIN_HOUR_MM = 0.1

RAINY_WEATHER_STATES = frozenset(
    {
        "rainy",
        "pouring",
        "lightning",
        "lightning-rainy",
        "hail",
        "snowy-rainy",
    }
)

_DEEP_CATEGORIES = frozenset({"trees", "citrus", "bushes"})
_TURF_CATEGORIES = frozenset({"lawn"})
_GARDEN_CATEGORIES = frozenset({"vegetable_garden"})
_DEEP_PRESETS = frozenset({"tree", "shrub", "cactus_succulent"})
_TURF_PRESETS = frozenset({"grass"})
_FLOWER_PRESETS = frozenset({"flower"})

# (deep drip, flowers+garden, turf) days. Buckets are [lo, hi) except the
# 1-2 in bucket, which includes exactly 2.0. Above 2.0 is the last row.
_BUCKETS: tuple[tuple[float, float, tuple[int, int, int]], ...] = (
    (0.10, 0.25, (1, 1, 1)),
    (0.25, 0.50, (2, 1, 2)),
    (0.50, 1.00, (4, 2, 3)),
    (1.00, 2.00, (6, 3, 4)),  # inclusive of 2.0 - see table_days()
)


def _clean_inches(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


@dataclass(frozen=True)
class RainBlend:
    """72h rainfall after the Tempest / Open-Meteo blend."""

    inches: float | None
    tempest_in: float | None
    open_meteo_in: float | None
    disagree: bool
    used: str  # mean | conservative | tempest_only | open_meteo_only | none

    @property
    def flag(self) -> str | None:
        return FLAG_RAIN_SOURCE_DISAGREE if self.disagree else None


def combine_rain_sources(tempest_in: Any, open_meteo_in: Any) -> RainBlend:
    """Blend two 72h totals.

    One missing source falls back to the other (``disagree`` stays false -
    there is nothing to disagree with). Both missing -> no rainfall. When both
    exist and the gap exceeds ``max(0.15 in, 40% of the larger)``, the result
    is ``lower + 25% of the gap`` and ``disagree`` is set.
    """
    tempest = _clean_inches(tempest_in)
    meteo = _clean_inches(open_meteo_in)
    if tempest is None and meteo is None:
        return RainBlend(None, None, None, False, "none")
    if tempest is None:
        return RainBlend(meteo, None, meteo, False, "open_meteo_only")
    if meteo is None:
        return RainBlend(tempest, tempest, None, False, "tempest_only")
    lo, hi = (tempest, meteo) if tempest <= meteo else (meteo, tempest)
    gap = hi - lo
    tolerance = max(RAIN_AGREE_ABS_IN, RAIN_AGREE_REL * hi)
    if gap <= tolerance:
        return RainBlend((tempest + meteo) / 2.0, tempest, meteo, False, "mean")
    return RainBlend(lo + RAIN_DISAGREE_BLEND * gap, tempest, meteo, True, "conservative")


def table_days(rainfall_in: float) -> tuple[int, int, int] | None:
    """(deep drip, flowers+garden, turf) days for R, or None below 0.10 in.

    Exactly 2.0 in stays in the 1-2 in row. Anything above 2.0 in is the last row.
    """
    if not math.isfinite(rainfall_in) or rainfall_in < TABLE_MIN_INCHES:
        return None
    if rainfall_in <= HEAVY_RAIN_IN:
        for lo, hi, days in _BUCKETS:
            # The 1-2 bucket's upper bound is inclusive; the others are [lo, hi).
            if lo == 1.00:
                if lo <= rainfall_in <= hi:
                    return days
            elif lo <= rainfall_in < hi:
                return days
        return None
    return (7, 4, 5)


def base_lockout_days(
    rainfall_in: float,
    zone_type: str,
    *,
    garden_present: bool = False,
    min_inches: float = TABLE_MIN_INCHES,
) -> int | None:
    """Days from the rain table for one zone type.

    Birdbath and germination are always None (0 days). A configured floor
    above the table's 0.10 in start suppresses lighter events. When a
    vegetable-garden zone shares the flowers+garden clock and R > 2 in, that
    clock is capped at 2 days.
    """
    if zone_type not in HOLDING_ZONE_TYPES:
        return None
    try:
        rain = float(rainfall_in)
        floor = float(min_inches)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rain):
        return None
    if math.isfinite(floor) and floor > 0 and rain < floor:
        return None
    row = table_days(rain)
    if row is None:
        return None
    index = HOLDING_ZONE_TYPES.index(zone_type)
    days = row[index]
    if (
        zone_type == ZONE_FLOWERS_GARDEN
        and garden_present
        and rain > HEAVY_RAIN_IN
        and days > GARDEN_HEAVY_RAIN_CAP_DAYS
    ):
        return GARDEN_HEAVY_RAIN_CAP_DAYS
    return days


def reference_daily_eto(*values: Any) -> float | None:
    """Hottest demand wins: the max of the current and forecast daily ETo values.

    A larger ETo shortens the lockout (the scale is ``0.12 / ETo``). Missing
    or non-positive inputs are ignored. None when nothing usable was passed -
    the scaler then uses the short summer fallback.
    """
    usable: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            usable.append(number)
    return max(usable) if usable else None


def eto_scale_factor(daily_eto_in: float | None) -> float:
    """``clamp(0.12 / daily_ETo, 0.5, 1.5)``. Missing ETo uses the 0.25 in fallback."""
    eto = daily_eto_in
    if eto is None or not math.isfinite(eto) or eto <= 0:
        eto = RAIN_LOCKOUT_FALLBACK_DAILY_ETO_IN
    return min(ETO_SCALE_MAX, max(ETO_SCALE_MIN, ETO_SCALE_REF_IN / eto))


def scaled_lockout_hours(
    days: float,
    daily_eto_in: float | None,
    max_hours: float | None,
) -> float:
    """Table days x ETo scale x 24, then the heat ceiling (``max_hours``)."""
    hours = float(days) * eto_scale_factor(daily_eto_in) * 24.0
    if max_hours is not None and math.isfinite(max_hours) and max_hours > 0:
        hours = min(hours, float(max_hours))
    return hours


def forecast_extends_lockout(precip_in: Any, probability_pct: Any) -> bool:
    """True when the next 24h calls for ≥0.25 in at ≥60% probability."""
    try:
        inches = float(precip_in)
        probability = float(probability_pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(inches) or not math.isfinite(probability):
        return False
    return inches >= FORECAST_EXTEND_MIN_IN and probability >= FORECAST_EXTEND_MIN_PROB


def extended_until(
    current: datetime | None,
    now: datetime,
    *,
    extra_days: float = FORECAST_EXTEND_DAYS,
    max_hours: float | None = None,
) -> datetime | None:
    """Push an active ``current`` forward by ``extra_days``, clamped to the heat ceiling.

    Does not start a lockout. If the ceiling already holds ``current``, the
    same timestamp comes back (no growth).
    """
    if current is None or not (now < current):
        return current
    proposed = current + timedelta(days=float(extra_days))
    if max_hours is not None and math.isfinite(max_hours) and max_hours > 0:
        ceiling = now + timedelta(hours=float(max_hours))
        if proposed > ceiling:
            proposed = ceiling
    if proposed <= current:
        return current
    return proposed


def moisture_releases_early(current_pct: Any, min_pct: Any) -> bool:
    """A reading strictly below the zone minimum may end a lockout early.

    Anything else - including a saturated reading - is not a release, and this
    function never produces a longer lockout. Non-numeric / non-finite values
    are not a release.
    """
    try:
        current = float(current_pct)
        minimum = float(min_pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(current) or not math.isfinite(minimum):
        return False
    return current < minimum


def weather_state_is_rain(state: Any) -> bool:
    return str(state or "").strip().lower() in RAINY_WEATHER_STATES


def detect_active_rain(
    *,
    weather_states: list[Any] | tuple[Any, ...] = (),
    today_in: float | None = None,
    previous_today_in: float | None = None,
    current_precip_mm: float | None = None,
) -> bool:
    """True only while rain is falling, not because a daily total is merely elevated.

    A rising edge on today's accumulation counts (the total grew since the last
    read). The first sample has no previous value and does not count - a sensor
    that already reads 0.4 in at startup is not proof that it is raining now.
    """
    if any(weather_state_is_rain(state) for state in weather_states):
        return True
    if (
        today_in is not None
        and previous_today_in is not None
        and today_in > previous_today_in + 0.001
    ):
        return True
    return current_precip_mm is not None and current_precip_mm > ACTIVE_RAIN_HOUR_MM


def _majority(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    # Stable: first-seen wins a tie (values are already in a caller-chosen order).
    return max(counts, key=lambda key: (counts[key], -values.index(key)))


def _preset_zone_type(preset: str | None) -> str | None:
    if preset in _TURF_PRESETS:
        return ZONE_TURF
    if preset in _FLOWER_PRESETS:
        return ZONE_FLOWERS_GARDEN
    if preset in _DEEP_PRESETS:
        return ZONE_DEEP_DRIP
    return None


def zone_is_garden(*, category: str | None, entity_id: str = "", friendly_name: str = "") -> bool:
    """Vegetable garden (the >2 in cap), not every bed whose name says garden.

    ``Flower Garden`` is flowers. A vegetable garden is the ``vegetable_garden``
    category, or a name that says vegetable / ``veg`` as its own word.
    """
    if category in _GARDEN_CATEGORIES:
        return True
    blob = f"{entity_id} {friendly_name}".lower()
    if "vegetable" in blob:
        return True
    tokens = [tok for tok in blob.replace("_", " ").replace("-", " ").split() if tok]
    return "veg" in tokens or "veggies" in tokens


def classify_zone(
    *,
    entity_id: str,
    friendly_name: str = "",
    category: str | None = None,
    schedule_profiles: tuple[str, ...] | list[str] = (),
    plant_presets: tuple[str, ...] | list[str] = (),
    establishment_active: bool = False,
) -> str:
    """Map one zone onto a lockout type using config that already exists.

    Order, most specific first:

    1. Germination - only an in-window establishment schedule. A name that says
       germination or new grass does not exempt the zone forever; use the
       override for that. These never take a rain lockout.
    2. Birdbath - the name says birdbath. ``independent_zones`` is not a
       birdbath; those zones fall through to the turf default unless something
       else classifies them.
    3. The zone's stored plant ``category`` (lawn / vegetable_garden / trees /
       citrus / bushes).
    4. Majority ``care_plan_preset`` of plants on the zone, then majority
       schedule ``split_profile``.
    5. Name keywords. ``flower`` wins over a bare ``garden`` word so Flower
       Garden is flowers, not a vegetable garden.
    6. Otherwise turf. Unconfigured zones already default to the lawn moisture
       band, so the lockout default matches that.
    """
    blob = f"{entity_id} {friendly_name}".lower()
    if establishment_active:
        return ZONE_GERMINATION
    if any(token in blob for token in ("birdbath", "bird bath", "bird_bath", "bird-bath")):
        return ZONE_BIRDBATH
    if category in _TURF_CATEGORIES:
        return ZONE_TURF
    if category in _GARDEN_CATEGORIES:
        return ZONE_FLOWERS_GARDEN
    if category in _DEEP_CATEGORIES:
        return ZONE_DEEP_DRIP
    preset_type = _preset_zone_type(_majority([p for p in plant_presets if p]))
    if preset_type:
        return preset_type
    profile_type = _preset_zone_type(_majority([p for p in schedule_profiles if p]))
    if profile_type:
        return profile_type
    if "flower" in blob and not zone_is_garden(
        category=category, entity_id=entity_id, friendly_name=friendly_name
    ):
        return ZONE_FLOWERS_GARDEN
    suggested = suggest_category(blob)
    if suggested == "lawn":
        return ZONE_TURF
    # "garden" shares the flowers+garden clock. The vegetable-garden cap is
    # separate and only applies to vegetable/veg keywords or that category.
    if suggested == "vegetable_garden":
        return ZONE_FLOWERS_GARDEN
    if suggested in _DEEP_CATEGORIES:
        return ZONE_DEEP_DRIP
    return ZONE_TURF


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class StoredRainLockout:
    """The lockout document persisted in its own Store key."""

    until_by_type: dict[str, datetime | None] = field(default_factory=dict)
    last_event_rain_in: float | None = None
    rain_source_disagree: bool = False
    forecast_extended: set[str] = field(default_factory=set)
    sources: dict[str, Any] = field(default_factory=dict)
    previous_today_in: float | None = None
    # zone_id -> lockout type. That zone is exempt while its type clock runs.
    released_zones: dict[str, str] = field(default_factory=dict)
    # False only for a store that has never recorded a rainfall baseline
    # (upgrade / empty file). In-memory objects default to already seeded so
    # tests and a running process arm on a real rise.
    baseline_seeded: bool = True

    def with_updates(self, **kwargs: Any) -> StoredRainLockout:
        payload: dict[str, Any] = {
            "until_by_type": dict(self.until_by_type),
            "last_event_rain_in": self.last_event_rain_in,
            "rain_source_disagree": self.rain_source_disagree,
            "forecast_extended": set(self.forecast_extended),
            "sources": dict(self.sources),
            "previous_today_in": self.previous_today_in,
            "released_zones": dict(self.released_zones),
            "baseline_seeded": self.baseline_seeded,
        }
        payload.update(kwargs)
        return StoredRainLockout(**payload)

    def __post_init__(self) -> None:
        until = {zone_type: self.until_by_type.get(zone_type) for zone_type in ALL_ZONE_TYPES}
        until[ZONE_GERMINATION] = None
        self.until_by_type = until
        self.forecast_extended = {z for z in self.forecast_extended if z in HOLDING_ZONE_TYPES}
        cleaned: dict[str, str] = {}
        for zone_id, zone_type in (self.released_zones or {}).items():
            if isinstance(zone_id, str) and zone_type in HOLDING_ZONE_TYPES:
                cleaned[zone_id] = zone_type
        self.released_zones = cleaned

    def to_dict(self) -> dict[str, Any]:
        return {
            "lockout_until": {
                zone_type: (
                    self.until_by_type.get(zone_type).isoformat()
                    if self.until_by_type.get(zone_type)
                    else None
                )
                for zone_type in ALL_ZONE_TYPES
            },
            "last_event_rain_in": self.last_event_rain_in,
            "rain_source_disagree": bool(self.rain_source_disagree),
            "forecast_extended": sorted(self.forecast_extended),
            "sources": dict(self.sources),
            "previous_today_in": self.previous_today_in,
            "released_zones": dict(self.released_zones),
            "baseline_seeded": bool(self.baseline_seeded),
        }

    @classmethod
    def from_dict(cls, data: Any) -> StoredRainLockout:
        if not isinstance(data, dict):
            data = {}
        raw_until = data.get("lockout_until")
        until: dict[str, datetime | None] = {}
        if isinstance(raw_until, dict):
            for zone_type in ALL_ZONE_TYPES:
                until[zone_type] = _parse_dt(raw_until.get(zone_type))
        last = _clean_inches(data.get("last_event_rain_in"))
        # 0.0 is a legitimate baseline; _clean_inches keeps it. None stays None.
        if data.get("last_event_rain_in") is None:
            last = None
        extended_raw = data.get("forecast_extended")
        extended = (
            {str(item) for item in extended_raw if str(item) in HOLDING_ZONE_TYPES}
            if isinstance(extended_raw, list)
            else set()
        )
        sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
        raw_released = data.get("released_zones")
        released: dict[str, str] = {}
        if isinstance(raw_released, dict):
            for zone_id, zone_type in raw_released.items():
                if isinstance(zone_id, str) and zone_type in HOLDING_ZONE_TYPES:
                    released[zone_id] = str(zone_type)
        if "baseline_seeded" in data:
            seeded = bool(data.get("baseline_seeded"))
        else:
            # A stored total is already a baseline. Missing both means upgrade.
            seeded = data.get("last_event_rain_in") is not None
        return cls(
            until_by_type=until,
            last_event_rain_in=last,
            rain_source_disagree=bool(data.get("rain_source_disagree")),
            forecast_extended=extended,
            sources=dict(sources),
            previous_today_in=_clean_inches(data.get("previous_today_in")),
            released_zones=released,
            baseline_seeded=seeded,
        )


def _blank_state() -> StoredRainLockout:
    return StoredRainLockout()


def release_zone_type(state: StoredRainLockout, zone_type: str) -> StoredRainLockout:
    """A completed watering ends that type's lockout. It does not clear the rain baseline.

    Leaving ``last_event_rain_in`` in place is what stops the same 72h total from
    immediately re-arming the lockout on the next tick.
    """
    if zone_type not in ALL_ZONE_TYPES or zone_type == ZONE_GERMINATION:
        return state
    if state.until_by_type.get(zone_type) is None and zone_type not in state.forecast_extended:
        return state
    until = dict(state.until_by_type)
    until[zone_type] = None
    return state.with_updates(
        until_by_type=until,
        forecast_extended=set(state.forecast_extended) - {zone_type},
    )


def _untils_equal(left: dict[str, datetime | None], right: dict[str, datetime | None]) -> bool:
    return all(left.get(zone_type) == right.get(zone_type) for zone_type in ALL_ZONE_TYPES)


@dataclass(frozen=True)
class LockoutUpdate:
    state: StoredRainLockout
    changed: bool
    logs: tuple[str, ...]
    armed_types: frozenset[str] = frozenset()


def decide_lockouts(
    prev: StoredRainLockout | None,
    *,
    now: datetime,
    rainfall_in: float | None,
    blend: RainBlend,
    daily_eto_in: float | None,
    max_hours: float | None,
    garden_present: bool,
    active_rain: bool,
    forecast_in: float | None,
    forecast_probability_pct: float | None,
    min_inches: float = TABLE_MIN_INCHES,
    today_in: float | None = None,
    legacy_hours: float | None = None,
    arm_from_table: bool = True,
    allow_forecast_extend: bool = True,
    allow_baseline_retreat: bool = True,
    seed_only: bool = False,
    dry_zones: dict[str, str] | None = None,
) -> LockoutUpdate:
    """Next lockout state.

    Rainfall (rising 72h total) and a qualifying forecast may set or extend a
    holding type. Soil moisture may only clear one. Birdbath follows active
    rain. Germination stays clear. The same rainfall total does not push an
    existing ``until`` later - that was the stuck-timer bug.
    """
    state = prev if prev is not None else _blank_state()
    until = {zone_type: state.until_by_type.get(zone_type) for zone_type in ALL_ZONE_TYPES}
    extended = set(state.forecast_extended)
    last = state.last_event_rain_in
    released = dict(state.released_zones)
    logs: list[str] = []
    armed_now: set[str] = set()
    rain = _clean_inches(rainfall_in) if rainfall_in is not None else None

    if seed_only:
        # No observation yet: stay unseeded and do not arm. Seeding a missing
        # total would make the first real reading look like new rain.
        if rain is None:
            return LockoutUpdate(state=state, changed=False, logs=())
        seeded_last = last if last is not None else rain
        sources = {
            "tempest_in": blend.tempest_in,
            "open_meteo_in": blend.open_meteo_in,
            "combined_in": blend.inches,
            "used": blend.used,
            "flag": blend.flag,
        }
        updated = state.with_updates(
            last_event_rain_in=seeded_last,
            baseline_seeded=True,
            rain_source_disagree=False,
            sources=sources,
            previous_today_in=today_in if today_in is not None else state.previous_today_in,
        )
        return LockoutUpdate(
            state=updated,
            changed=seeded_last != last or not state.baseline_seeded,
            logs=("seeded rainfall baseline without arming",) if seeded_last != last else (),
        )

    # ── birdbath: only while rain is falling ──────────────────────
    if active_rain:
        horizon = now + ACTIVE_RAIN_HOLD
        current = until.get(ZONE_BIRDBATH)
        if current is None or current < now + ACTIVE_RAIN_REFRESH_WITHIN:
            until[ZONE_BIRDBATH] = horizon
            logs.append("birdbath lockout while rain is falling")
    elif until.get(ZONE_BIRDBATH) is not None:
        until[ZONE_BIRDBATH] = None
        logs.append("birdbath lockout cleared - rain stopped")
    until[ZONE_GERMINATION] = None

    # ── event rain arms / extends holding types on a rising total ─
    if not arm_from_table:
        # v1.62 fallback (or "do not arm from this sample"). A positive
        # legacy_hours is the old ET-scaled duration. None means the old
        # formula declined to lock - record the baseline, do not extend.
        if rain is not None:
            rose = last is None or rain > last + 0.001
            dropped = last is not None and rain < last - 0.01
            if rose:
                last = rain
                if legacy_hours is not None and legacy_hours > 0:
                    proposed = now + timedelta(hours=float(legacy_hours))
                    for zone_type in HOLDING_ZONE_TYPES:
                        current = until.get(zone_type)
                        if current is None or proposed > current:
                            until[zone_type] = proposed
                            extended.discard(zone_type)
                            armed_now.add(zone_type)
                    logs.append(
                        f"legacy rain lockout {float(legacy_hours):.1f}h from today's {rain:.2f} in"
                    )
            elif dropped and allow_baseline_retreat:
                last = rain
                logs.append(
                    f"rain total fell to {rain:.2f} in - baseline reset, lockout not extended"
                )
    elif rain is not None:
        rose = last is None or rain > last + 0.001
        dropped = last is not None and rain < last - 0.01
        if rose:
            last = rain
            for zone_type in HOLDING_ZONE_TYPES:
                days = base_lockout_days(
                    rain, zone_type, garden_present=garden_present, min_inches=min_inches
                )
                if days is None:
                    continue
                hours = scaled_lockout_hours(days, daily_eto_in, max_hours)
                proposed = now + timedelta(hours=hours)
                current = until.get(zone_type)
                if current is None or proposed > current:
                    until[zone_type] = proposed
                    extended.discard(zone_type)
                    armed_now.add(zone_type)
                    logs.append(
                        f"{zone_type} lockout {hours:.1f}h from {rain:.2f} in "
                        f"({days}d x ETo scale, heat ceiling {max_hours}h)"
                    )
        elif dropped and allow_baseline_retreat:
            last = rain
            logs.append(f"rain total fell to {rain:.2f} in - baseline reset, lockout not extended")

    # ── forecast may add one day to a lockout that is already active.
    # Cached or stale Open-Meteo data must not take this branch.
    if allow_forecast_extend and forecast_extends_lockout(forecast_in, forecast_probability_pct):
        for zone_type in HOLDING_ZONE_TYPES:
            current = until.get(zone_type)
            if current is None or not (now < current) or zone_type in extended:
                continue
            proposed = extended_until(current, now, max_hours=max_hours)
            extended.add(zone_type)
            if proposed is not None and proposed > current:
                until[zone_type] = proposed
                logs.append(
                    f"{zone_type} lockout extended 1d for forecast "
                    f"{forecast_in:.2f} in @ {forecast_probability_pct:.0f}%"
                )
            else:
                logs.append(
                    f"{zone_type} forecast extension held at the heat ceiling ({max_hours}h)"
                )

    # A new arm of a type puts every zone of that type back under the clock.
    if armed_now:
        released = {
            zone_id: zone_type
            for zone_id, zone_type in released.items()
            if zone_type not in armed_now
        }

    # One dry zone releases only itself, and never on the tick that armed its type.
    for zone_id, zone_type in (dry_zones or {}).items():
        if zone_type not in HOLDING_ZONE_TYPES or zone_type in armed_now:
            continue
        current = until.get(zone_type)
        if current is not None and now < current and released.get(zone_id) != zone_type:
            released[zone_id] = zone_type
            logs.append(f"{zone_id} released early - soil moisture below minimum")

    sources = {
        "tempest_in": blend.tempest_in,
        "open_meteo_in": blend.open_meteo_in,
        "combined_in": blend.inches,
        "used": blend.used,
        "forecast_24h_in": forecast_in,
        "forecast_24h_probability_pct": forecast_probability_pct,
        "flag": blend.flag,
    }
    updated = state.with_updates(
        until_by_type=until,
        last_event_rain_in=last,
        rain_source_disagree=blend.disagree,
        forecast_extended=extended,
        sources=sources,
        previous_today_in=today_in if today_in is not None else state.previous_today_in,
        released_zones=released,
        baseline_seeded=True,
    )
    changed = (
        not _untils_equal(state.until_by_type, updated.until_by_type)
        or state.last_event_rain_in != updated.last_event_rain_in
        or state.rain_source_disagree != updated.rain_source_disagree
        or state.forecast_extended != updated.forecast_extended
        or state.released_zones != updated.released_zones
    )
    return LockoutUpdate(
        state=updated, changed=changed, logs=tuple(logs), armed_types=frozenset(armed_now)
    )


def _stat_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def aggregate_tempest_72h(
    rows: list[Any] | None,
    *,
    now: datetime,
    state_class: str | None,
    live_today_in: float | None,
    unit: str | None,
) -> float | None:
    """72h inches from recorder rows.

    ``total_increasing`` sensors have no usable ``max`` (it is absent). Their
    ``sum`` is the running total, so the window is last sum minus first sum,
    plus any live reading ahead of the last sum (long-term stats lag ~1h).

    A resetting daily total (rain today) uses each local day's peak. The first
    day is partial when the window opens after midnight: only the increase seen
    inside the window is counted, not that day's full peak. Today is
    ``max(statistic peak, live sensor)`` so the lagging statistic cannot hide
    rain that has already fallen.
    """
    window_start = now - timedelta(hours=72)
    parsed: list[tuple[datetime, Any]] = []
    for row in rows or []:
        start = _parse_dt(_stat_get(row, "start"))
        if start is None:
            continue
        if start.tzinfo is None and now.tzinfo is not None:
            start = start.replace(tzinfo=now.tzinfo)
        elif start.tzinfo is not None and now.tzinfo is None:
            start = start.replace(tzinfo=None)
        if start < window_start or start > now:
            continue
        parsed.append((start, row))
    parsed.sort(key=lambda item: item[0])
    if not parsed and live_today_in is None:
        return None

    if state_class == "total_increasing":
        sums: list[float] = []
        for _start, row in parsed:
            raw = _stat_get(row, "sum")
            if raw is None:
                raw = _stat_get(row, "state")
            inches = rain_to_inches(raw, unit) if raw is not None else None
            if inches is not None:
                sums.append(inches)
        if not sums:
            return live_today_in
        growth = max(0.0, sums[-1] - sums[0])
        if live_today_in is not None and live_today_in > sums[-1]:
            growth += live_today_in - sums[-1]
        return growth

    by_day: dict[Any, list[Any]] = {}
    for start, row in parsed:
        raw = _stat_get(row, "max")
        if raw is None:
            raw = _stat_get(row, "state")
        inches = rain_to_inches(raw, unit) if raw is not None else None
        if inches is None:
            continue
        slot = by_day.get(start.date())
        if slot is None:
            by_day[start.date()] = [start, inches, inches]
        else:
            slot[2] = max(slot[2], inches)
    total = 0.0
    counted = False
    today = now.date()
    first_day = window_start.date()
    for day, (first_start, first_val, peak) in by_day.items():
        if day == today:
            used = peak
            if live_today_in is not None:
                used = max(used, live_today_in)
            total += used
            counted = True
            continue
        if day == first_day and first_start > window_start:
            total += max(0.0, peak - first_val)
            counted = True
            continue
        total += peak
        counted = True
    if today not in by_day and live_today_in is not None:
        total += live_today_in
        counted = True
    return total if counted else None
