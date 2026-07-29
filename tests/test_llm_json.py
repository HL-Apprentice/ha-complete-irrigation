"""Tolerant LLM JSON extraction (v1.62).

Two paths used to carry different extractors: the plant-ID path had learned to
repair what small models actually emit, while the scheduling path shipped a
bare find("{")..rfind("}") with no repair. A parse failure there is silent — the
advisor proposes nothing, which looks exactly like "no conflicts found". Now
both use this one implementation, so these cases hold for both.
"""

from __future__ import annotations

from custom_components.complete_irrigation.llm_json import extract_json_object


def test_plain_object():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_prose_before_and_after():
    assert extract_json_object('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_code_fence():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_line_comments_are_repaired():
    # The scheduling path returned None for this — a silent no-op.
    out = extract_json_object('{\n  // the reason\n  "a": 1\n}')
    assert out == {"a": 1}


def test_trailing_comma_is_repaired():
    assert extract_json_object('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_echoed_annotation_after_a_number_is_repaired():
    out = extract_json_object('{"fertilize_every_days": 30 (0 = not necessary)}')
    assert out == {"fertilize_every_days": 30}


def test_nested_objects_survive_the_repair():
    out = extract_json_object('{"items": [{"type": "shift", "start": "03:30"}], "summary": "x"}')
    assert out["items"][0]["type"] == "shift"
    assert out["summary"] == "x"


def test_repair_never_breaks_already_valid_json():
    # A URL contains "//" — the comment-stripper must not corrupt a valid reply,
    # because the unrepaired span is tried as a fallback.
    src = '{"url": "https://example.com/a", "n": 1}'
    assert extract_json_object(src) == {"url": "https://example.com/a", "n": 1}


def test_no_object_returns_none():
    for text in ("", "no json here", "[1, 2, 3]", "}{"):
        assert extract_json_object(text) is None


def test_non_string_input_returns_none():
    for bad in (None, 42, [], {}, object()):
        assert extract_json_object(bad) is None


def test_a_top_level_array_yields_its_inner_object():
    # Documented consequence of the find("{")..rfind("}") span: an array wrapper
    # is stripped and the inner object is returned. Asserted so the behaviour is
    # pinned rather than accidental.
    assert extract_json_object('[{"a": 1}]') == {"a": 1}


def test_truncated_output_returns_none_rather_than_partial():
    assert extract_json_object('{"a": 1, "b":') is None


def test_both_call_sites_share_this_implementation():
    # The whole point of v1.62: one extractor, not two that drift.
    from custom_components.complete_irrigation.schedule_review import _extract_json_obj
    from custom_components.complete_irrigation.species_id import extract_suggestion_json

    tricky = '{\n // note\n "a": 1,\n}'
    assert _extract_json_obj(tricky) == {"a": 1}
    assert extract_suggestion_json(tricky) == {"a": 1}
