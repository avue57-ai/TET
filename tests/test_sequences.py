"""Sequence configs must parse and keep the shape the campaign builder reads."""

import pytest

from vertex.ai.personalize import load_sequence

ARMS = ["linkedin_led", "email_first"]
COPY_STEPS = {"li_note", "li_msg", "email1", "email2", "email3", "email4", "email5"}


@pytest.mark.parametrize("arm", ARMS)
def test_sequence_parses_and_steps_are_well_formed(arm):
    seq = load_sequence(arm)
    assert seq["arm"] == arm
    assert seq["version"]
    assert seq["steps"] and all(s.get("key") and s.get("type") for s in seq["steps"])
    assert COPY_STEPS <= set(seq["step_purposes"])


def test_linkedin_led_conditional_has_both_branches():
    seq = load_sequence("linkedin_led")
    cond = next(s for s in seq["steps"] if s["type"] == "conditional")
    assert cond["conditionKey"] == "linkedinInviteAccepted"
    assert cond["accepted"] and cond["fallback"]
    assert all(s.get("key") and s.get("type") for s in cond["accepted"] + cond["fallback"])
