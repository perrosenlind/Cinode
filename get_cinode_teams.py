"""Fetch Cinode team information for the configured company."""
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
    """Invoke get_cinode_token.py and parse the JSON response."""

    token_script = Path(__file__).with_name("get_cinode_token.py")
    result = subprocess.run(
        [sys.executable, str(token_script)],
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse token helper output") from exc

    if "access_token" not in payload:
        raise RuntimeError("Token response missing access_token")

    return payload


def fetch_teams(access_token: str, company_id: int) -> list[dict]:
    url = f"{API_BASE_URL}/companies/{company_id}/teams"
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


def print_team_summary(teams: list[dict]) -> None:
    if not teams:
        print("No teams found for this company.")
        return

    print(f"Teams ({len(teams)}):")
    for team in teams:
        name = team.get("name") or "<unnamed>"
        team_id = team.get("id", "N/A")
        description = team.get("description") or ""
        parent_team_id = team.get("parentTeamId")

        print(f"- {name} (ID: {team_id})")
        if description:
            print(f"    Description: {description}")
        if parent_team_id:
            print(f"    Parent Team ID: {parent_team_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Cinode teams for the configured company")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON payload instead of a summary",
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
    teams = fetch_teams(token_payload["access_token"], company_id)

    if args.json:
        print(json.dumps(teams, indent=2))
    else:
        print_team_summary(teams)


if __name__ == "__main__":
    main()
