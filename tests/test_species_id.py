"""Repair-aware extraction of a vision model's species reply (v1.40.8).

Small local vision models (e.g. qwen2.5vl:7b) don't always emit strict JSON:
they wrap it in prose/fences, echo the prompt's inline hints into a value, or
leave trailing commas. extract_suggestion_json repairs those so a near-miss
still identifies the plant, and validate_suggestion bounds the result.
"""

from __future__ import annotations

from custom_components.complete_irrigation.species_id import (
    extract_suggestion_json,
    validate_suggestion,
)

# The ACTUAL reply qwen2.5vl:7b returned for the oleander photo (invalid JSON:
# it copied the prompt annotation "(0 = not necessary)" into the value, and
# echoed the pipe list for sunlight_class). Before the fix this failed to parse.
_BROKEN_QWEN = """{
  "species": "Euphorbia lathyris",
  "common_name": "Devil's Milkweed",
  "confidence": 0.7,
  "sunlight_class": "full_sun|bright_shade",
  "temp_low_f": -15,
  "temp_high_f": 120,
  "wucols_category": "low",
  "care_plan_preset": "cactus_succulent",
  "water_every_days": 7,
  "fertilize_every_days": 30 (0 = not necessary),
  "canopy_area_sqft": 5.0,
  "note": "Tolerates hot and arid climates well."
}"""


def test_repairs_the_real_broken_qwen_reply():
    obj = extract_suggestion_json(_BROKEN_QWEN)
    assert obj is not None  # was None before the repair layer
    assert obj["species"] == "Euphorbia lathyris"
    assert obj["fertilize_every_days"] == 30  # annotation stripped, value kept
    sug = validate_suggestion(obj, model="qwen2.5vl:7b", now_iso="2026-07-12T00:00:00")
    assert sug is not None
    assert sug["species"] == "Euphorbia lathyris"
    assert sug["sunlight_class"] == "full_sun"  # first token of the echoed pipe list
    assert sug["fertilize_every_days"] == 30
    assert sug["care_plan_preset"] == "cactus_succulent"


def test_strips_prose_and_code_fences():
    assert extract_suggestion_json('```json\n{"species":"Rosa"}\n```') == {"species": "Rosa"}
    assert extract_suggestion_json('Sure! {"species":"Rosa"} ok') == {"species": "Rosa"}


def test_strips_trailing_commas_and_line_comments():
    assert extract_suggestion_json('{"species":"Rosa",}') == {"species": "Rosa"}
    assert extract_suggestion_json('{"species":"Rosa" // a note\n}') == {"species": "Rosa"}


def test_none_on_unusable_input():
    assert extract_suggestion_json("no json at all") is None
    assert extract_suggestion_json("") is None
    assert extract_suggestion_json(None) is None
    assert extract_suggestion_json("[1,2,3]") is None  # not an object


def test_research_reply_by_name_validates_without_canopy():
    # v1.40.9 — a by-NAME research reply (no photo, so no canopy) validates with
    # the full care attribute set. This is the exact clean reply qwen2.5vl:7b gave
    # for "Nerium oleander".
    reply = (
        '{"species":"Nerium oleander","common_name":"Oleander","confidence":0.9,'
        '"sunlight_class":"full_sun","temp_low_f":20,"temp_high_f":110,'
        '"wucols_category":"moderate","care_plan_preset":"shrub","water_every_days":7,'
        '"fertilize_every_days":0,"note":"tolerates heat and drought well"}'
    )
    sug = validate_suggestion(
        extract_suggestion_json(reply), model="qwen2.5vl:7b", now_iso="2026-07-12T00:00:00"
    )
    assert sug["species"] == "Nerium oleander"
    assert sug["common_name"] == "Oleander"
    assert sug["wucols_category"] == "moderate"
    assert sug["care_plan_preset"] == "shrub"
    assert (sug["temp_low_f"], sug["temp_high_f"]) == (20, 110)
    assert sug["canopy_area_sqft"] is None  # by-name research never sets canopy


def test_pipe_enum_first_token_across_all_enums():
    obj = {
        "species": "Test",
        "sunlight_class": "partial_sun|deep_shade",
        "wucols_category": "moderate|high",
        "care_plan_preset": "tree|shrub",
    }
    sug = validate_suggestion(obj, model="m", now_iso="2026-01-01T00:00:00")
    assert sug["sunlight_class"] == "partial_sun"
    assert sug["wucols_category"] == "moderate"
    assert sug["care_plan_preset"] == "tree"
