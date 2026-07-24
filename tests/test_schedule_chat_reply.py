"""Chat reply confirmation footer (v1.58.3) — augment_chat_reply.

The chat must always tell the user what actually happened to a change request:
confirm validated proposals, admit rejected ones, and stay out of the way of a
plain Q&A answer.
"""

from __future__ import annotations

from custom_components.complete_irrigation.schedule_review import augment_chat_reply


def test_confirms_single_validated_proposal():
    out = augment_chat_reply("I've proposed moving Citrus to 03:30.", 1, 1)
    assert out.startswith("I've proposed moving Citrus to 03:30.")
    assert "✅ 1 proposed change ready" in out
    assert "Apply" in out


def test_confirms_multiple_with_plural():
    out = augment_chat_reply("Two tweaks.", 3, 2)
    assert "✅ 2 proposed changes ready" in out


def test_admits_rejected_validation():
    # The model drafted items but the never-drop validator killed them all —
    # the reply must NOT imply a change landed.
    out = augment_chat_reply("I'll shorten the trees run.", 2, 0)
    assert "⚠️" in out
    assert "nothing was proposed" in out
    assert "✅" not in out


def test_plain_answer_untouched():
    # Q&A with no change requested: no footer at all.
    assert augment_chat_reply("Citrus runs at 07:30 for 4h.", 0, 0) == (
        "Citrus runs at 07:30 for 4h."
    )


def test_empty_reply_with_proposals_is_note_only():
    out = augment_chat_reply("", 1, 1)
    assert out.startswith("✅ 1 proposed change ready")
    assert "\n\n" not in out


def test_whitespace_reply_trimmed():
    out = augment_chat_reply("  ok  ", 0, 0)
    assert out == "ok"
