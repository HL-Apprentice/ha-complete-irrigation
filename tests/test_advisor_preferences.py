"""Standing advisor guidance (v1.61) — user_preferences_block.

Declining a suggestion has no memory of its own: without persisted guidance the
next nightly run proposes the very same thing again. These lock in that the
user's own words reach the prompt, outrank the model's optimisation instincts,
and cannot grow without bound.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.complete_irrigation.schedule_review import user_preferences_block


def _coord(prefs):
    return SimpleNamespace(config={"advisor_preferences": prefs} if prefs is not None else {})


def test_no_preferences_adds_nothing_to_the_prompt():
    assert user_preferences_block(_coord(None)) == ""
    assert user_preferences_block(_coord([])) == ""


def test_preference_text_reaches_the_prompt():
    block = user_preferences_block(
        _coord([{"text": "Never move Trees earlier - watered late on purpose"}])
    )
    assert "Never move Trees earlier - watered late on purpose" in block
    assert block.lstrip().startswith("THE USER HAS TOLD YOU")


def test_block_tells_the_model_the_user_outranks_it():
    # The whole point: a suggestion the user rejected must not come back, even
    # when the model thinks it is an improvement.
    block = user_preferences_block(_coord([{"text": "Keep the bird bath at 10am"}]))
    assert "do NOT re-propose" in block
    assert "even when" in block


def test_every_preference_is_listed_as_its_own_bullet():
    block = user_preferences_block(_coord([{"text": "one"}, {"text": "two"}, {"text": "three"}]))
    assert block.count("\n- ") == 3
    for t in ("one", "two", "three"):
        assert f"- {t}" in block


def test_blank_and_malformed_entries_are_skipped_not_crashed():
    block = user_preferences_block(
        _coord([{"text": "  "}, {"nope": 1}, "not-a-dict", {"text": "real one"}, None])
    )
    assert "real one" in block
    assert block.count("\n- ") == 1  # only the usable one


def test_prompt_growth_is_bounded():
    # Every preference is pasted into every advisor prompt, so an unbounded list
    # would quietly eat the model's context.
    block = user_preferences_block(_coord([{"text": f"rule {i}"} for i in range(60)]))
    assert block.count("\n- ") == 20
    assert "rule 59" in block  # newest guidance is the guidance that survives
    assert "rule 0" not in block


def test_missing_config_key_is_safe():
    assert user_preferences_block(SimpleNamespace(config={})) == ""
