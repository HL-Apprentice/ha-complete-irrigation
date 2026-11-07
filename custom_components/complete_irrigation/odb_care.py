"""Merge Outdoor Dream Builder pages into the operational care table.

The page file stays verbatim. This module derives the fields lookup_care
needs. Outdoor Dream Builder water classes are that site's own labels, mapped
onto the keys WUCOLS_FACTORS already uses. They are not the UC WUCOLS dataset.

USDA Plant Hardiness Zone Map (USDA Agricultural Research Service), average
annual extreme minimum temperature. Each full zone is 10 F wide and zone 1
starts at -60 F, so the low end of zone N is -60 + (N - 1) * 10. Zone 9 is
20 F. Half-zones split a zone into 5 F steps (9a is 20 to 25, 9b is 25 to 30).
Quick Stats on these pages are whole zones, and the canonical low is the start
of the lowest numbered zone.

https://planthardiness.ars.usda.gov/
"""

from __future__ import annotations

import re
from typing import Any

from .care_tasks import CARE_PLAN_PRESETS
from .hydraulics import WUCOLS_FACTORS
from .reference_plants import load_reference
from .species_id import SUNLIGHT_LUX

# ODB's own water class, not a WUCOLS publication category.
ODB_WATER_TO_WUCOLS: dict[str, str] = {
    "Very Low (Xeric)": "very_low",
    "Low": "low",
    "Moderate": "moderate",
    "High": "high",
}

# ODB sun wording -> the sunlight enum species_id already accepts.
# "partial shade" is the partial-sun class (some direct sun), not deep shade.
# "any" has no narrower class; full_sun is the open-desert class and is flagged.
ODB_SUN_TO_ENUM: dict[str, str] = {
    "full sun": "full_sun",
    "partial shade": "partial_sun",
    "any": "full_sun",
}

# ODB library category -> an existing care-plan preset.
# Palms are trees in the curated table. Vines follow bougainvillea (shrub).
# Perennials, annuals, and groundcovers use the flower plan.
# "accent" on these pages is the architectural desert group; the curated
# members of that group (ocotillo, desert spoon) are cactus_succulent.
ODB_CATEGORY_TO_PRESET: dict[str, str] = {
    "tree": "tree",
    "palm": "tree",
    "shrub": "shrub",
    "vine": "shrub",
    "cactus": "cactus_succulent",
    "succulent": "cactus_succulent",
    "accent": "cactus_succulent",
    "perennial": "flower",
    "annual": "flower",
    "groundcover": "flower",
    "grass": "grass",
}

# Most common water_days already shipped for that preset, counted on the
# original curated rows only. Flower's 4-day and 7-day cadences tie; the
# shorter one is kept so a fallback does not water less often than half of
# those rows. Grass had no curated cadence; weekly is the flagged fallback.
PRESET_DEFAULT_WATER_DAYS: dict[str, int] = {
    "tree": 10,
    "shrub": 10,
    "flower": 4,
    "cactus_succulent": 30,
    "grass": 7,
}

# Lowest temp_high already used for that preset. A page that does not state a
# heat ceiling does not get a higher one. Grass had no curated heat figure;
# 100 F is the cactus and tree floor.
PRESET_DEFAULT_TEMP_HIGH_F: dict[str, int] = {
    "tree": 100,
    "shrub": 95,
    "flower": 95,
    "cactus_succulent": 100,
    "grass": 100,
}

# Fertilizer interval from CARE_PLAN_PRESETS (the fertilize task's days).
PRESET_DEFAULT_FERT_DAYS: dict[str, int] = {
    preset: next(days for kind, days in tasks if kind == "fertilize")
    for preset, tasks in CARE_PLAN_PRESETS.items()
}

# Zone number -> low end of that zone's average annual extreme minimum, F.
USDA_ZONE_MIN_F: dict[int, int] = {zone: -60 + (zone - 1) * 10 for zone in range(1, 14)}

_MAX_NOTE = 240
_ZONE_RANGE = re.compile(r"USDA\s+(\d{1,2})[ab]?(?:\s*[-\u2013]\s*(\d{1,2}))?", re.IGNORECASE)
_NUM_WORDS = (
    ("twelve", "12"),
    ("ten", "10"),
    ("nine", "9"),
    ("eight", "8"),
    ("seven", "7"),
    ("six", "6"),
    ("five", "5"),
    ("four", "4"),
    ("three", "3"),
    ("two", "2"),
    ("one", "1"),
)
_INTERVAL = re.compile(
    r"every\s+(\d+)\s*(?:-|to)\s*(\d+)\s*(weeks?|days?)"
    r"|every\s+(\d+)\s*(weeks?|days?)"
    r"|every\s+other\s+week"
    r"|once or twice a month|twice a month"
    r"|twice\s+(?:a\s+week|weekly)"
    r"|once\s+a\s+month|once\s+a\s+week"
    r"|(?<![a-z])monthly|(?<![a-z])weekly|(?<![a-z])daily"
)
_HOT = re.compile(
    r"\bsummer\b|\bheat\b|\bhot\b|warm season|warm months|hottest|spring through fall"
)
_COOL = re.compile(r"\bwinter\b|\bcool|cold season")
_ESTABLISHING = re.compile(
    r"first (?:summer|year|spring)|while young|to establish|while establishing|"
    r"newly planted|new transplant|young plant|young transplant"
)
_NONE_FERT = re.compile(r"no fertilizer|not necessary|not needed|never fertil|none needed|no feed")


def usda_zone_minimum_f(label: str) -> int | None:
    """Low end, in F, of the lowest USDA zone named in a Quick Stats label.

    "USDA 9-11" is zone 9, which is 20 F. Returns None when no zone is named.
    """
    match = _ZONE_RANGE.search(label or "")
    if not match:
        return None
    zone = int(match.group(1))
    if zone not in USDA_ZONE_MIN_F:
        return None
    return USDA_ZONE_MIN_F[zone]


def _with_digits(text: str) -> str:
    out = text.lower().replace("\u2013", "-").replace("\u2014", "-")
    for word, num in _NUM_WORDS:
        out = re.sub(rf"\b{word}\b", num, out)
    return out


def _interval_days(match: re.Match[str]) -> int:
    if match.group(1):
        span = min(int(match.group(1)), int(match.group(2)))
        return span * 7 if match.group(3).startswith("week") else span
    if match.group(4):
        count = int(match.group(4))
        return count * 7 if match.group(5).startswith("week") else count
    text = match.group(0)
    if text in ("monthly", "once a month"):
        return 30
    if "month" in text:
        return 15
    if text.startswith("twice"):
        return 3
    if "other" in text:
        return 14
    if text in ("weekly", "once a week"):
        return 7
    if text == "daily":
        return 1
    raise ValueError(text)


def hot_season_water_days(sentence: str) -> int | None:
    """Days between waterings in the hot season, or None when it is not stated.

    A range uses the shorter end ("every 2-3 weeks" is 14). Intervals tied to
    winter or the cool season do not count. An establishment-only interval
    (first summer, while young) does not become the ongoing cadence when no
    established hot-season number is stated.
    """
    if not sentence or not isinstance(sentence, str):
        return None
    text = _with_digits(sentence)
    matches = list(_INTERVAL.finditer(text))
    usable: list[int] = []
    for index, match in enumerate(matches):
        days = _interval_days(match)
        if not 1 <= days <= 60:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # Modifiers that belong to this interval follow it ("every 2 weeks in
        # summer", "weekly through the first year"). Words before it belong to
        # the previous interval.
        after = text[match.end() : end]
        cool = _COOL.search(after)
        hot = _HOT.search(after)
        if cool and (hot is None or cool.start() < hot.start()):
            continue
        if _ESTABLISHING.search(after):
            continue
        usable.append(days)
    if not usable:
        return None
    return min(usable)


def _fert_days(text: str, preset: str) -> int:
    """Page fertilizer interval, or the preset's fertilize cadence."""
    lowered = (text or "").lower()
    if _NONE_FERT.search(lowered):
        return 0
    if re.search(r"two to three|2-3 times|3 times|twice a year", lowered):
        return 365 // 3
    if re.search(r"spring|once a year|annual", lowered):
        return 365
    return PRESET_DEFAULT_FERT_DAYS[preset]


def _note(sentence: str) -> str:
    text = " ".join(sentence.split())
    if len(text) <= _MAX_NOTE:
        return text
    return text[:_MAX_NOTE].rstrip()


def _candidate(page: dict[str, Any]) -> dict[str, Any] | None:
    """One operational care record, or None when a required fact will not map."""
    wucols = ODB_WATER_TO_WUCOLS.get(page.get("water_needs"))
    sun = ODB_SUN_TO_ENUM.get(page.get("sun"))
    preset = ODB_CATEGORY_TO_PRESET.get(page.get("category"))
    temp_low = usda_zone_minimum_f(str(page.get("usda_zone") or ""))
    guidance = page.get("watering_guidance")
    if (
        wucols not in WUCOLS_FACTORS
        or sun not in SUNLIGHT_LUX
        or preset not in CARE_PLAN_PRESETS
        or temp_low is None
        or not isinstance(guidance, str)
        or not guidance.strip()
    ):
        return None
    defaulted: list[str] = []
    heat = page.get("heat_tolerance_f")
    if isinstance(heat, int) and temp_low < heat <= 140:
        temp_high = heat
    else:
        temp_high = PRESET_DEFAULT_TEMP_HIGH_F[preset]
        defaulted.append("temp_high")
    if not temp_low < temp_high:
        return None
    parsed = hot_season_water_days(guidance)
    if parsed is None:
        water_days = PRESET_DEFAULT_WATER_DAYS[preset]
        defaulted.append("water_days")
    else:
        water_days = parsed
    if page.get("sun") == "any":
        defaulted.append("sun")
    botanical = str(page["botanical_name"]).strip()
    return {
        "key": " ".join(botanical.casefold().split()),
        "common": str(page["common_name"]).strip(),
        "wucols": wucols,
        "sun": sun,
        "temp_low": temp_low,
        "temp_high": temp_high,
        "preset": preset,
        "water_days": water_days,
        "fert_days": _fert_days(str(page.get("fertilizer_guidance") or ""), preset),
        "note": _note(guidance),
        "source": page["source_url"],
        "defaulted": tuple(defaulted),
    }


def _care_row(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "common": candidate["common"],
        "wucols": candidate["wucols"],
        "sun": candidate["sun"],
        "temp_low": candidate["temp_low"],
        "temp_high": candidate["temp_high"],
        "preset": candidate["preset"],
        "water_days": candidate["water_days"],
        "fert_days": candidate["fert_days"],
        "note": candidate["note"],
        "source": candidate["source"],
        "defaulted": candidate["defaulted"],
    }


def merge_odb_care(
    base: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Copy base and apply ODB pages.

    A botanical name that is already a key is an existing species row. Its
    water_days moves only when the page interval is shorter. Its temp_low
    becomes the USDA zone minimum. Other fields on that row stay as they are.
    A longer interval is not applied. A name that is not already a key is a
    new species row. Genus keys that are only a prefix of the botanical name
    are left untouched.
    """
    care = {key: dict(row) for key, row in base.items()}
    report: dict[str, Any] = {
        "added": [],
        "changed": [],
        "not_applied_would_reduce": [],
        "defaulted_temp_high": [],
        "defaulted_water_days": [],
        "skipped": [],
    }
    for page in load_reference():
        candidate = _candidate(page)
        if candidate is None:
            report["skipped"].append(page.get("source_url"))
            continue
        key = candidate["key"]
        if key in base:
            current = care[key]
            original = base[key]
            applied = False
            if candidate["water_days"] < original["water_days"]:
                current["water_days"] = candidate["water_days"]
                report["changed"].append(
                    {
                        "key": key,
                        "common": original["common"],
                        "field": "water_days",
                        "before": original["water_days"],
                        "after": candidate["water_days"],
                    }
                )
                applied = True
                if "water_days" in candidate["defaulted"]:
                    report["defaulted_water_days"].append(key)
            elif candidate["water_days"] > original["water_days"]:
                report["not_applied_would_reduce"].append(
                    {
                        "key": key,
                        "common": original["common"],
                        "water_days": original["water_days"],
                        "odb_water_days": candidate["water_days"],
                    }
                )
            if candidate["temp_low"] != original["temp_low"]:
                current["temp_low"] = candidate["temp_low"]
                report["changed"].append(
                    {
                        "key": key,
                        "common": original["common"],
                        "field": "temp_low",
                        "before": original["temp_low"],
                        "after": candidate["temp_low"],
                    }
                )
                applied = True
            if applied:
                current["source"] = candidate["source"]
                flags = []
                if current["water_days"] != original["water_days"] and (
                    "water_days" in candidate["defaulted"]
                ):
                    flags.append("water_days")
                if flags:
                    current["defaulted"] = tuple(flags)
            continue
        care[key] = _care_row(candidate)
        report["added"].append(key)
        if "temp_high" in candidate["defaulted"]:
            report["defaulted_temp_high"].append(key)
        if "water_days" in candidate["defaulted"]:
            report["defaulted_water_days"].append(key)
    return care, report


def odb_name_aliases(care: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """ODB common names that should resolve, when they belong to one row.

    Existing rows keep their original common name. "Texas Sage" still has to
    find the Texas Ranger row. A name shared by two rows is left unset.
    """
    owned: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for rec in care.values():
        name = str(rec.get("common") or "").strip().casefold()
        if not name:
            continue
        prior = owned.get(name)
        if prior is None:
            owned[name] = rec
        elif prior is not rec:
            ambiguous.add(name)
    aliases: dict[str, dict[str, Any]] = {}
    for page in load_reference():
        key = " ".join(str(page.get("botanical_name") or "").strip().casefold().split())
        rec = care.get(key)
        if rec is None:
            continue
        alias = str(page.get("common_name") or "").strip().casefold()
        if not alias or alias in care or alias in ambiguous:
            continue
        if alias in owned and owned[alias] is not rec:
            continue
        aliases[alias] = rec
    return aliases
