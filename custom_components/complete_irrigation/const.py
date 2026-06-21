"""Constants for HA Complete Irrigation Integration."""

from __future__ import annotations

DOMAIN = "complete_irrigation"
NAME = "HA Complete Irrigation Integration"

# Known irrigation integrations we auto-detect at config time.
# Maps HA integration domain -> display name shown in setup.
KNOWN_IRRIGATION_DOMAINS: dict[str, str] = {
    "rachio": "Rachio (official)",
    "rachio_local": "Rachio Local (biofects)",
    "hydrawise": "Hunter Hydrawise",
    "rainmachine": "RainMachine",
    "bhyve": "Orbit B-Hyve",
    "opensprinkler": "OpenSprinkler",
    "smart_irrigation": "Smart Irrigation",
}

# Plant categories — defaults per the spec.
# Min / target / max moisture %, with info text for the help tooltip.
PLANT_CATEGORIES: dict[str, dict] = {
    "lawn": {
        "name": "Lawn",
        "min": 21,
        "target": 31,
        "max": 40,
        "info": (
            "The best moisture content for grass at a depth of 3 to 4 inches "
            "is typically around 21% to 40%. This range helps ensure that the "
            "grass has enough water to thrive without becoming waterlogged."
        ),
    },
    "bushes": {
        "name": "Bushes",
        "min": 21,
        "target": 41,
        "max": 60,
        "info": (
            "The ideal moisture content for bushes at a depth of 3-4 inches is "
            "generally between 21% and 60%. This range helps ensure healthy "
            "growth and prevents stress on the plants."
        ),
    },
    "vegetable_garden": {
        "name": "Vegetable garden",
        "min": 41,
        "target": 61,
        "max": 80,
        "info": (
            "The best moisture content for a vegetable garden is between 41% "
            "and 80% at a depth of 3-4 inches. This range ensures that "
            "vegetables receive adequate water for healthy growth."
        ),
    },
    "citrus": {
        "name": "Citrus",
        "min": 21,
        "target": 31,
        "max": 40,
        "info": (
            "The optimal soil moisture level for citrus plants is between 21% "
            "and 40%. Maintaining this range helps ensure healthy growth and "
            "prevents issues like root rot or drought stress."
        ),
    },
    "trees": {
        "name": "Trees",
        "min": 21,
        "target": 31,
        "max": 40,
        "info": (
            "The best moisture content in the soil for trees at a depth of "
            "3-4 inches is typically around 21% to 40%. This range helps "
            "ensure that trees receive adequate moisture without becoming "
            "waterlogged."
        ),
    },
}

# Default conflict resolution policy and timings.
DEFAULT_BUFFER_MINUTES = 2
DEFAULT_QUIET_HOURS_START = "22:00"
DEFAULT_QUIET_HOURS_END = "07:00"
DEFAULT_SMALL_SHIFT_TOLERANCE_MIN = 15
DEFAULT_CASCADE_CAP_HOURS = 2
DEFAULT_CASCADE_DEFER_LIMIT = 3
# v1.20 — never-drop scheduling. When serializing one-zone-at-a-time would
# push a run more than the cascade cap past its scheduled start, the resolver
# COMPRESSES the runs ahead of it (down to this fraction of their requested
# minutes) to reclaim time instead of dropping anything. A run is never
# skipped — at worst it runs late. 70 = a run may be shortened to at most 30%
# below its requested duration before the resolver prefers deferring it late.
DEFAULT_COMPRESS_FLOOR_PCT = 70
DEFAULT_MANUAL_RUN_MINUTES = 10
# (v1.24 removed MAX_MANUAL_RUN_MINUTES=60 — it was wrongly applied to scheduled
#  runs via run_zone, silently blocking every schedule over 60 min. The single
#  absolute cap is now MAX_SCHEDULE_DURATION_MIN=480 in services.py.)

# Per-zone moisture defaults when the user hasn't configured min/target/max
# (matches the "lawn" preset in PLANT_CATEGORIES above). Used by the
# coordinator's moisture gate when a zone has bound sensors but no
# explicit thresholds.
DEFAULT_ZONE_MIN_PCT = 21
DEFAULT_ZONE_TARGET_PCT = 31
DEFAULT_ZONE_MAX_PCT = 40

# v1.18 — 12-color preset palette for schedule color-coding. Hex values
# chosen for good contrast on both the light and dark panel themes and
# distinct from each other for colorblind-reasonable separation. The
# panel restricts the picker to these; the model accepts any of them
# (or None = no color / use the default accent).
SCHEDULE_COLOR_PALETTE = (
    "#e53935",  # red
    "#fb8c00",  # orange
    "#fdd835",  # yellow
    "#43a047",  # green
    "#00acc1",  # cyan
    "#1e88e5",  # blue
    "#3949ab",  # indigo
    "#8e24aa",  # purple
    "#d81b60",  # pink
    "#6d4c41",  # brown
    "#546e7a",  # blue-grey
    "#00897b",  # teal
)

# Hot weather adjustment defaults (in user's unit; converted internally).
DEFAULT_HOT_WEATHER_F = 100
DEFAULT_HOT_WEATHER_BOOST_PCT = 25

# Rain lockout tiers — (rain_inches_lower_bound, lockout_hours).
DEFAULT_RAIN_LOCKOUT_TIERS: list[tuple[float, int]] = [
    (0.10, 4),
    (0.25, 6),
    (0.50, 12),
    (1.00, 24),
]

# New grass establishment defaults.
DEFAULT_NEW_GRASS_CYCLES_PER_DAY = 3
DEFAULT_NEW_GRASS_MINUTES_PER_CYCLE = 10
DEFAULT_NEW_GRASS_DAYS = 12

# Sensor combine modes for zones with multiple moisture sensors.
COMBINE_AVERAGE = "average"
COMBINE_LOWEST = "lowest"
COMBINE_HIGHEST = "highest"
COMBINE_PRIMARY = "primary"

# Conflict resolution policies.
CONFLICT_DEFER_NEW = "defer_new"  # Option A
CONFLICT_SHIFT_EXISTING = "shift_existing"  # Option B
CONFLICT_SPLIT_DIFFERENCE = "split_difference"  # Option C
