"""Rain-lockout policy — pure logic, no Home Assistant imports.

The coordinator reads sensors and Open-Meteo, then applies these decisions:

  • 72h rainfall from Tempest history and Open-Meteo (``past_days=3``)
  • per-zone-type hold (deep drip / flowers+garden / turf)
  • birdbaths hold only while rain is actually falling
  • germination holds never
  • ETo scales the day table; the existing heat ceiling still caps it
  • soil moisture and a finished run can release a zone early, never lengthen a hold
  • a confident near-term forecast can add one day to a hold that is already active

Zone type is derived from data the install already has. Nothing here writes a
zone or schedule setting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .weather_gate import rain_lockout_max_hours

# Durable holds. Birdbath and germination are not stored as multi-day deadlines.
TYPE_DEEP_DRIP = "deep_drip"
TYPE_FLOWERS_GARDEN = "flowers_garden"
TYPE_TURF = "turf"
TYPE_BIRDBATH = "birdbath"
TYPE_GERMINATION = "germination"

DURABLE_ZONE_TYPES: tuple[str, ...] = (TYPE_DEEP_DRIP, TYPE_FLOWERS_GARDEN, TYPE_TURF)
ZONE_TYPES: tuple[str, ...] = (
    TYPE_DEEP_DRIP,
    TYPE_FLOWERS_GARDEN,
    TYPE_TURF,
    TYPE_BIRDBATH,
    TYPE_GERMINATION,
)

# Reference daily ETo (inches). scale = clamp(0.12 / ETo, 0.5, 1.5).
# 0.12 in/day → 1.0x. Hotter (higher ETo) shortens the hold; cooler lengthens it.
ETO_SCALE_REFERENCE_IN = 0.12
ETO_SCALE_MIN = 0.5
ETO_SCALE_MAX = 1.5

# Table starts here. Below this, rain never starts a multi-day hold.
RAIN_EVENT_MIN_INCHES = 0.10

# Two sources "agree" when the gap is within max(0.15 in, 40% of the larger).
AGREE_ABS_IN = 0.15
AGREE_FRACTION = 0.40
# On disagreement, use the lower reading plus this fraction of the gap.
DISAGREE_GAP_FRACTION = 0.25

# A new sample this close to the rain total that armed the hold is the same event
# (a daily accumulator must not push the deadline forward on every tick).
SAME_EVENT_EPSILON_IN = 0.01

# Forecast may add one day only when this much rain is likely inside 24h.
FORECAST_EXTEND_MIN_IN = 0.25
FORECAST_EXTEND_MIN_PROB = 60.0
FORECAST_EXTEND_DAYS = 1

# Owner day table: deep drip / flowers+garden / turf.
# Bands are [lo, hi) except the 1-2 in band, which includes 2.0; ">2" is strict.
# The ">2" flowers+garden cell is written "4 (garden capped at 2)": flowers+garden
# is one lockout type, so that cell is the cap, 2 days.
_DAY_TABLE: tuple[tuple[float, float | None, bool, int, int, int], ...] = (
    # lo, hi, hi_inclusive, deep, flowers_garden, turf
    (0.10, 0.25, False, 1, 1, 1),
    (0.25, 0.50, False, 2, 1, 2),
    (0.50, 1.00, False, 4, 2, 3),
    (1.00, 2.00, True, 6, 3, 4),
    (2.00, None, False, 7, 2, 5),
)
# The uncapped ">2" flowers+garden figure, before the owner's cap of 2 days.
FLOWERS_GARDEN_OVER_2_UNCAPPED_DAYS = 4

RAIN_SOURCE_DISAGREE = "RAIN_SOURCE_DISAGREE"

_RAINY_CONDITIONS = frozenset(
    {"rainy", "pouring", "lightning", "lightning-rainy", "hail", "snowy-rainy"}
)

_GERMINATION_WORDS = (
    "germinat",
    "establishment",
    "new planting",
    "new grass",
    "seedling",
    "new lawn",
    "new turf",
)
_BIRDBATH_WORDS = ("birdbath", "bird bath", "bird-bath", "bird_bath")
_TURF_WORDS = ("lawn", "turf", "grass")
_GARDEN_WORDS = ("flower", "garden", "veggie", "vegetable", "herb", "annual")
_DEEP_WORDS = (
    "tree",
    "citrus",
    "shrub",
    "bush",
    "hedge",
    "drip",
    "oak",
    "lemon",
    "orange",
    "lime",
)

_CATEGORY_TYPE = {
    "lawn": TYPE_TURF,
    "vegetable_garden": TYPE_FLOWERS_GARDEN,
    "bushes": TYPE_DEEP_DRIP,
    "citrus": TYPE_DEEP_DRIP,
    "trees": TYPE_DEEP_DRIP,
}
_PROFILE_TYPE = {
    "grass": TYPE_TURF,
    "flower": TYPE_FLOWERS_GARDEN,
    "tree": TYPE_DEEP_DRIP,
    "shrub": TYPE_DEEP_DRIP,
    "cactus_succulent": TYPE_DEEP_DRIP,
}


def _finite_non_negative(value: float | None) -> float | None:
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
class CombinedRain:
    """72h rainfall after the two-source rule."""

    inches: float | None
    tempest_in: float | None
    meteo_in: float | None
    source: str  # mean | conservative | tempest | open_meteo | none
    disagree: bool
    last_rain_at: datetime | None


def sources_agree(left: float, right: float) -> bool:
    """True when the gap is within max(0.15 in, 40% of the larger reading)."""
    gap = abs(left - right)
    limit = max(AGREE_ABS_IN, AGREE_FRACTION * max(left, right))
    return gap <= limit


def combine_rain_totals(
    tempest_in: float | None,
    meteo_in: float | None,
    *,
    tempest_last_rain_at: datetime | None = None,
    meteo_last_rain_at: datetime | None = None,
) -> CombinedRain:
    """Blend two 72h totals.

    Agreement → arithmetic mean. Disagreement → lower value + 25% of the gap,
    and ``disagree`` is set so the caller can raise RAIN_SOURCE_DISAGREE.
    One missing source → the other, with no disagree flag. Both missing → none.
    """
    tempest = _finite_non_negative(tempest_in)
    meteo = _finite_non_negative(meteo_in)
    anchor = _later(tempest_last_rain_at, meteo_last_rain_at)
    if tempest is None and meteo is None:
        return CombinedRain(None, None, None, "none", False, None)
    if tempest is None:
        return CombinedRain(meteo, None, meteo, "open_meteo", False, meteo_last_rain_at)
    if meteo is None:
        return CombinedRain(tempest, tempest, None, "tempest", False, tempest_last_rain_at)
    if sources_agree(tempest, meteo):
        return CombinedRain((tempest + meteo) / 2.0, tempest, meteo, "mean", False, anchor)
    lower = min(tempest, meteo)
    gap = abs(tempest - meteo)
    return CombinedRain(
        lower + DISAGREE_GAP_FRACTION * gap,
        tempest,
        meteo,
        "conservative",
        True,
        anchor,
    )


def _later(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if left >= right else right


def base_lockout_days(rain_in: float, zone_type: str) -> int | None:
    """Unscaled days for a durable zone type, or None when this type does not hold.

    Birdbath and germination return None: birdbath is an active-rain check,
    germination never holds. Rain below 0.10 in returns None for every type.
    """
    if zone_type not in DURABLE_ZONE_TYPES:
        return None
    rain = _finite_non_negative(rain_in)
    if rain is None or rain < RAIN_EVENT_MIN_INCHES:
        return None
    column = {TYPE_DEEP_DRIP: 3, TYPE_FLOWERS_GARDEN: 4, TYPE_TURF: 5}[zone_type]
    for lo, hi, hi_inclusive, *days in _DAY_TABLE:
        deep, garden, turf = days
        row = (deep, garden, turf)
        if rain < lo:
            continue
        if hi is None:
            if rain > lo:
                return row[column - 3]
            continue
        if hi_inclusive:
            if rain <= hi:
                return row[column - 3]
        elif rain < hi:
            return row[column - 3]
    return None


def eto_scale_factor(daily_eto_in: float | None) -> float:
    """``clamp(0.12 / ETo, 0.5, 1.5)``. Missing or non-positive ETo → 1.0 (no scale)."""
    eto = _finite_non_negative(daily_eto_in)
    if eto is None or eto <= 0:
        return 1.0
    return max(ETO_SCALE_MIN, min(ETO_SCALE_MAX, ETO_SCALE_REFERENCE_IN / eto))


def select_lockout_daily_eto(*candidates: float | None) -> float | None:
    """Hottest positive daily ETo among current and forecast figures.

    Higher ETo shortens the hold (``0.12 / ETo``). Taking the max errs toward
    watering when the forecast is hotter than the figure already on file.
    """
    vals: list[float] = []
    for candidate in candidates:
        number = _finite_non_negative(candidate)
        if number is not None and number > 0:
            vals.append(number)
    return max(vals) if vals else None


def scaled_lockout_hours(
    rain_in: float,
    zone_type: str,
    daily_eto_in: float | None,
    temp_f: float | None,
    *,
    max_hours: int | None = None,
) -> int | None:
    """Day-table hours after ETo scaling, then the existing heat ceiling.

    ``max_hours`` overrides the ceiling (tests). Otherwise
    ``rain_lockout_max_hours(temp_f)`` applies, including the 24h fail-safe
    when temperature is missing.
    """
    days = base_lockout_days(rain_in, zone_type)
    if days is None:
        return None
    raw = days * eto_scale_factor(daily_eto_in) * 24.0
    hours = round(raw)
    ceiling = rain_lockout_max_hours(temp_f) if max_hours is None else int(max_hours)
    return max(1, min(hours, ceiling))


def forecast_should_extend(precip_in_24h: float | None, probability_pct: float | None) -> bool:
    """True when the next 24h shows >= 0.25 in at >= 60% probability.

    Probability may be 0-100 or 0-1. Missing probability never extends: amount
    alone is not a confident forecast.
    """
    amount = _finite_non_negative(precip_in_24h)
    if amount is None or amount < FORECAST_EXTEND_MIN_IN:
        return False
    prob = _finite_non_negative(probability_pct)
    if prob is None:
        return False
    if prob <= 1.0:
        prob *= 100.0
    return prob >= FORECAST_EXTEND_MIN_PROB


def moisture_releases_lockout(current_pct: float | None, target_pct: float) -> bool:
    """Wet soil (at or above target) may end a hold early.

    A dry reading returns False. Callers must not move ``lockout_until`` later
    because the soil is dry — moisture never extends a hold.
    """
    current = _finite_non_negative(current_pct)
    target = _finite_non_negative(target_pct)
    if current is None or target is None:
        return False
    return current >= target


def active_rain(
    weather_conditions: list[str] | tuple[str, ...] | None,
    *,
    precip_rate_in_per_h: float | None = None,
    current_hour_precip_mm: float | None = None,
) -> bool:
    """True only when something says water is falling right now.

    A daily accumulation staying elevated after the storm does not count.
    Unknown data is not active rain (birdbaths keep their zero-day default).
    """
    for condition in weather_conditions or ():
        if str(condition or "").strip().lower() in _RAINY_CONDITIONS:
            return True
    rate = _finite_non_negative(precip_rate_in_per_h)
    if rate is not None and rate > 0:
        return True
    hour_mm = _finite_non_negative(current_hour_precip_mm)
    return hour_mm is not None and hour_mm > 0


def _words(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().lower()


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _majority(values: list[str]) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    winners = [key for key, count in counts.items() if count == best]
    if len(winners) != 1:
        return None
    return winners[0]


def classify_zone_type(
    *,
    zone_entity_id: str = "",
    friendly_name: str | None = None,
    category: str | None = None,
    split_profile: str | None = None,
    plant_presets: list[str] | None = None,
    independent: bool = False,
    schedule_name: str | None = None,
    created_via: str | None = None,
) -> str:
    """Zone type from existing config. Does not invent a stored setting.

    Order, first match wins:

    1. Germination — ``created_via == "establishment"`` or a germination name
       (new planting / new grass / seedling / establishment).
    2. Birdbath — ``independent_zones`` or a birdbath name.
    3. Zone moisture ``category`` (lawn, vegetable_garden, bushes, citrus, trees).
    4. Schedule ``split_profile`` (grass / flower / tree / shrub / cactus).
    5. Majority plant ``care_plan_preset`` on the zone.
    6. Name keywords on the friendly name, schedule name, and entity id.
    7. Default ``turf`` (middle hold). Unclassified zones still lock out;
       turf is neither the longest tree hold nor the shortest garden hold.
    """
    blob = " ".join(_words(part) for part in (friendly_name, schedule_name, zone_entity_id) if part)
    if created_via == "establishment" or _contains_any(blob, _GERMINATION_WORDS):
        return TYPE_GERMINATION
    if independent or _contains_any(blob, _BIRDBATH_WORDS):
        return TYPE_BIRDBATH
    mapped = _CATEGORY_TYPE.get(_words(category))
    if mapped:
        return mapped
    mapped = _PROFILE_TYPE.get(_words(split_profile))
    if mapped:
        return mapped
    mapped = _PROFILE_TYPE.get(_majority(list(plant_presets or ())) or "")
    if mapped:
        return mapped
    if _contains_any(blob, _DEEP_WORDS):
        return TYPE_DEEP_DRIP
    if _contains_any(blob, _GARDEN_WORDS):
        return TYPE_FLOWERS_GARDEN
    if _contains_any(blob, _TURF_WORDS):
        return TYPE_TURF
    return TYPE_TURF


def sum_daily_peaks(
    samples: list[tuple[datetime, float]],
    now: datetime,
    *,
    hours: int = 72,
) -> tuple[float | None, datetime | None]:
    """Sum a midnight-resetting daily rain accumulator over the lookback.

    ``sensor.*_rain_accumulation_today`` climbs through the day and resets at
    midnight. Each local calendar day's contribution is that day's peak, and
    the peaks are summed. ``last_rain_at`` is the time of the peak on the
    latest day whose peak is above zero — the anchor for "days since the rain",
    so a flat daily total cannot push the deadline on every tick.

    Returns ``(None, None)`` when nothing usable falls in the window.
    """
    if not samples or now is None:
        return None, None
    start = now - timedelta(hours=hours)
    peaks: dict[date, float] = {}
    peak_at: dict[date, datetime] = {}
    for ts, raw in samples:
        val = _finite_non_negative(raw)
        if ts is None or val is None:
            continue
        if ts < start or ts > now + timedelta(minutes=1):
            continue
        day = ts.date()
        if day not in peaks or val >= peaks[day]:
            peaks[day] = val
            peak_at[day] = ts
    if not peaks:
        return None, None
    rainy = [day for day, val in peaks.items() if val > 0]
    last_rain_at = peak_at[max(rainy)] if rainy else None
    return sum(peaks.values()), last_rain_at


def _local_naive(moment: datetime) -> datetime:
    """Drop tzinfo for comparison with Open-Meteo's timezone-local timestamps."""
    if moment.tzinfo is None:
        return moment
    return moment.replace(tzinfo=None)


def _restore_tz(moment: datetime, like: datetime) -> datetime:
    if like.tzinfo is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=like.tzinfo)
    return moment


def _parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def parse_open_meteo_rain(payload: Any, now: datetime) -> dict[str, Any]:
    """Normalize an Open-Meteo payload requested with ``past_days=3``.

    Daily ``precipitation_sum`` (mm) over the dates touched by the last 72h
    becomes ``precip_72h_in``. Hourly ``precipitation`` +
    ``precipitation_probability`` over the next 24h feed the one-day extension
    rule. Near-term ET0 (today and tomorrow, the hotter day) and the forecast
    high are returned for the ETo scale and the existing heat ceiling.

    ``now`` is the yard's local time (aware or naive). Open-Meteo timestamps
    are local when the request used that timezone; they are compared naively
    and stamped back onto ``now``'s tzinfo.
    """
    out: dict[str, Any] = {
        "precip_72h_in": None,
        "last_rain_at": None,
        "forecast_24h_in": None,
        "forecast_prob_max": None,
        "forecast_extends": False,
        "forecast_daily_eto_in": None,
        "forecast_high_f": None,
        "current_hour_precip_mm": None,
    }
    if not isinstance(payload, dict) or now is None:
        return out
    local_now = _local_naive(now)
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}

    times = daily.get("time") if isinstance(daily.get("time"), list) else []
    raw_precips = daily.get("precipitation_sum")
    precips = raw_precips if isinstance(raw_precips, list) else []
    et0 = (
        daily.get("et0_fao_evapotranspiration")
        if isinstance(daily.get("et0_fao_evapotranspiration"), list)
        else []
    )
    raw_highs = daily.get("temperature_2m_max")
    highs = raw_highs if isinstance(raw_highs, list) else []

    window_start = local_now - timedelta(hours=72)
    total_mm = 0.0
    saw_precip = False
    last_rain_naive: datetime | None = None
    et0_in: list[float] = []
    high_c: list[float] = []
    today = local_now.date()
    tomorrow = today + timedelta(days=1)
    for index, raw_day in enumerate(times):
        try:
            day = date.fromisoformat(str(raw_day))
        except ValueError:
            continue
        in_window = window_start.date() <= day <= today
        precip_ok = in_window and index < len(precips) and isinstance(precips[index], (int, float))
        if precip_ok:
            mm = float(precips[index])
            if math.isfinite(mm) and mm >= 0:
                total_mm += mm
                saw_precip = True
                if mm > 0:
                    # End of that local day, but never past `now`.
                    end = datetime(day.year, day.month, day.day, 23, 59)
                    if end > local_now:
                        end = local_now
                    if last_rain_naive is None or end > last_rain_naive:
                        last_rain_naive = end
        if day in (today, tomorrow):
            if index < len(et0) and isinstance(et0[index], (int, float)):
                mm = float(et0[index])
                if math.isfinite(mm) and mm > 0:
                    et0_in.append(mm / 25.4)
            if index < len(highs) and isinstance(highs[index], (int, float)):
                temp = float(highs[index])
                if math.isfinite(temp):
                    high_c.append(temp)
    if saw_precip:
        out["precip_72h_in"] = total_mm / 25.4
        if last_rain_naive is not None:
            out["last_rain_at"] = _restore_tz(last_rain_naive, now)
    if et0_in:
        out["forecast_daily_eto_in"] = max(et0_in)
    if high_c:
        out["forecast_high_f"] = max(high_c) * 9.0 / 5.0 + 32.0

    h_times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    h_prec = hourly.get("precipitation") if isinstance(hourly.get("precipitation"), list) else []
    h_prob = (
        hourly.get("precipitation_probability")
        if isinstance(hourly.get("precipitation_probability"), list)
        else []
    )
    hour_floor = local_now.replace(minute=0, second=0, microsecond=0)
    horizon = local_now + timedelta(hours=24)
    sum_mm = 0.0
    saw_hour = False
    max_prob: float | None = None
    for index, raw_hour in enumerate(h_times):
        stamp = _parse_stamp(str(raw_hour))
        if stamp is None:
            continue
        stamp = _local_naive(stamp)
        mm = None
        if index < len(h_prec) and isinstance(h_prec[index], (int, float)):
            candidate = float(h_prec[index])
            if math.isfinite(candidate) and candidate >= 0:
                mm = candidate
        # Hour timestamps are the start of the hour. Keep the one that contains now.
        if (
            mm is not None
            and hour_floor <= stamp <= local_now + timedelta(hours=1)
            and stamp <= local_now < stamp + timedelta(hours=1)
        ):
            out["current_hour_precip_mm"] = mm
        if not (hour_floor <= stamp < horizon):
            continue
        if mm is not None:
            sum_mm += mm
            saw_hour = True
        if index < len(h_prob) and isinstance(h_prob[index], (int, float)):
            prob = float(h_prob[index])
            if math.isfinite(prob) and prob >= 0:
                max_prob = prob if max_prob is None else max(max_prob, prob)
    if saw_hour:
        out["forecast_24h_in"] = sum_mm / 25.4
    out["forecast_prob_max"] = max_prob
    out["forecast_extends"] = forecast_should_extend(out["forecast_24h_in"], max_prob)
    return out


@dataclass
class LockoutState:
    """Persisted rain-lockout snapshot. Its own store file, never the schedules file."""

    lockout_until: dict[str, datetime] = field(default_factory=dict)
    event_rain_in: float | None = None
    event_anchor: datetime | None = None
    released_zones: set[str] = field(default_factory=set)
    forecast_extended_on: dict[str, str] = field(default_factory=dict)
    rain_source_disagree: bool = False
    last_tempest_in: float | None = None
    last_open_meteo_in: float | None = None
    last_combined_in: float | None = None
    rain_source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lockout_until": {
                zone_type: moment.isoformat()
                for zone_type, moment in self.lockout_until.items()
                if zone_type in DURABLE_ZONE_TYPES and isinstance(moment, datetime)
            },
            "event_rain_in": self.event_rain_in,
            "event_anchor": self.event_anchor.isoformat() if self.event_anchor else None,
            "released_zones": sorted(self.released_zones),
            "forecast_extended_on": {
                zone_type: day
                for zone_type, day in self.forecast_extended_on.items()
                if zone_type in DURABLE_ZONE_TYPES and isinstance(day, str)
            },
            "rain_source_disagree": bool(self.rain_source_disagree),
            "last_tempest_in": self.last_tempest_in,
            "last_open_meteo_in": self.last_open_meteo_in,
            "last_combined_in": self.last_combined_in,
            "rain_source": self.rain_source or "none",
        }

    @classmethod
    def from_dict(cls, raw: Any) -> LockoutState:
        """Tolerant load. A missing file, a partial dict, or a bad timestamp
        must not take setup down — those fields just stay empty."""
        if not isinstance(raw, dict):
            return cls()
        until: dict[str, datetime] = {}
        stored_until = raw.get("lockout_until")
        if isinstance(stored_until, dict):
            for zone_type, text in stored_until.items():
                if zone_type not in DURABLE_ZONE_TYPES or not isinstance(text, str):
                    continue
                try:
                    until[zone_type] = datetime.fromisoformat(text)
                except ValueError:
                    continue
        anchor = None
        anchor_text = raw.get("event_anchor")
        if isinstance(anchor_text, str):
            try:
                anchor = datetime.fromisoformat(anchor_text)
            except ValueError:
                anchor = None
        released: set[str] = set()
        if isinstance(raw.get("released_zones"), list):
            released = {str(zone) for zone in raw["released_zones"] if zone}
        extended: dict[str, str] = {}
        if isinstance(raw.get("forecast_extended_on"), dict):
            for zone_type, day in raw["forecast_extended_on"].items():
                if zone_type in DURABLE_ZONE_TYPES and isinstance(day, str):
                    extended[zone_type] = day
        event_rain = _finite_non_negative(raw.get("event_rain_in"))
        return cls(
            lockout_until=until,
            event_rain_in=event_rain,
            event_anchor=anchor,
            released_zones=released,
            forecast_extended_on=extended,
            rain_source_disagree=bool(raw.get("rain_source_disagree")),
            last_tempest_in=_finite_non_negative(raw.get("last_tempest_in")),
            last_open_meteo_in=_finite_non_negative(raw.get("last_open_meteo_in")),
            last_combined_in=_finite_non_negative(raw.get("last_combined_in")),
            rain_source=str(raw.get("rain_source") or "none"),
        )
