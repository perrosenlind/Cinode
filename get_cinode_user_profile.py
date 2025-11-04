"""Select a Cinode user and display their profile information."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Dict, List, Optional

import requests

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token
from get_cinode_user_skills import build_user_index, prompt_for_user

API_BASE_URL = "https://api.cinode.com/v0.1"


def fetch_user_profile(access_token: str, company_id: int, company_user_id: int) -> Dict:
    url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}/profile"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if response.status_code == 204 or not response.content.strip():
        return {}

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Failed to parse profile response for user {company_user_id}: {response.text!r}"
        ) from exc


def _fmt_date(value: Optional[str]) -> str:
    if not value:
        return "N/A"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value


def _print_translated_list(title: str, entries: List[Dict], name_keys: List[str]) -> None:
    if not entries:
        print(f"{title}: None")
        return

    print(f"{title}: {len(entries)}")
    for entry in entries:
        translations = entry.get("translations") or []
        if translations:
            primary = translations[0]
            name_parts = [primary.get(key) for key in name_keys if primary.get(key)]
            name = ", ".join(name_parts) if name_parts else "<unnamed>"
        else:
            name = entry.get("name") or entry.get("title") or "<unnamed>"

        start = _fmt_date(entry.get("startDate"))
        end = _fmt_date(entry.get("endDate"))
        current_flag = entry.get("isCurrent")
        current = " (current)" if current_flag else ""
        print(f"  - {name}{current} [{start} → {end}]")


def summarize_profile(profile: Dict, user_entry: Dict) -> None:
    if not profile:
        print("No profile data returned for this user.")
        return

    full_name = " ".join(part for part in [user_entry.get("firstName"), user_entry.get("lastName")] if part)
    full_name = full_name or "<unknown>"

    print(f"Profile for {full_name} (User ID: {user_entry.get('companyUserId')})")
    print("- Profile ID:", profile.get("id", "N/A"))
    print("- Created:", _fmt_date(profile.get("createdWhen")))
    print("- Updated:", _fmt_date(profile.get("updatedWhen")))
    print("- Published:", _fmt_date(profile.get("publishedWhen")))

    _print_translated_list("Employers", profile.get("employers") or [], ["name", "title"])
    _print_translated_list("Work Experience", profile.get("workExperience") or [], ["title", "employer"])
    _print_translated_list("Education", profile.get("education") or [], ["schoolName", "programName"])
    _print_translated_list("Training", profile.get("training") or [], ["title", "issuer"])

    skills = profile.get("skills") or []
    if skills:
        print(f"Skills ({len(skills)}):")
        for skill in skills:
            keyword = skill.get("keyword") or {}
            name = keyword.get("masterSynonym") or (keyword.get("synonyms") or [None])[0]
            name = name or "<unnamed>"
            level = skill.get("level")
            work_days = skill.get("numberOfDaysWorkExperience")
            level_info = f" level {level}" if level is not None else ""
            work_info = f", {work_days} days" if work_days is not None else ""
            print(f"  - {name}{level_info}{work_info}")
    else:
        print("Skills: None")

    languages = profile.get("languages") or []
    if languages:
        print("Languages:")
        for lang in languages:
            language = lang.get("language") or {}
            name = language.get("name") or language.get("lang") or "<unnamed>"
            level = lang.get("level")
            print(f"  - {name} (level {level if level is not None else 'N/A'})")
    else:
        print("Languages: None")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Cinode user profile data")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the raw JSON profile payload",
    )
    parser.add_argument(
        "--user",
        "-u",
        dest="user_query",
        help="Pre-filter users by name or ID and auto-select if a single match is found",
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

    profile = fetch_user_profile(access_token, company_id, selected_user_id)

    if args.json:
        print(json.dumps(profile, indent=2))
    else:
        summarize_profile(profile, user_index[selected_user_id])


if __name__ == "__main__":
    main()
