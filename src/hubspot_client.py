"""HubSpot as the property data source.

Each rental property is a HubSpot Company. The free CRM caps custom
properties at 10, so instead of one custom property per field we use the
standard name/description fields plus three custom properties, with the
rest of the guest-safe fields stored as JSON in rental_guest_info.

Usage (one-time upload from data/properties.json):
    python src/hubspot_client.py
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
API_BASE = "https://api.hubapi.com"
API_VERSION = "2026-09"
MAX_ATTEMPTS = 3

# listing_id is text, not a number: Airbnb IDs like 905341218171745855 are
# too large for HubSpot's number fields to store exactly.
CUSTOM_PROPERTIES = [
    {"name": "rental_property_code", "label": "Rental property code", "type": "string", "fieldType": "text"},
    {"name": "rental_listing_id", "label": "Rental listing ID", "type": "string", "fieldType": "text"},
    {"name": "rental_guest_info", "label": "Rental guest info (JSON)", "type": "string", "fieldType": "textarea"},
]

# Allow-lists in both directions. Upload only sends these, and reads only
# keep these, so a restricted field can't reach HubSpot by accident, and
# can't come back out of it if someone types one into the CRM by hand.
GUEST_INFO_FIELDS = [
    "neighbourhood", "suburb_aliases", "property_type", "amenities",
    "minimum_nights", "max_guests", "check_in_features", "house_rules",
    "check_in_time", "check_out_time", "wifi_network", "parking",
]
READ_PROPERTIES = ["name", "description"] + [p["name"] for p in CUSTOM_PROPERTIES]


class PropertySourceError(Exception):
    """Property data couldn't be loaded. The agent routes these to human_review."""


class HubSpotClient:
    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        self.token = token or os.environ.get("HUBSPOT_TOKEN", "")
        if not self.token:
            raise PropertySourceError("HUBSPOT_TOKEN is not set in .env")
        self.session = session or requests.Session()

    def request(self, method: str, path: str, ok_statuses: tuple = (), **kwargs) -> dict:
        url = f"{API_BASE}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.session.request(method, url, headers=headers, timeout=15, **kwargs)
            except requests.RequestException as e:
                raise PropertySourceError(f"Couldn't reach HubSpot: {e}") from e

            if resp.status_code < 400 or resp.status_code in ok_statuses:
                return resp.json() if resp.content else {}
            # Rate limits and server errors are usually temporary, so retry
            # with backoff before giving up.
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                    continue
                raise PropertySourceError(f"HubSpot unavailable after {MAX_ATTEMPTS} attempts (HTTP {resp.status_code})")
            if resp.status_code == 401:
                raise PropertySourceError("HubSpot rejected the token (401). Check HUBSPOT_TOKEN.")
            if resp.status_code == 403:
                raise PropertySourceError("HubSpot token is missing a required scope (403).")
            raise PropertySourceError(f"HubSpot error {resp.status_code}: {resp.text[:200]}")


# --- Upload ------------------------------------------------------------------

def build_company_properties(prop: dict) -> dict:
    guest_info = {field: prop[field] for field in GUEST_INFO_FIELDS if field in prop}
    payload = {
        "name": prop["name"],
        "description": prop["description"],
        "rental_property_code": prop["property_code"],
        "rental_listing_id": prop["listing_id"],
        "rental_guest_info": json.dumps(guest_info, ensure_ascii=False),
    }
    # The allow-list above should make this impossible; this is the last
    # line of defence before data leaves the machine.
    if "FAKE-" in json.dumps(payload):
        raise ValueError(f"Refusing to upload {prop['property_code']}: payload contains a restricted value")
    return payload


def list_rental_companies(client: HubSpotClient) -> list[dict]:
    companies, after = [], None
    while True:
        params = {"limit": 100, "properties": ",".join(READ_PROPERTIES)}
        if after:
            params["after"] = after
        page = client.request("GET", f"/crm/objects/{API_VERSION}/companies", params=params)
        # The CRM holds other companies too (HubSpot adds a sample one), so
        # only records with a property code count as rentals.
        companies += [c for c in page.get("results", []) if c["properties"].get("rental_property_code")]
        after = page.get("paging", {}).get("next", {}).get("after")
        if not after:
            return companies


def upload(properties: list[dict], client: HubSpotClient) -> None:
    for definition in CUSTOM_PROPERTIES:
        # 409 means the property already exists, so re-running the upload is safe.
        client.request(
            "POST", f"/crm/properties/{API_VERSION}/companies",
            json={**definition, "groupName": "companyinformation"}, ok_statuses=(409,),
        )

    existing = {c["properties"]["rental_property_code"]: c["id"] for c in list_rental_companies(client)}
    for prop in properties:
        body = {"properties": build_company_properties(prop)}
        code = prop["property_code"]
        if code in existing:
            client.request("PATCH", f"/crm/objects/{API_VERSION}/companies/{existing[code]}", json=body)
            print(f"Updated {code}")
        else:
            client.request("POST", f"/crm/objects/{API_VERSION}/companies", json=body)
            print(f"Created {code}")


# --- Property source ---------------------------------------------------------

class HubSpotPropertySource:
    """Same interface as JsonPropertySource: all_properties() -> list[dict]."""

    def __init__(self, client: HubSpotClient | None = None):
        self._client = client

    def all_properties(self) -> list[dict]:
        # Created on first use so importing the agent never needs a token
        # (e.g. when running with --local).
        if self._client is None:
            self._client = HubSpotClient()

        properties = []
        for company in list_rental_companies(self._client):
            p = company["properties"]
            code = p["rental_property_code"]
            try:
                guest_info = json.loads(p.get("rental_guest_info") or "")
            except json.JSONDecodeError:
                raise PropertySourceError(f"{code} in HubSpot has missing or invalid rental_guest_info")
            properties.append({
                "property_code": code,
                "listing_id": p.get("rental_listing_id") or "",
                "name": p.get("name") or "",
                "description": p.get("description") or "",
                **{field: guest_info[field] for field in GUEST_INFO_FIELDS if field in guest_info},
            })

        if not properties:
            raise PropertySourceError("No rental properties found in HubSpot. Run: python src/hubspot_client.py")
        return properties


def main() -> None:
    load_dotenv(ROOT / ".env")
    properties = json.loads((ROOT / "data" / "properties.json").read_text())
    upload(properties, HubSpotClient())
    print(f"Uploaded {len(properties)} properties (guest-safe fields only).")


if __name__ == "__main__":
    main()
