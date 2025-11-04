"""Interactively select a Cinode user, list trainings, and export to CSV."""
from __future__ import annotations

import argparse
import csv
import json
import re
from itertools import groupby
from pathlib import Path
from typing import Dict, List, Optional

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token
from get_cinode_user_profile import fetch_user_profile
from get_cinode_user_skills import build_user_index, prompt_for_user

TRAINING_TYPE_LABELS = {0: "Course", 1: "Certification"}


def extract_trainings(profile: Dict) -> List[Dict]:
    """Return trainings array from the profile payload."""

    if not profile:
        return []
    trainings = profile.get("training")
    if isinstance(trainings, list):
        return trainings
    return []


def _pick_first_translation(entry: Dict) -> Dict:
    translations = entry.get("translations") or []
    if translations:
        return translations[0]
    return {}


def training_metadata(training: Dict) -> Dict[str, Optional[str]]:
    translation = _pick_first_translation(training)
    name = (
        translation.get("name")
        or translation.get("title")
        or training.get("name")
        or ""
    )
    title = (
        translation.get("title")
        or training.get("title")
        or name
        or "<unnamed>"
    )
    issuer = (
        translation.get("issuer")
        or translation.get("provider")
        or training.get("issuer")
        or training.get("provider")
        or ""
    )
    completed = (
        training.get("completedWhen")
        or training.get("completedDate")
        or training.get("completionDate")
        or training.get("date")
        or ""
    )
    expires = (
        training.get("expiresWhen")
        or training.get("expirationDate")
        or training.get("expireDate")
        or ""
    )

    return {
        "name": name,
        "title": title,
        "issuer": issuer,
        "completed": completed,
        "expires": expires,
        "trainingType": training.get("trainingType"),
        "translation": translation,
    }


def print_trainings_details(trainings: List[Dict]) -> None:
    if not trainings:
        print("No trainings found for this user.")
        return

    print(f"Trainings ({len(trainings)} total):")

    def sort_key(entry: Dict) -> int:
        training_type = entry.get("trainingType")
        return training_type if isinstance(training_type, int) else 999

    sorted_trainings = sorted(trainings, key=sort_key)

    for training_type, group in groupby(sorted_trainings, key=sort_key):
        group_list = list(group)
        label = TRAINING_TYPE_LABELS.get(training_type, str(training_type))
        print(f"\n{label} ({len(group_list)}):")

        for training in group_list:
            meta = training_metadata(training)
            translation = meta["translation"]

            print(f"  - {meta['title']}")
            print(f"    trainingType: {meta['trainingType']}")
            if meta["name"] and meta["name"] != meta["title"]:
                print(f"    name: {meta['name']}")
            if meta["issuer"]:
                print(f"    issuer: {meta['issuer']}")
            if meta["completed"]:
                print(f"    completed: {meta['completed']}")
            if meta["expires"]:
                print(f"    expires: {meta['expires']}")

            for key in [
                "id",
                "profileTrainingId",
                "companyTrainingId",
                "certificateUrl",
                "certificateLink",
                "certificateName",
                "certificateDescription",
                "tags",
            ]:
                value = training.get(key)
                if value not in (None, ""):
                    print(f"    {key}: {value}")

            if translation:
                print("    translation:")
                for key, value in translation.items():
                    print(f"      {key}: {value}")

            other_keys = {
                key: value
                for key, value in training.items()
                if key
                not in {
                    "trainingType",
                    "id",
                    "profileTrainingId",
                    "companyTrainingId",
                    "issuer",
                    "provider",
                    "completedWhen",
                    "completedDate",
                    "completionDate",
                    "date",
                    "expiresWhen",
                    "expirationDate",
                    "expireDate",
                    "certificateUrl",
                    "certificateLink",
                    "certificateName",
                    "certificateDescription",
                    "tags",
                    "translations",
                }
            }
            if other_keys:
                print("    other:")
                for key, value in other_keys.items():
                    print(f"      {key}: {value}")


def print_trainings_overview(trainings: List[Dict]) -> None:
    if not trainings:
        print("No trainings found for this user.")
        return

    print(f"Trainings overview ({len(trainings)} total):")

    def sort_key(entry: Dict) -> int:
        training_type = entry.get("trainingType")
        return training_type if isinstance(training_type, int) else 999

    sorted_trainings = sorted(trainings, key=sort_key)
    grouped_by_type: Dict[int, List[Dict]] = {}
    for training in sorted_trainings:
        training_type = training.get("trainingType")
        key = training_type if isinstance(training_type, int) else 999
        grouped_by_type.setdefault(key, []).append(training)

    for training_type, group in grouped_by_type.items():
        label = TRAINING_TYPE_LABELS.get(training_type, str(training_type))
        print(f"\n{label} ({len(group)}):")

        issuer_map: Dict[str, List[Dict]] = {}
        for training in group:
            meta = training_metadata(training)
            issuer = meta["issuer"] or training.get("provider") or "<unspecified supplier>"
            issuer_map.setdefault(issuer, []).append((meta, training))

        for issuer in sorted(issuer_map.keys(), key=lambda name: name.lower()):
            entries = issuer_map[issuer]
            print(f"  {issuer} ({len(entries)}):")
            for meta, training in sorted(entries, key=lambda item: item[0]["title"].lower()):
                details: List[str] = []
                year = training_year(training, meta)
                if year:
                    details.append(f"year {year}")
                elif meta["completed"]:
                    details.append(f"completed {meta['completed']}")
                if meta["expires"]:
                    details.append(f"expires {meta['expires']}")

                extra = f" ({'; '.join(details)})" if details else ""
                print(f"    - {meta['title']}{extra}")


def completed_year(timestamp: Optional[str]) -> str:
    if not timestamp:
        return ""

    value = timestamp.strip()
    if not value:
        return ""

    base = value.split("T", 1)[0]
    if len(base) >= 4 and base[:4].isdigit():
        return base[:4]

    match = re.search(r"(19|20)\d{2}", value)
    if match:
        return match.group(0)

    return ""


def training_year(training: Dict, meta: Dict[str, Optional[str]]) -> str:
    raw_year = training.get("year")
    if isinstance(raw_year, int):
        string_year = str(raw_year)
        return string_year if len(string_year) == 4 else ""
    if isinstance(raw_year, str):
        cleaned = raw_year.strip()
        if len(cleaned) == 4 and cleaned.isdigit():
            return cleaned
        match = re.search(r"(19|20)\d{2}", cleaned)
        if match:
            return match.group(0)

    return completed_year(meta.get("completed"))


def build_csv_rows(trainings: List[Dict]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for training in trainings:
        meta = training_metadata(training)
        rows.append(
            {
                "name": meta["name"],
                "title": meta["title"],
                "issuer": meta["issuer"],
                "expireDate": meta["expires"],
                "year": training_year(training, meta),
            }
        )
    return rows


def resolve_output_path(user_entry: Dict, requested: Optional[Path]) -> Path:
    if requested:
        return requested

    first = (user_entry.get("firstName") or "").strip()
    last = (user_entry.get("lastName") or "").strip()
    user_id = user_entry.get("companyUserId")
    base_parts = [part for part in [first, last] if part]
    base = "_".join(base_parts) if base_parts else f"user_{user_id}"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base)
    safe = safe or "cinode_user"
    return Path(f"{safe}_trainings.csv")


def write_csv(rows: List[Dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "title", "issuer", "expireDate", "year"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_path}")




def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select a Cinode user and list their profile trainings",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the resulting trainings payload as JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a CSV summary to this path (defaults to '<user>_trainings.csv')",
    )
    parser.add_argument(
        "--user",
        "-u",
        dest="user_query",
        help="Pre-filter users by name or ID and auto-select if a single match is found",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show the full per-training details (original verbose output)",
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

    user_entry = user_index[selected_user_id]
    profile = fetch_user_profile(access_token, company_id, selected_user_id)
    trainings = extract_trainings(profile)

    if args.json:
        print(json.dumps(trainings, indent=2))
    elif args.details:
        print_trainings_details(trainings)
    else:
        print_trainings_overview(trainings)

    rows = build_csv_rows(trainings)
    output_path = resolve_output_path(user_entry, args.output)
    write_csv(rows, output_path)


if __name__ == "__main__":
    main()
