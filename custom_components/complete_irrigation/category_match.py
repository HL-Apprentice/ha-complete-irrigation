"""Pure-logic helpers for plant-category matching (PRD #20).

Used by config_flow to auto-suggest a category based on the zone's name
during setup. Kept HA-free so it's unit-testable.
"""

from __future__ import annotations

# Order matters: more specific keywords first when they could overlap.
# Each tuple is (substring-to-match-lowercase, category-id).
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("lemon", "citrus"),
    ("orange", "citrus"),
    ("lime", "citrus"),
    ("citrus", "citrus"),
    ("oak", "trees"),
    ("tree", "trees"),
    ("lawn", "lawn"),
    ("grass", "lawn"),
    ("turf", "lawn"),
    ("veg", "vegetable_garden"),
    ("tomato", "vegetable_garden"),
    ("herb", "vegetable_garden"),
    ("garden", "vegetable_garden"),
    ("bush", "bushes"),
    ("shrub", "bushes"),
    ("hedge", "bushes"),
]


def suggest_category(zone_name: str) -> str | None:
    """Suggest a plant category from a zone name. Returns None if no
    keyword matches."""
    if not zone_name:
        return None
    lower = zone_name.lower()
    for kw, cat in _CATEGORY_KEYWORDS:
        if kw in lower:
            return cat
    return None
