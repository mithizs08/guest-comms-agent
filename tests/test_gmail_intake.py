"""Offline tests for Gmail intake: replies are only ever drafts, they thread
correctly, and direct (non-platform) emails still match a property.
"""

import email
import email.policy
import json
from pathlib import Path

import gmail_intake
from agent import identify_property, parse_email, platform_from_rules

ROOT = Path(__file__).resolve().parent.parent
PROPERTIES = json.loads((ROOT / "data" / "properties.json").read_text())

GUEST_EMAIL = b"""From: Jo Guest <jo.guest@gmail.com>
To: you@example.com
Subject: Parking at the Neutral Bay apartment
Message-ID: <abc123@mail.gmail.com>
Content-Type: text/plain; charset="utf-8"

Hi, we're staying at your Neutral Bay apartment next week. Is there parking?
"""


class FakeImap:
    def __init__(self):
        self.appended, self.stored = [], []

    def append(self, folder, flags, date, message):
        self.appended.append((folder, flags, message))
        return "OK", []

    def uid(self, command, *args):
        self.stored.append((command, *args))
        return "OK", []


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_module_has_no_way_to_send_email():
    source = Path(gmail_intake.__file__).read_text()
    assert "smtplib" not in source and ".send(" not in source


def test_reply_is_saved_as_threaded_draft():
    imap = FakeImap()
    gmail_intake.save_draft_reply(imap, '"[Gmail]/Drafts"', parse(GUEST_EMAIL), "Hi Jo,\n\nYes.", "you@example.com")

    folder, flags, raw = imap.appended[0]
    draft = parse(raw)
    assert folder == '"[Gmail]/Drafts"' and flags == "(\\Draft)"
    assert draft["To"] == "Jo Guest <jo.guest@gmail.com>"
    assert draft["Subject"] == "Re: Parking at the Neutral Bay apartment"
    assert draft["In-Reply-To"] == "<abc123@mail.gmail.com>"
    assert "<abc123@mail.gmail.com>" in draft["References"]


def test_labels_category_and_processed_together():
    imap = FakeImap()
    gmail_intake.add_labels(imap, b"42", ["triage/routine", gmail_intake.PROCESSED_LABEL])
    assert imap.stored == [("STORE", b"42", "+X-GM-LABELS", '("triage/routine" "agent-processed")')]


def test_direct_email_has_unknown_platform_but_matches_property():
    raw = gmail_intake.to_agent_format(parse(GUEST_EMAIL))
    parsed = parse_email(raw)

    assert platform_from_rules(parsed) is None  # falls back to Claude, which should say "unknown"
    assert identify_property(parsed, PROPERTIES)["property_code"] == "NOR-01"
