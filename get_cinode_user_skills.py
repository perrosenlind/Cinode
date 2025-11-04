"""Interactively select a Cinode user from team memberships and list their skills."""
from __future__ import annotations

import argparse
import json
from typing import Dict, List, Optional

import requests

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token, fetch_teams
from get_cinode_team_members import fetch_team_members

API_BASE_URL = "https://api.cinode.com/v0.1"


def fetch_user_skills(access_token: str, company_id: int, company_user_id: int) -> List[Dict]:
    url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}/skills"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if response.status_code == 204 or not response.content.strip():
        return []

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Failed to parse skills response for user {company_user_id}: {response.text!r}"
        ) from exc


def build_user_index(access_token: str, company_id: int) -> Dict[int, Dict]:
    """Return a dictionary keyed by company user ID with aggregated team metadata."""

    teams = fetch_teams(access_token, company_id)
    index: Dict[int, Dict] = {}

    for team in teams:
        team_id = team.get("id")
        if team_id is None:
            continue
        team_name = team.get("name") or f"Team {team_id}"
        members = fetch_team_members(access_token, company_id, team_id)

        for member in members:
            user = member.get("companyUser") or {}
            user_id = user.get("companyUserId")
            if user_id is None:
                continue

            entry = index.setdefault(
                user_id,
                {
                    "companyUserId": user_id,
                    "firstName": user.get("firstName") or "",
                    "lastName": user.get("lastName") or "",
                    "companyUserType": user.get("companyUserType"),
                    "teams": set(),
                },
            )

            entry["teams"].add(team_name)

    return index


def prompt_for_user(
    selection_index: Dict[int, Dict],
    quick_query: Optional[str] = None,
    auto_confirm_single: bool = False,
) -> Optional[int]:
    """Prompt for a user selection, optionally seeded with a quick search query."""

    if not selection_index:
        print("No users were found in the retrieved team memberships.")
        return None

    sorted_users = sorted(
        selection_index.values(),
        key=lambda u: ((u.get("firstName") or "").lower(), (u.get("lastName") or "").lower()),
    )

    help_text = (
        "Type part of the first name, last name, or user ID to filter. "
        "Press Enter without typing to list all users. Type 'q' to quit."
    )

    print(help_text)

    def describe_user(user: Dict) -> tuple[str, str]:
        first = user.get("firstName") or ""
        last = user.get("lastName") or ""
        summary_parts = [part for part in [first, last] if part]
        summary = " ".join(summary_parts) or "<unknown>"
        teams = ", ".join(sorted(user.get("teams", [])))
        user_type = user.get("companyUserType")
        extra_parts: List[str] = []
        if user_type is not None:
            extra_parts.append(f"type {user_type}")
        if teams:
            extra_parts.append(f"teams: {teams}")
        extra_text = f" ({'; '.join(extra_parts)})" if extra_parts else ""
        return summary, extra_text

    def match_users(query: str) -> tuple[List[Dict], bool]:
        if not query:
            return sorted_users, False
        lowered = query.lower()
        if lowered in {"q", "quit", "exit"}:
            return [], True
        matches = [
            user
            for user in sorted_users
            if lowered in str(user.get("companyUserId", "")).lower()
            or lowered in (user.get("firstName") or "").lower()
            or lowered in (user.get("lastName") or "").lower()
        ]
        return matches, False

    pending_queries: List[str] = []
    if quick_query is not None:
        pending_queries.append(quick_query.strip())

    while True:
        if pending_queries:
            query = pending_queries.pop(0)
            query_is_quick = True
        else:
            query = input("Search user: ").strip()
            query_is_quick = False

        matches, should_quit = match_users(query)
        if should_quit:
            return None

        if not matches:
            if query_is_quick and query:
                print(f"No users matched '{query}'.")
            else:
                print("No matches, try again.")
            continue

        if auto_confirm_single and query_is_quick and query and len(matches) == 1:
            chosen = matches[0]
            summary, extra_text = describe_user(chosen)
            user_id = chosen.get("companyUserId")
            print(f"Selected {summary} [ID: {user_id}]{extra_text}")
            return user_id

        if query and len(matches) > 20:
            print(f"{len(matches)} matches found; please refine your search.")
            continue

        display_slice = matches[:20]
        for idx, user in enumerate(display_slice, start=1):
            user_id = user.get("companyUserId")
            summary, extra_text = describe_user(user)
            print(f"{idx}. {summary} [ID: {user_id}]{extra_text}")

        if len(matches) > len(display_slice):
            print("…more results truncated; narrow the search to see additional entries.")

        selection = input("Choose a number from the list (or press Enter to refine): ").strip()
        if not selection:
            continue

        if not selection.isdigit():
            print("Please enter a valid number from the list.")
            continue

        index = int(selection)
        if not 1 <= index <= len(display_slice):
            print("Number out of range for the displayed entries.")
            continue

        chosen = display_slice[index - 1]
        return chosen.get("companyUserId")


def print_skills_summary(skills: List[Dict]) -> None:
    if not skills:
        print("No skills found for this user.")
        return

    print(f"Skills ({len(skills)}):")
    for skill in skills:
        keyword = skill.get("keyword") or {}
        name = keyword.get("masterSynonym") or (keyword.get("synonyms") or [None])[0]
        name = name or "<unnamed>"
        level = skill.get("level")
        level_goal = skill.get("levelGoal")
        goal_deadline = skill.get("levelGoalDeadline")
        work_days = skill.get("numberOfDaysWorkExperience")
        favourite = skill.get("favourite")

        parts = [f"- {name}"]
        if level is not None:
            parts.append(f"level {level}")
        if work_days is not None:
            parts.append(f"{work_days} days experience")
        if level_goal is not None:
            goal_text = f"goal {level_goal}"
            if goal_deadline:
                goal_text += f" by {goal_deadline}"
            parts.append(goal_text)
        if favourite:
            parts.append("favourite")

        print(", ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select a Cinode user from team memberships and show their skills",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the resulting skills payload as JSON",
    )
    parser.add_argument(
        "--user",
        "-u",
        dest="user_query",
        help="Pre-filter users by name or ID and auto-select if only one match",
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

    user_index = build_user_index(access_token, company_id)
    selected_user_id = prompt_for_user(
        user_index,
        quick_query=args.user_query,
        auto_confirm_single=bool(args.user_query),
    )
    if selected_user_id is None:
        print("No user selected.")
        return

    skills = fetch_user_skills(access_token, company_id, selected_user_id)

    if args.json:
        print(json.dumps(skills, indent=2))
    else:
        print_skills_summary(skills)


if __name__ == "__main__":
    main()
