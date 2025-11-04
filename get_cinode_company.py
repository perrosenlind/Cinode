"""Fetch Cinode company metadata using the stored credentials and token helper."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import requests

from get_cinode_token import CREDENTIALS_FILE, load_credentials

API_BASE_URL = "https://api.cinode.com/v0.1"


def ensure_access_token() -> dict:
    """Run the token helper script and capture its JSON output."""

    token_script = Path(__file__).with_name("get_cinode_token.py")
    result = subprocess.run(
        [sys.executable, str(token_script)],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stderr:
        # Relay any warnings from the token script.
        print(result.stderr.strip(), file=sys.stderr)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive fallback
        raise RuntimeError("Failed to parse token script JSON output") from exc

    if "access_token" not in payload:
        raise RuntimeError("Token response missing access_token")

    return payload


def fetch_company(access_token: str, company_id: int) -> dict:
    url = f"{API_BASE_URL}/companies/{company_id}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def format_bool(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "N/A"


def print_company_summary(payload: dict) -> None:
    """Render a readable summary of the company payload."""

    name = payload.get("name") or "N/A"
    company_id = payload.get("id", "N/A")
    print(f"Company: {name} (ID: {company_id})")

    print("  Corporate Identity Number:", payload.get("corporateIdentityNumber") or "N/A")
    print("  VAT Number:", payload.get("vatNumber") or "N/A")
    reg_year = payload.get("registrationYear")
    print("  Registration Year:", reg_year if reg_year not in (None, "") else "N/A")
    print("  Tax Registered:", format_bool(payload.get("isTaxRegistered")))

    default_currency = payload.get("defaultCurrency") or {}
    if default_currency:
        code = default_currency.get("currencyCode") or "N/A"
        desc = default_currency.get("description") or ""
        desc_part = f" - {desc}" if desc else ""
        print(f"  Default Currency: {code}{desc_part}")
    else:
        print("  Default Currency: N/A")

    other_currencies = payload.get("currencies") or []
    if other_currencies:
        print("  Additional Currencies:")
        for curr in other_currencies:
            code = curr.get("currencyCode") or "N/A"
            desc = curr.get("description") or ""
            desc_part = f" - {desc}" if desc else ""
            print(f"    - {code}{desc_part}")
    else:
        print("  Additional Currencies: None")

    tags = payload.get("tags") or []
    if tags:
        print("  Tags:")
        for tag in tags:
            print(f"    - {tag.get('name') or 'N/A'} (ID: {tag.get('id', 'N/A')})")
    else:
        print("  Tags: None")

    addresses = payload.get("addresses") or []
    if addresses:
        print("  Addresses:")
        for idx, addr in enumerate(addresses, start=1):
            print(f"    {idx}. {addr.get('street1') or 'N/A'}")
            street2 = addr.get("street2")
            if street2:
                print(f"       {street2}")
            city_line_parts = [addr.get("zipCode"), addr.get("city"), addr.get("country")]
            city_line = " ".join(part for part in city_line_parts if part)
            if city_line:
                print(f"       {city_line}")
            email = addr.get("email")
            if email:
                print(f"       Email: {email}")
            comments = addr.get("comments")
            if comments:
                print(f"       Comments: {comments}")
            address_type = addr.get("addressType")
            if address_type is not None:
                print(f"       Type: {address_type}")
    else:
        print("  Addresses: None")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Cinode company information")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw JSON payload instead of the human-readable summary",
    )
    args = parser.parse_args()

    credentials = load_credentials(CREDENTIALS_FILE)
    company_id_raw = credentials.get("CINODE_COMPANY_ID")
    if not company_id_raw:
        raise ValueError("CINODE_COMPANY_ID is missing in credentials.txt")

    try:
        company_id = int(company_id_raw)
    except ValueError as exc:
        raise ValueError("CINODE_COMPANY_ID must be an integer") from exc

    token_payload = ensure_access_token()
    company_data = fetch_company(token_payload["access_token"], company_id)

    if args.json:
        print(json.dumps(company_data, indent=2))
    else:
        print_company_summary(company_data)


if __name__ == "__main__":
    main()
