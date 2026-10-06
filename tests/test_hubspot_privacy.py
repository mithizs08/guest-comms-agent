"""Restricted fields must never reach HubSpot or come back out of it, and
HubSpot failures must route to human_review instead of crashing.

These tests use a fake HTTP session, so they run offline with no token.
"""

import json
from pathlib import Path

import pytest

from agent import process_email
from hubspot_client import CUSTOM_PROPERTIES, HubSpotClient, HubSpotPropertySource, PropertySourceError, upload

ROOT = Path(__file__).resolve().parent.parent
PROPERTIES = json.loads((ROOT / "data" / "properties.json").read_text())
RESTRICTED_KEYS = set(PROPERTIES[0]["restricted"]) | {"restricted"}


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Records every request and replies from a queue (or 200 {} by default)."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse()


def assert_no_restricted(data):
    text = json.dumps(data, ensure_ascii=False)
    assert "FAKE-" not in text
    for key in RESTRICTED_KEYS:
        assert f'"{key}"' not in text and f'\\"{key}\\"' not in text, key


def company(code, guest_info, **extra):
    return {"id": code, "properties": {
        "rental_property_code": code, "rental_listing_id": "123", "name": "Test listing",
        "description": "Test", "rental_guest_info": json.dumps(guest_info), **extra,
    }}


def test_upload_never_sends_restricted_fields():
    session = FakeSession()
    upload(PROPERTIES, HubSpotClient(token="test", session=session))

    sent = [r.get("json") for r in session.requests if r.get("json")]
    assert len(sent) == len(CUSTOM_PROPERTIES) + len(PROPERTIES)
    assert_no_restricted(sent)


def test_fetch_drops_restricted_fields_added_in_hubspot():
    # Simulate someone pasting restricted data into the CRM by hand.
    leaked = {**PROPERTIES[0], "restricted": PROPERTIES[0]["restricted"], "lockbox_code": "FAKE-LOCK-X"}
    page = {"results": [company("WAV-01", leaked, lockbox_code="FAKE-LOCK-Y")]}
    session = FakeSession([FakeResponse(200, page)])

    properties = HubSpotPropertySource(HubSpotClient(token="test", session=session)).all_properties()

    assert properties[0]["property_code"] == "WAV-01"
    assert properties[0]["suburb_aliases"] == PROPERTIES[0]["suburb_aliases"]
    assert_no_restricted(properties)


def test_fetch_ignores_non_rental_companies():
    sample = {"id": "1", "properties": {"name": "HubSpot", "rental_property_code": None}}
    page = {"results": [sample, company("WAV-01", {"neighbourhood": "Waverley"})]}
    session = FakeSession([FakeResponse(200, page)])

    properties = HubSpotPropertySource(HubSpotClient(token="test", session=session)).all_properties()
    assert [p["property_code"] for p in properties] == ["WAV-01"]


@pytest.mark.parametrize("response, message", [
    (FakeResponse(401), "rejected the token"),
    (FakeResponse(403), "missing a required scope"),
    (FakeResponse(200, {"results": []}), "No rental properties"),
    (FakeResponse(200, {"results": [{"id": "1", "properties": {"rental_property_code": "WAV-01"}}]}), "invalid rental_guest_info"),
])
def test_hubspot_errors_raise_property_source_error(response, message):
    source = HubSpotPropertySource(HubSpotClient(token="test", session=FakeSession([response])))
    with pytest.raises(PropertySourceError, match=message):
        source.all_properties()


def test_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("hubspot_client.time.sleep", lambda s: None)
    page = {"results": [company("WAV-01", {"neighbourhood": "Waverley"})]}
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, page)])

    properties = HubSpotPropertySource(HubSpotClient(token="test", session=session)).all_properties()
    assert len(session.requests) == 2 and properties[0]["property_code"] == "WAV-01"


def test_hubspot_failure_routes_email_to_human_review():
    source = HubSpotPropertySource(HubSpotClient(token="bad", session=FakeSession([FakeResponse(401)])))
    raw = (ROOT / "data" / "sample_emails" / "email_01.txt").read_text()

    # client=None: platform comes from rules and Claude must not be called
    # when there's no property data.
    result = process_email(None, raw, source)

    assert result["category"] == "human_review"
    assert "rejected the token" in result["reason"]
    assert result["draft_reply"] == ""
