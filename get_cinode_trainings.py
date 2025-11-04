"""List all company trainings grouped by training type."""
from __future__ import annotations

import argparse
import json
from typing import Dict, List

import requests

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token

API_BASE_URL = "https://api.cinode.com/v0.1"
TRAINING_TYPE_LABELS = {0: "Course", 1: "Certification"}


def fetch_trainings(access_token: str, company_id: int, training_type: int) -> List[Dict]:
    """Return trainings for the given company and training type."""

    url = f"{API_BASE_URL}/companies/{company_id}/trainings/{training_type}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if response.status_code == 204:
        return []

    if response.status_code == 404:
        raise RuntimeError(
            "Company trainings endpoint returned 404. Your Cinode tenant likely "
            "does not have the CompanyTraining module enabled, or the token lacks "
            "CompanyAdmin privileges."
        )

    if response.status_code == 403:
        raise RuntimeError(
            "Company trainings endpoint returned 403 Forbidden. Confirm that the "
            "current token belongs to a CompanyAdmin and that the CompanyTraining "
            "module is enabled for the tenant."
        )

    response.raise_for_status()

    if not response.content.strip():
        return []

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Failed to parse trainings response for type {training_type}: {response.text!r}"
        ) from exc


def summarize_trainings(grouped_trainings: Dict[int, List[Dict]]) -> None:
    for training_type, items in grouped_trainings.items():
        label = TRAINING_TYPE_LABELS.get(training_type, str(training_type))
        print(f"\n{label} ({len(items)}):")
        if not items:
            print("  (none)")
            continue

        for training in items:
            name = training.get("name") or training.get("title") or "<unnamed>"
            training_id = training.get("companyTrainingId", training.get("id", "N/A"))
            code = training.get("code")
            tags = training.get("tags") or []
            tag_names = ", ".join(tag.get("name") for tag in tags if tag.get("name"))

            line = f"  - {name} (ID: {training_id})"
            if code:
                line += f", code: {code}"
            if tag_names:
                line += f", tags: {tag_names}"
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="List Cinode company trainings by type")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON grouped by training type",
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
    access_token = token_payload["access_token"]

    grouped: Dict[int, List[Dict]] = {}
    for training_type in TRAINING_TYPE_LABELS.keys():
        grouped[training_type] = fetch_trainings(access_token, company_id, training_type)

    if args.json:
        print(json.dumps(grouped, indent=2))
    else:
        summarize_trainings(grouped)


if __name__ == "__main__":
    main()
