"""The automatic draft check: no contact details, except 000 for emergencies."""

import pytest

from agent import contains_contact_details, enforce_reply_rules


@pytest.mark.parametrize("text", [
    "If you smell gas, please leave and call 000.",
    "If anyone is hurt, dial 000 straight away.",
    # Numbers that aren't phone numbers must not be flagged.
    "Check-in is from 3:00 pm on 16 Oct 2026.",
    "Extra guests are $100 per night, up to 10 guests.",
    "The apartment has 2 tandem spaces and sleeps 4.",
])
def test_allowed(text):
    assert not contains_contact_details(text)


@pytest.mark.parametrize("text", [
    "Call us on 0412 345 678.",
    "You can call Lifeline on 13 11 14.",
    "Please ring 112 if needed.",
    "Text 0400123456 when you arrive.",
    "Call 000 or our mobile 0412 345 678.",
    "Email host@example.com",
    "See www.example.com",
])
def test_flagged(text):
    assert contains_contact_details(text)


def draft(text):
    return {"category": "urgent", "reason": "Leak.", "draft_reply": text, "missing_info": []}


def test_emergency_000_keeps_category():
    assert enforce_reply_rules(draft("If you see sparks, please call 000."))["category"] == "urgent"


def test_short_number_forces_human_review():
    result = enforce_reply_rules(draft("Please call our plumber on 131 444."))
    assert result["category"] == "human_review"
    assert "Flagged" in result["reason"]
