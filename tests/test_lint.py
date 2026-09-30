"""Tests for the deterministic copy lint."""

from vertex.ai.qa import lint_step


def _number_flags(body, allowed=frozenset()):
    return [f for f in lint_step("email4", None, body, "hook", set(allowed)) if f.startswith("number_parroted")]


def test_call_length_in_cta_is_not_parroted():
    assert _number_flags("Worth a 15-minute call?") == []
    assert _number_flags("Open to 15 minutes next week?") == []


def test_other_numbers_still_flagged_unless_in_hook():
    assert _number_flags("You serve 312 customers.") == ["number_parroted:312"]
    assert _number_flags("You serve 312 customers.", {"312"}) == []
