"""Compare Cinode trainings CSV with Credly badges CSV for a single user."""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

TRAININGS_EXPECTED_HEADERS = ["name", "title", "issuer", "expireDate", "year"]
BADGES_EXPECTED_HEADERS = [
    "Employee Name",
    "Badge Title",
    "Issue Date",
    "Expiry Date",
    "Issuer",
]


def normalize_title(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.lower())

    def normalize_token(token: str) -> str:
        original = token
        if token.endswith("ity") and len(token) > 4:
            token = token[:-3] + "e"
        if token.endswith("ing") and len(token) > 4:
            token = token[:-3]
        elif token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("ied") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("ed") and len(token) > 3 and not original.endswith("eed"):
            token = token[:-2]
        if token.endswith("s") and len(token) > 3:
            token = token[:-1]
        return token

    canonical_tokens = [normalize_token(token) for token in tokens if token]
    if not canonical_tokens:
        return ""
    canonical_tokens.sort()
    return " ".join(canonical_tokens)


def read_csv(path: Path) -> tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    return reader.fieldnames or [], rows


def inspect_headers(actual: Sequence[str], expected: Sequence[str], label: str) -> None:
    actual_set = set(actual)
    expected_set = set(expected)

    missing = expected_set - actual_set
    extra = actual_set - expected_set

    print(f"[{label}] Columns: {', '.join(actual) if actual else '<none>'}")
    if missing:
        print(f"  !! Missing expected columns: {', '.join(sorted(missing))}")
    if extra:
        print(f"  ?? Unexpected columns present: {', '.join(sorted(extra))}")


def canonical_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:10]


def canonical_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip()


def parse_year(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    cleaned = value.strip()
    if len(cleaned) == 4 and cleaned.isdigit():
        return int(cleaned)
    return None


def aggregate_badge_targets(rows: Iterable[Dict[str, str]]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, Dict[Optional[int], Dict[str, object]]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "title_counts": Counter(),
                "issue_dates": [],
                "expiry_dates": [],
                "issuers": Counter(),
                "rows": [],
            }
        )
    )

    for row in rows:
        title = (row.get("Badge Title") or "").strip()
        if not title:
            continue
        key = normalize_title(title)
        issue = canonical_date(row.get("Issue Date"))
        expiry = canonical_date(row.get("Expiry Date"))
        issuer = canonical_text(row.get("Issuer"))
        year = parse_year(issue[:4] if issue else None)

        bucket = grouped[key][year]
        bucket["title_counts"][title] += 1
        if issue:
            bucket["issue_dates"].append(issue)
        if expiry:
            bucket["expiry_dates"].append(expiry)
        if issuer:
            bucket["issuers"][issuer] += 1
        bucket["rows"].append(row)

    targets: Dict[str, List[Dict[str, object]]] = {}
    for key, per_year in grouped.items():
        entries: List[Dict[str, object]] = []
        for year, data in per_year.items():
            issue = max(data["issue_dates"]) if data["issue_dates"] else None
            expiry = max(data["expiry_dates"]) if data["expiry_dates"] else None
            issuer = data["issuers"].most_common(1)[0][0] if data["issuers"] else ""
            title_counts: Counter = data["title_counts"]
            preferred_title = None
            if title_counts:
                preferred_title = max(
                    title_counts.items(), key=lambda item: (item[1], len(item[0]))
                )[0]
            title_variants = sorted(title_counts.keys()) if title_counts else []
            is_certified = any("certified" in title.lower() for title in title_counts.keys())
            entries.append(
                {
                    "title_variants": title_variants,
                    "preferred_title": preferred_title or (title_variants[0] if title_variants else ""),
                    "issue_date": issue,
                    "expiry_date": expiry,
                    "issuer": issuer,
                    "year": year,
                    "rows": data["rows"],
                    "is_certified": is_certified,
                }
            )
        entries.sort(key=lambda item: (item["year"] is None, item["year"] if item["year"] is not None else 0))
        targets[key] = entries

    return targets


def aggregate_training_rows(rows: Iterable[Dict[str, str]]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, Dict[Optional[int], Dict[str, object]]] = defaultdict(
        lambda: defaultdict(
            lambda: {
                "titles": set(),
                "issuers": set(),
                "expiry_dates": set(),
                "rows": [],
                "is_certified": False,
            }
        )
    )

    for row in rows:
        title = (row.get("title") or row.get("name") or "").strip()
        if not title:
            continue
        key = normalize_title(title)
        year = parse_year(row.get("year"))
        entry = grouped[key][year]
        entry["titles"].add(title)
        issuer = canonical_text(row.get("issuer"))
        if issuer:
            entry["issuers"].add(issuer)
        expiry = canonical_date(row.get("expireDate"))
        if expiry:
            entry["expiry_dates"].add(expiry)
        entry["rows"].append(row)
        if "certified" in title.lower():
            entry["is_certified"] = True

    aggregated: Dict[str, List[Dict[str, object]]] = {}
    for key, per_year in grouped.items():
        entries: List[Dict[str, object]] = []
        for year, data in per_year.items():
            entries.append(
                {
                    "year": year,
                    "titles": sorted(data["titles"]),
                    "issuers": sorted(data["issuers"]),
                    "expiry_dates": sorted(data["expiry_dates"]),
                    "rows": data["rows"],
                    "is_certified": data["is_certified"],
                }
            )
        entries.sort(key=lambda item: (item["year"] is None, item["year"] if item["year"] is not None else 0))
        aggregated[key] = entries

    return aggregated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare Cinode trainings CSV with Credly badges CSV",
    )
    parser.add_argument(
        "trainings_csv",
        type=Path,
        nargs="?",
        default=Path("Per_Rosenlind_trainings.csv"),
        help="Path to the trainings CSV exported by the Cinode helper",
    )
    parser.add_argument(
        "badges_csv",
        type=Path,
        nargs="?",
        default=Path("../Credly/all_badges.csv"),
        help="Path to the Credly badges CSV",
    )
    args = parser.parse_args()

    trainings_headers, trainings_rows = read_csv(args.trainings_csv)
    badges_headers, badges_rows = read_csv(args.badges_csv)

    inspect_headers(trainings_headers, TRAININGS_EXPECTED_HEADERS, "Cinode Trainings")
    inspect_headers(badges_headers, BADGES_EXPECTED_HEADERS, "Credly Badges")

    training_map = aggregate_training_rows(trainings_rows)
    badge_map = aggregate_badge_targets(badges_rows)

    trainings_total = sum(len(entry["rows"]) for entries in training_map.values() for entry in entries)
    badges_total = sum(len(entry["rows"]) for entries in badge_map.values() for entry in entries)

    print()
    print(f"Trainings rows processed: {trainings_total} (unique normalized titles: {len(training_map)})")
    print(f"Badge rows processed:     {badges_total} (unique normalized titles: {len(badge_map)})")

    def pick_title(entries: List[Dict[str, object]], fallback: str = "<unknown>") -> str:
        for entry in entries:
            preferred = entry.get("preferred_title")
            if preferred:
                return preferred
            titles = entry.get("titles") or entry.get("title_variants") or []
            if titles:
                return titles[0]
        return fallback

    cert_training_keys = {
        key for key, entries in training_map.items() if any(entry["is_certified"] for entry in entries)
    }
    cert_badge_keys = {
        key for key, entries in badge_map.items() if any(entry["is_certified"] for entry in entries)
    }

    trainings_only = sorted(cert_training_keys - cert_badge_keys)
    badges_only = sorted(cert_badge_keys - cert_training_keys)

    if trainings_only:
        print("\nCertified titles present in trainings CSV but missing from badges CSV:")
        for key in trainings_only:
            sample = pick_title(training_map.get(key, []))
            years = sorted(
                {
                    str(entry["year"]) if entry["year"] is not None else "unknown"
                    for entry in training_map.get(key, [])
                    if entry["is_certified"]
                }
            )
            suffix = f" (years {', '.join(years)})" if years else ""
            print(f"  - {sample}{suffix}")
    else:
        print("\nNo certified titles found exclusively in the trainings CSV.")

    if badges_only:
        print("\nCertified titles present in badges CSV but missing from trainings CSV:")
        for key in badges_only:
            sample = pick_title(badge_map.get(key, []))
            years = sorted(
                {
                    str(entry["year"]) if entry["year"] is not None else "unknown"
                    for entry in badge_map.get(key, [])
                    if entry["is_certified"]
                }
            )
            suffix = f" (years {', '.join(years)})" if years else ""
            print(f"  - {sample}{suffix}")
    else:
        print("\nNo certified titles found exclusively in the badges CSV.")

    shared_keys = sorted(cert_training_keys & cert_badge_keys)
    if not shared_keys:
        print("\nNo overlapping certified titles found; nothing further to compare.")
        return

    print("\nComparing overlapping certified titles:")
    for key in shared_keys:
        training_entries = [entry for entry in training_map.get(key, []) if entry["is_certified"]]
        badge_entries = [entry for entry in badge_map.get(key, []) if entry["is_certified"]]

        title_display = pick_title(training_entries) or pick_title(badge_entries)
        badge_years = {entry["year"] for entry in badge_entries}
        issues: List[str] = []

        for badge_entry in badge_entries:
            year = badge_entry["year"]
            year_display = year if year is not None else "unknown"
            match = next((entry for entry in training_entries if entry["year"] == year), None)
            if not match:
                issues.append(f"  - Missing Cinode training for year {year_display}")
                continue

            cinode_issuers = match["issuers"]
            badge_issuer = canonical_text(badge_entry.get("issuer"))
            if badge_issuer:
                cinode_issuer_lower = {issuer.lower() for issuer in cinode_issuers}
                if badge_issuer.lower() not in cinode_issuer_lower:
                    cinode_display = ", ".join(cinode_issuers) if cinode_issuers else "<none>"
                    issues.append(
                        f"  - Year {year_display}: issuer mismatch (Cinode: {cinode_display}; Badge: {badge_issuer})"
                    )

            badge_expiry = badge_entry["expiry_date"]
            badge_expiry_year = badge_expiry[:4] if badge_expiry else None
            training_expiry_years = sorted({date[:4] for date in match["expiry_dates"] if date})

            if badge_expiry_year:
                if not training_expiry_years:
                    issues.append(
                        f"  - Year {year_display}: Cinode missing expiry date (badge {badge_expiry})"
                    )
                elif badge_expiry_year not in training_expiry_years:
                    issues.append(
                        f"  - Year {year_display}: expiry mismatch (Cinode years {training_expiry_years}; Badge {badge_expiry_year})"
                    )

        for training_entry in training_entries:
            if training_entry["year"] not in badge_years:
                year_display = training_entry["year"] if training_entry["year"] is not None else "unknown"
                issues.append(
                    f"  - Extra Cinode training for year {year_display} (no matching badge issuance)"
                )

        print(f"\n{title_display}:")
        if issues:
            for issue in issues:
                print(issue)
        else:
            print("  (no discrepancies)")


if __name__ == "__main__":
    main()
