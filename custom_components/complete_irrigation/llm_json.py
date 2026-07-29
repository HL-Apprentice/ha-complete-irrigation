"""Tolerant JSON extraction from an LLM reply (v1.62).

Small models reliably emit *almost* JSON: a code fence, a sentence of preamble,
a `//` comment, a trailing comma, an annotation echoed back after a number. The
plant-identification path learned this the hard way and grew a repair pass. The
newer scheduling path shipped its own weaker copy, so the same model quirk that
was already handled in one place silently returned "no advice" in the other.

One implementation, used by both. Pure + HA-free.
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: Any) -> dict | None:
    """The first JSON OBJECT in ``text``, repaired, or None if there isn't one.

    Repairs, in order:
      * prose / code fences / a leading label around the object,
      * ``//`` line comments,
      * a prompt annotation echoed after a value (``30 (0 = not necessary)``),
      * trailing commas before a closing brace or bracket.

    The unrepaired span is tried as a fallback, so a reply that was already
    valid can never be broken by the repair pass.
    """
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    original = text[start : end + 1]
    repaired = re.sub(r"//[^\n\r]*", "", original)  # line comments
    # `<value> (annotation)` after a number -> `<value>` (an observed failure)
    repaired = re.sub(r"([:,]\s*-?\d+(?:\.\d+)?)\s*\([^)]*\)", r"\1", repaired)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)  # trailing commas
    for candidate in (repaired, original):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
