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
DEFAULT_MANUAL_RUN_MINUTES = 10

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
CONFLICT_DEFER_NEW = "defer_new"          # Option A
CONFLICT_SHIFT_EXISTING = "shift_existing"  # Option B
CONFLICT_SPLIT_DIFFERENCE = "split_difference"  # Option C
