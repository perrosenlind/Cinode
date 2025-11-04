"""Synchronize Cinode profile trainings with Credly badge data."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

from compare_trainings_and_badges import (
    BADGES_EXPECTED_HEADERS,
    TRAININGS_EXPECTED_HEADERS,
    inspect_headers,
    normalize_title,
    read_csv,
)
from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token
from get_cinode_user_profile import fetch_user_profile
from get_cinode_user_skills import build_user_index, prompt_for_user
from get_cinode_user_trainings import extract_trainings, training_metadata

API_BASE_URL = "https://api.cinode.com/v0.1"
DATE_SUFFIX = "T00:00:00"


def parse_badge_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:10]


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


def aggregate_badge_targets(badge_rows: Iterable[Dict[str, str]]) -> Dict[str, List[Dict[str, object]]]:
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

    for row in badge_rows:
        title = (row.get("Badge Title") or "").strip()
        if not title:
            continue
        key = normalize_title(title)
        issue = parse_badge_date(row.get("Issue Date"))
        expiry = parse_badge_date(row.get("Expiry Date"))
        issuer = canonical_text(row.get("Issuer"))
        year = int(issue[:4]) if issue and issue[:4].isdigit() else None

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


def training_year_value(training: Dict, meta: Dict[str, object]) -> Optional[int]:
    value = training.get("year")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if len(cleaned) == 4 and cleaned.isdigit():
            return int(cleaned)

    completed = canonical_date(meta.get("completed"))
    if completed and completed[:4].isdigit():
        return int(completed[:4])
    return None


def determine_training_operations(
    trainings: Iterable[Dict],
    badge_targets: Dict[str, List[Dict[str, object]]],
) -> tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    updates: List[Dict[str, object]] = []
    creations: List[Dict[str, object]] = []
    missing_templates: List[Dict[str, object]] = []

    existing_by_key: Dict[str, List[tuple[Dict, Dict]]] = defaultdict(list)
    for training in trainings:
        meta = training_metadata(training)
        title = meta["title"].strip()
        if not title or "certified" not in title.lower():
            continue
        key = normalize_title(title)
        existing_by_key[key].append((training, meta))

    for key, targets in badge_targets.items():
        certified_targets = [target for target in targets if target.get("is_certified")]
        if not certified_targets:
            continue

        existing_entries = existing_by_key.get(key, [])
        if not existing_entries:
            missing_templates.append({
                "key": key,
                "targets": certified_targets,
            })
            for target in certified_targets:
                creations.append(
                    {
                        "template_training": None,
                        "template_meta": None,
                        "target": target,
                        "key": key,
                    }
                )
            continue

        template_training, template_meta = existing_entries[0]

        existing_infos: List[Dict[str, object]] = []
        for idx, (training, meta) in enumerate(existing_entries):
            existing_infos.append(
                {
                    "index": idx,
                    "training": training,
                    "meta": meta,
                    "year": training_year_value(training, meta),
                    "training_type": training.get("trainingType"),
                }
            )

        used_existing: set[int] = set()
        matches: List[tuple[Dict[str, object], Dict[str, object]]] = []
        pending_targets: List[Dict[str, object]] = []

        for target in certified_targets:
            target_year = target["year"]
            match_info: Optional[Dict[str, object]] = None
            if target_year is not None:
                for info in existing_infos:
                    if info["index"] in used_existing:
                        continue
                    if info["year"] == target_year:
                        match_info = info
                        break
            if match_info:
                used_existing.add(match_info["index"])
                matches.append((target, match_info))
            else:
                pending_targets.append(target)

        def pick_fallback(target: Dict[str, object]) -> Optional[Dict[str, object]]:
            candidates = [info for info in existing_infos if info["index"] not in used_existing]
            if not candidates:
                return None

            target_year = target["year"]

            def candidate_sort(info: Dict[str, object]) -> tuple[int, int, int]:
                training_type = info["training_type"]
                type_rank = 0 if training_type == 1 else 1
                year = info["year"]
                if year is None or target_year is None:
                    year_distance = 0 if year == target_year else 10_000
                else:
                    year_distance = abs(year - target_year)
                return (type_rank, year_distance, info["index"])

            candidates.sort(key=candidate_sort)
            return candidates[0]

        for target in pending_targets:
            fallback_info = pick_fallback(target)
            if fallback_info is not None:
                used_existing.add(fallback_info["index"])
                matches.append((target, fallback_info))
            else:
                creations.append(
                    {
                        "template_training": template_training,
                        "template_meta": template_meta,
                        "target": target,
                        "key": key,
                    }
                )

        unused_infos = [info for info in existing_infos if info["index"] not in used_existing]
        if unused_infos and certified_targets:
            def target_distance(target: Dict[str, object], info: Dict[str, object]) -> int:
                target_year = target.get("year")
                info_year = info.get("year")
                if target_year is None or info_year is None:
                    return 0 if target_year == info_year else 10_000
                return abs(target_year - info_year)

            for info in unused_infos:
                best_target = min(certified_targets, key=lambda t: target_distance(t, info))
                matches.append((best_target, info))

        for target, info in matches:
            training = info["training"]
            meta = info["meta"]

            current_completed_raw = (
                training.get("completedWhen")
                or training.get("completedDate")
                or training.get("completionDate")
                or training.get("date")
            )
            current_expires_raw = (
                training.get("expiresWhen")
                or training.get("expirationDate")
                or training.get("expireDate")
            )

            current_completed = canonical_date(current_completed_raw)
            current_expires = canonical_date(current_expires_raw)
            current_year = training_year_value(training, meta)
            current_issuer = canonical_text(meta["issuer"])

            target_year = target["year"]
            target_expires = target["expiry_date"]
            target_completed = target["issue_date"]
            target_issuer = canonical_text(target["issuer"])
            target_title = (target.get("preferred_title") or "").strip()

            changes: Dict[str, object] = {}

            if target_completed and target_completed != current_completed:
                changes["completedWhen"] = target_completed
            if target_expires != current_expires:
                changes["expiresWhen"] = target_expires
            if target_year is not None and target_year != current_year:
                changes["year"] = target_year
            if target_year is None and current_year is not None:
                changes["year"] = None
            if target_issuer and canonical_text(current_issuer).lower() != target_issuer.lower():
                changes["issuer"] = target_issuer
            if target_title:
                current_title = (meta.get("title") or "").strip()
                if current_title != target_title:
                    changes["title"] = target_title
                current_name = (meta.get("name") or "").strip()
                if current_name != target_title:
                    changes["name"] = target_title

            if not changes:
                continue

            updates.append(
                {
                    "training": training,
                    "meta": meta,
                    "target": target,
                    "current": {
                        "completed": current_completed,
                        "expires": current_expires,
                        "year": current_year,
                        "issuer": current_issuer,
                        "title": (meta.get("title") or "").strip(),
                        "name": (meta.get("name") or "").strip(),
                    },
                    "changes": changes,
                }
            )

    return updates, creations, missing_templates


def build_update_payload(update: Dict[str, object]) -> Dict[str, object]:
    training: Dict = update["training"]
    meta: Dict[str, object] = update["meta"]
    changes: Dict[str, object] = update["changes"]
    current: Dict[str, object] = update["current"]

    payload: Dict[str, object] = {}

    training_type = training.get("trainingType")
    if training_type is not None:
        payload["trainingType"] = training_type

    if "companyTrainingId" in training and training.get("companyTrainingId") is not None:
        payload["companyTrainingId"] = training.get("companyTrainingId")

    if "code" in training and training.get("code") is not None:
        payload["code"] = training.get("code")

    payload["saveTo"] = training.get("saveTo") or "Profile"

    def iso_date(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        trimmed = canonical_date(value)
        if not trimmed:
            return None
        return f"{trimmed}{DATE_SUFFIX}"

    completed_value = changes.get("completedWhen") if "completedWhen" in changes else current.get("completed")
    completed_iso = iso_date(completed_value)
    if "completedWhen" in changes or completed_iso is not None:
        payload["completedWhen"] = completed_iso
        payload["completedDate"] = completed_iso
        payload["completionDate"] = completed_iso
        payload["date"] = completed_iso

    expires_value = changes.get("expiresWhen") if "expiresWhen" in changes else current.get("expires")
    expires_iso = iso_date(expires_value)
    if "expiresWhen" in changes or expires_iso is not None:
        payload["expiresWhen"] = expires_iso
        payload["expirationDate"] = expires_iso
        payload["expireDate"] = expires_iso

    if "year" in changes:
        payload["year"] = changes["year"]
    else:
        year = current.get("year")
        if year is not None:
            payload["year"] = year

    def default_text(key: str, fallback: Optional[str] = "") -> str:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        translation = meta.get("translation") or {}
        if isinstance(translation, dict):
            trans_value = translation.get(key)
            if isinstance(trans_value, str) and trans_value.strip():
                return trans_value.strip()
        training_value = training.get(key)
        if isinstance(training_value, str) and training_value.strip():
            return training_value.strip()
        return fallback or ""

    title_value = changes.get("title") if "title" in changes else default_text("title")
    payload["title"] = title_value

    issuer_value = changes.get("issuer") if "issuer" in changes else default_text("issuer") or default_text("provider")
    if issuer_value:
        payload["issuer"] = issuer_value

    name_value = changes.get("name") if "name" in changes else default_text("name", fallback=title_value)
    if name_value:
        payload["name"] = name_value

    translations_payload: List[Dict[str, object]] = []
    for translation in training.get("translations") or []:
        entry: Dict[str, object] = {}
        profile_translation_id = translation.get("profileTranslationId")
        if profile_translation_id is not None:
            entry["profileTranslationId"] = profile_translation_id

        language_id = translation.get("languageId")
        if language_id is None:
            profile_translation = translation.get("profileTranslation") or {}
            if isinstance(profile_translation, dict):
                branch = profile_translation.get("languageBranch") or {}
                if isinstance(branch, dict):
                    language_id = branch.get("languageId")
                    language = branch.get("language") if isinstance(branch.get("language"), dict) else None
                    if language and not language_id:
                        language_id = language.get("languageId")
        if language_id is not None:
            entry["languageId"] = language_id

        translation_title = title_value if "title" in changes else translation.get("title") or title_value
        entry["title"] = translation_title

        translation_issuer = issuer_value if "issuer" in changes else translation.get("issuer") or issuer_value
        if translation_issuer:
            entry["issuer"] = translation_issuer

        if translation.get("supplier") is not None:
            entry["supplier"] = translation.get("supplier")

        if translation.get("description") is not None:
            entry["description"] = translation.get("description")

        if translation.get("name") is not None:
            entry["name"] = translation.get("name")

        translations_payload.append(entry)

    if translations_payload:
        payload["translations"] = translations_payload

    return payload


def build_creation_payload(
    template_training: Optional[Dict],
    template_meta: Optional[Dict[str, object]],
    target: Dict[str, object],
    badge_key: Optional[str] = None,
) -> Dict[str, object]:
    preferred_title = (target.get("preferred_title") or "").strip()
    if not preferred_title:
        if template_meta:
            preferred_title = (template_meta.get("title") or template_meta.get("name") or "").strip()
        if not preferred_title and target.get("title_variants"):
            preferred_title = target["title_variants"][0]
    if not preferred_title:
        preferred_title = badge_key or "Unnamed training"

    title = preferred_title
    name = preferred_title

    target_issuer = canonical_text(target.get("issuer"))
    template_issuer = canonical_text(template_meta.get("issuer")) if template_meta else ""
    issuer = target_issuer or template_issuer

    template_provider = canonical_text(template_training.get("provider")) if template_training else ""
    provider = issuer or template_provider

    training_type = None
    if template_training is not None:
        training_type = template_training.get("trainingType")
    if training_type is None:
        training_type = 1 if target.get("is_certified") else 0

    payload: Dict[str, object] = {
        "trainingType": training_type,
        "title": title,
        "name": name,
        "issuer": issuer,
        "provider": provider or issuer,
        "saveTo": (template_training.get("saveTo") if template_training else None) or "Profile",
    }

    if template_training:
        company_training_id = template_training.get("companyTrainingId")
        if company_training_id is not None:
            payload["companyTrainingId"] = company_training_id

        code = template_training.get("code")
        if code is not None:
            payload["code"] = code

    target_year = target.get("year")
    if target_year is not None:
        payload["year"] = target_year

    issue_date = target.get("issue_date")
    if issue_date:
        iso_value = f"{issue_date}{DATE_SUFFIX}"
        payload["completedWhen"] = iso_value
        payload["completedDate"] = iso_value
        payload["completionDate"] = iso_value
        payload["date"] = iso_value

    expiry_date = target.get("expiry_date")
    if expiry_date:
        iso_value = f"{expiry_date}{DATE_SUFFIX}"
        payload["expiresWhen"] = iso_value
        payload["expirationDate"] = iso_value
        payload["expireDate"] = iso_value

    def translation_language_id(translation: Dict[str, object]) -> Optional[int]:
        if translation.get("languageId") is not None:
            return translation.get("languageId")
        profile_translation = translation.get("profileTranslation")
        if isinstance(profile_translation, dict):
            branch = profile_translation.get("languageBranch")
            if isinstance(branch, dict):
                language_id = branch.get("languageId")
                if language_id is not None:
                    return language_id
                language = branch.get("language")
                if isinstance(language, dict) and language.get("languageId") is not None:
                    return language.get("languageId")
        return None

    translations_payload: List[Dict[str, object]] = []
    if template_training:
        for translation in template_training.get("translations") or []:
            entry: Dict[str, object] = {}
            language_id = translation_language_id(translation)
            if language_id is not None:
                entry["languageId"] = language_id
            entry["title"] = title
            if issuer:
                entry["issuer"] = issuer
            supplier = translation.get("supplier")
            if supplier is not None:
                entry["supplier"] = supplier
            description = translation.get("description")
            if description is not None:
                entry["description"] = description
            translations_payload.append(entry)

    if translations_payload:
        payload["translations"] = translations_payload

    return payload


def apply_changes(
    updates: Iterable[Dict[str, object]],
    creations: Iterable[Dict[str, object]],
    access_token: str,
    company_id: int,
    user_id: int,
    dry_run: bool,
) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    updates_list = list(updates)
    creations_list = list(creations)

    update_actions: List[tuple[Dict[str, object], Dict[str, object]]] = []
    if updates_list:
        print("Updates:")
    for update in updates_list:
        payload = build_update_payload(update)
        update_actions.append((update, payload))

        training = update["training"]
        training_id = training.get("id")
        title = update["meta"]["title"]
        print(f"- {title} [ID: {training_id}]")
        for field, display_key in (
            ("completedWhen", "completed"),
            ("expiresWhen", "expires"),
            ("year", "year"),
            ("issuer", "issuer"),
            ("title", "title"),
            ("name", "name"),
        ):
            if field not in update["changes"]:
                continue
            current_value = update["current"].get(display_key)
            new_value = update["changes"][field]
            print(f"    {field}: {current_value!r} -> {new_value!r}")

    creation_actions: List[tuple[Dict[str, object], Dict[str, object]]] = []
    if creations_list:
        print("Creations:")
    for creation in creations_list:
        payload = build_creation_payload(
            creation["template_training"],
            creation["template_meta"],
            creation["target"],
            creation.get("key"),
        )
        creation_actions.append((creation, payload))

        template_meta = creation.get("template_meta") or {}
        target = creation["target"]
        title_variants = target.get("title_variants") or []
        preferred_title = (target.get("preferred_title") or "").strip()
        title = (
            preferred_title
            or template_meta.get("title")
            or template_meta.get("name")
            or (title_variants[0] if title_variants else creation.get("key") or "Unnamed training")
        )
        target_year = creation["target"].get("year")
        target_year_display = target_year if target_year is not None else "unknown"
        print(f"- {title} (new entry for year {target_year_display})")
        print(f"    issue_date: {creation['target']['issue_date']!r}")
        print(f"    expiry_date: {creation['target']['expiry_date']!r}")
        print(f"    issuer: {payload.get('issuer')!r}")

    if dry_run:
        return

    confirm = input("Proceed with these changes? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Aborted by user; no changes applied.")
        return

    for update, payload in update_actions:
        training = update["training"]
        training_id = training.get("id")
        url = f"{API_BASE_URL}/companies/{company_id}/users/{user_id}/profile/trainings/{training_id}"
        response = requests.put(url, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 204):
            detail = None
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            message = detail if detail else f"HTTP {response.status_code}: {response.reason or ''}".strip()
            raise RuntimeError(f"Failed to update training {training_id}: {message}")

    for creation, payload in creation_actions:
        url = f"{API_BASE_URL}/companies/{company_id}/users/{user_id}/profile/trainings"
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 201):
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            title = creation["template_meta"].get("title") or creation["target"]["title_variants"][0]
            raise RuntimeError(f"Failed to create training '{title}': {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update Cinode profile trainings based on Credly badges",
    )
    parser.add_argument(
        "--trainings-csv",
        type=Path,
        default=Path("Per_Rosenlind_trainings.csv"),
        help="Path to the Cinode trainings CSV export",
    )
    parser.add_argument(
        "--badges-csv",
        type=Path,
        default=Path("../Credly/all_badges.csv"),
        help="Path to the Credly badges CSV",
    )
    parser.add_argument(
        "--user",
        "-u",
        dest="user_query",
        help="Pre-filter the Cinode user list and auto-select when unique",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the updates instead of running in dry-run mode",
    )
    args = parser.parse_args()

    trainings_headers, trainings_rows = read_csv(args.trainings_csv)
    badges_headers, badges_rows = read_csv(args.badges_csv)

    inspect_headers(trainings_headers, TRAININGS_EXPECTED_HEADERS, "Cinode Trainings")
    inspect_headers(badges_headers, BADGES_EXPECTED_HEADERS, "Credly Badges")

    badge_targets = aggregate_badge_targets(badges_rows)

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
    trainings = extract_trainings(profile)
    print(f"Loaded {len(trainings)} trainings from Cinode profile.")

    updates, creations, missing_templates = determine_training_operations(trainings, badge_targets)
    if missing_templates:
        print("\nNo existing Cinode training templates found for these certified badges; new entries will be created from badge data:")
        for missing in missing_templates:
            titles = {variant for target in missing["targets"] for variant in target["title_variants"]}
            sample = sorted(titles)[0] if titles else missing["key"]
            years = sorted({target["year"] for target in missing["targets"] if target["year"] is not None})
            years_display = f" years {', '.join(map(str, years))}" if years else ""
            print(f"  - {sample}{years_display}")

    if not updates and not creations:
        print("No certified trainings require changes.")
        return

    mode = "dry-run" if not args.apply else "apply"
    print(f"Planned changes ({mode}):")
    apply_changes(
        updates,
        creations,
        access_token,
        company_id,
        selected_user_id,
        dry_run=not args.apply,
    )

    if not args.apply:
        print("\nDry run complete. Re-run with --apply to push the changes.")
    else:
        print("\nChanges applied successfully.")


if __name__ == "__main__":
    main()
