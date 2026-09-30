"""The critic must see everything the writer was allowed to use."""

from vertex.ai.claude import render
from vertex.ai.personalize import Critique


def _payload(**extra):
    base = {"company_name": "Acme", "hq": "Austin, TX", "company_summary": "Makes dispatch software.",
            "hook_type": "product_niche", "hook_text": "Sells dispatch software.", "messages": "[email1] ..."}
    return {**base, **extra}


def test_critic_sees_second_hook_hq_and_summary():
    out = render("copy_critic", _payload(backup_hook_text="Launched a mobile app."), Critique)
    assert "Launched a mobile app." in out
    assert "Austin, TX" in out
    assert "Makes dispatch software." in out


def test_critic_renders_without_second_hook():
    out = render("copy_critic", _payload(backup_hook_text=""), Critique)
    assert "Second signal" not in out
