"""Aggregate Cinode team members for every team in the company."""
from __future__ import annotations

import argparse
import json
from typing import List, Dict

import requests

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token, fetch_teams

API_BASE_URL = "https://api.cinode.com/v0.1"


def fetch_team_members(access_token: str, company_id: int, team_id: int) -> List[Dict]:
    """Return the members for a single team."""

    url = f"{API_BASE_URL}/companies/{company_id}/teams/{team_id}/members"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if response.status_code == 204 or not response.content.strip():
        # The API returns no payload when the team has no members.
        return []

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Failed to parse members response for team {team_id}: {response.text!r}"
        ) from exc


def summarize_memberships(team_memberships: List[Dict]) -> None:
    if not team_memberships:
        print("No teams found.")
        return

    total_members = sum(len(entry["members"]) for entry in team_memberships)
    print(f"Team memberships: {total_members} entries across {len(team_memberships)} teams")

    for entry in team_memberships:
        team = entry["team"]
        members = entry["members"]
        team_name = team.get("name") or "<unnamed team>"
        team_id = team.get("id", "N/A")
        print(f"\n{team_name} (ID: {team_id}) — {len(members)} member(s)")

        if not members:
            print("  (no members)")
            continue

        sorted_members = sorted(
            members,
            key=lambda m: (
                (m.get("companyUser") or {}).get("firstName") or "",
                (m.get("companyUser") or {}).get("lastName") or "",
            ),
        )

        for member in sorted_members:
            user = member.get("companyUser") or {}
            first = user.get("firstName") or ""
            last = user.get("lastName") or ""
            full_name = (f"{first} {last}").strip() or "<unknown>"
            user_id = user.get("companyUserId", "N/A")
            availability = member.get("availabilityPercent")
            availability_text = f", availability {availability}%" if availability is not None else ""
            print(f"  - {full_name} (User ID: {user_id}{availability_text})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch all Cinode team members for every team in the configured company",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the aggregated data as JSON instead of a summary",
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

    teams = fetch_teams(access_token, company_id)

    team_memberships: List[Dict] = []
    for team in teams:
        team_id = team.get("id")
        if team_id is None:
            continue
        members = fetch_team_members(access_token, company_id, team_id)
        team_memberships.append(
            {
                "team": {
                    "id": team_id,
                    "name": team.get("name"),
                    "description": team.get("description"),
                    "parentTeamId": team.get("parentTeamId"),
                },
                "members": members,
            }
        )

    if args.json:
        print(json.dumps(team_memberships, indent=2))
    else:
        summarize_memberships(team_memberships)


if __name__ == "__main__":
    main()
