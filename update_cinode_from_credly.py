"""Select a Cinode user, locate their Credly profile, and list displayed badges."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests

try:
    from rich import box  # type: ignore[import]
    from rich.console import Console  # type: ignore[import]
    from rich.panel import Panel  # type: ignore[import]
    from rich.table import Table  # type: ignore[import]
    from rich.text import Text  # type: ignore[import]
except Exception:  # pragma: no cover - Rich is optional
    Console = None
    Table = None
    Panel = None
    Text = None
    box = None

if TYPE_CHECKING:  # pragma: no cover - typing aid only
    from rich.console import Console as RichConsole  # type: ignore[import]
else:  # pragma: no cover - runtime fallback
    RichConsole = Any

from get_cinode_token import CREDENTIALS_FILE, load_credentials
from get_cinode_teams import ensure_access_token
from get_cinode_user_skills import build_user_index, prompt_for_user
from get_cinode_user_profile import fetch_user_profile
from get_cinode_user_trainings import extract_trainings, training_metadata
from compare_trainings_and_badges import normalize_title
from sync_trainings_from_badges import build_update_payload

API_BASE_URL = "https://api.cinode.com/v0.1"
SOCIAL_FIELD_CANDIDATES: Dict[str, list[str]] = {
    "LinkedIn": ["linkedIn", "linkedInUrl", "linkedin", "linkedinUrl"],
    "Twitter": ["twitter"],
    "Homepage": ["homepage", "website"],
    "Blog": ["blog"],
    "GitHub": ["gitHub", "github"],
}
TARGET_SUBSTRING = "www.credly.com"
CREDLY_HOST = "https://www.credly.com"
CREDLY_TIMEOUT = 20
DATE_SUFFIX = "T00:00:00"

ConsoleType = Optional[RichConsole]
SUPPRESS_OUTPUT = False


def emit(message: str, console: ConsoleType, style: Optional[str] = None) -> None:
    if SUPPRESS_OUTPUT:
        return
    if console:
        console.print(message, style=style)
    else:
        print(message)


def emit_blank(console: ConsoleType) -> None:
    emit("", console)


def _add_years(base: datetime, years: int) -> datetime:
    try:
        return base.replace(year=base.year + years)
    except ValueError:
        return base.replace(month=2, day=28, year=base.year + years)


def json_default(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def fetch_user_details(access_token: str, company_id: int, company_user_id: int) -> Dict[str, Any]:
    url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}"
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
            f"Failed to parse user response for ID {company_user_id}: {response.text!r}"
        ) from exc


def extract_social_links(user_payload: Dict[str, Any]) -> Dict[str, str]:
    links: Dict[str, str] = {}
    if not user_payload:
        return links

    for label, keys in SOCIAL_FIELD_CANDIDATES.items():
        for raw_key in keys:
            value = user_payload.get(raw_key)
            if isinstance(value, str):
                value = value.strip()
            if not value:
                continue
            if TARGET_SUBSTRING.lower() not in value.lower():
                continue
            links[label] = value
            break
    return links


def print_social_links(user_entry: Dict, social_links: Dict[str, str], console: ConsoleType) -> None:
    name_parts = [user_entry.get("firstName"), user_entry.get("lastName")]
    full_name = " ".join(part for part in name_parts if part) or "<unknown>"
    user_id = user_entry.get("companyUserId", "N/A")

    if console and Table is not None:
        title = f"Social links for {full_name} (User ID: {user_id})"
        if social_links:
            table = Table(title=title, box=box.SIMPLE_HEAVY)
            table.add_column("Label", style="cyan", no_wrap=True)
            table.add_column("URL", style="magenta")
            for label, value in social_links.items():
                table.add_row(label, value)
            console.print(table)
        else:
            console.print(Panel("No social links available.", title=title, box=box.SIMPLE))
        return

    emit(f"Social links for {full_name} (User ID: {user_id})", console)
    if not social_links:
        emit("- No social links available.", console)
        return

    for label, value in social_links.items():
        emit(f"- {label}: {value}", console)


def categorize_learning_item(title: str) -> str:
    if "certified" in (title or "").lower():
        return "Certification"
    return "Course"


def _strip_issuer_tokens(title: str, issuer: str) -> str:
    if not title or not issuer:
        return title
    cleaned = title
    issuer_pattern = re.escape(issuer.strip())
    if issuer_pattern:
        cleaned = re.sub(issuer_pattern, " ", cleaned, flags=re.IGNORECASE)
    issuer_tokens = re.findall(r"[a-z0-9]+", issuer.lower())
    for token in issuer_tokens:
        cleaned = re.sub(rf"\b{re.escape(token)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or title


def generate_match_keys(title: str, issuer: str) -> Set[str]:
    variants = set()
    base = (title or "").strip()
    if base:
        variants.add(base)
        if issuer:
            stripped = _strip_issuer_tokens(base, issuer)
            variants.add(stripped)
        words = base.split()
        if len(words) > 1:
            trimmed_variant = " ".join(words[1:]).strip()
            if trimmed_variant:
                variants.add(trimmed_variant)

    keys: Set[str] = set()
    for variant in variants:
        normalized = normalize_title(variant)
        if normalized:
            keys.add(normalized)
    return keys


def extract_numeric_tokens(text: str) -> Set[str]:
    tokens: Set[str] = set()
    if not text:
        return tokens
    for match in re.findall(r"\d+(?:\.\d+)?", text):
        tokens.add(match)
    return tokens


def build_credly_badge_api_url(profile_url: str) -> str:
    parsed = urlparse(profile_url)
    if not parsed.path:
        raise ValueError("Credly URL is missing a path segment")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ValueError("Unable to determine Credly profile slug")

    slug: Optional[str] = None

    for idx, segment in enumerate(segments):
        if segment.lower() == "users" and idx + 1 < len(segments):
            slug = segments[idx + 1]
            break

    if slug is None and segments[-1].lower() in {"badges", "badge"} and len(segments) >= 2:
        slug = segments[-2]

    if slug is None:
        slug = segments[-1]

    if not slug:
        raise ValueError("Credly profile slug could not be resolved")

    return f"{CREDLY_HOST}/users/{slug}/badges.json"


def normalize_issuer(badge: Dict[str, Any]) -> str:
    template_issuer = badge.get("badge_template", {}).get("issuer", {})
    for entity in template_issuer.get("entities", []) or []:
        entity_data = entity.get("entity", {})
        name = entity_data.get("name")
        if name:
            return name

    issuer_info = badge.get("issuer", {})
    for entity in issuer_info.get("entities", []) or []:
        entity_data = entity.get("entity", {})
        name = entity_data.get("name")
        if name:
            return name

    summary = issuer_info.get("summary") or template_issuer.get("summary")
    return summary or "Unknown issuer"


def fetch_badges_from_credly(profile_url: str, console: ConsoleType = None) -> List[Dict[str, str]]:
    badges: List[Dict[str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    try:
        next_url = build_credly_badge_api_url(profile_url)
    except ValueError as exc:
        emit(f"Invalid Credly link '{profile_url}': {exc}", console, style="red")
        return badges

    while next_url:
        try:
            response = requests.get(next_url, timeout=CREDLY_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            emit(f"Failed to fetch Credly badges from {next_url}: {exc}", console, style="red")
            break

        payload = response.json()
        data = payload.get("data") or []

        for entry in data:
            template = entry.get("badge_template", {})
            share_url = entry.get("share_url") or entry.get("url") or profile_url
            record = {
                "title": template.get("name") or "Unnamed badge",
                "issued_at": entry.get("issued_at_date") or entry.get("issued_at") or "",
                "expires_at": entry.get("expires_at_date") or entry.get("expires_at") or "",
                "issuer": normalize_issuer(entry),
                "badge_url": share_url,
                "category": "",  # placeholder, updated below
                "expires_is_derived": False,
            }
            record["category"] = categorize_learning_item(record["title"])
            if not record.get("expires_at") and record["category"] == "Course":
                issued_raw = record.get("issued_at")
                issuer = record.get("issuer", "")
                if issued_raw and "fortinet" in issuer.lower():
                    try:
                        issued_date = datetime.strptime(issued_raw[:10], "%Y-%m-%d")
                    except ValueError:
                        issued_date = None
                    if issued_date is not None:
                        derived_expiry = _add_years(issued_date, 2).strftime("%Y-%m-%d")
                        record["expires_at"] = derived_expiry
                        record["expires_is_derived"] = True
            fingerprint = (record["title"], record["issued_at"], record["badge_url"])
            if fingerprint in seen_keys:
                continue
            seen_keys.add(fingerprint)
            badges.append(record)

        metadata = payload.get("metadata") or {}
        next_url = metadata.get("next_page_url")
        if next_url and next_url.startswith("/"):
            next_url = f"{CREDLY_HOST}{next_url}"

    return badges


def print_badge_summary(badges: List[Dict[str, str]], console: ConsoleType) -> None:
    if not badges:
        emit("No Credly badges were found for this link.", console, style="yellow")
        return

    if console and Table is not None:
        table = Table(
            title=f"Credly badges ({len(badges)})",
            box=box.SIMPLE,
            show_lines=False,
        )
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Badge", style="green")
        table.add_column("Issuer", style="magenta")
        table.add_column("Issued", style="white")
        table.add_column("Expires", style="white")
        table.add_column("Share URL", style="blue")

        for badge in badges:
            expires_value = badge.get("expires_at") or ""
            if badge.get("expires_is_derived") and expires_value:
                expires_cell = Text(expires_value, style="orange3")
            else:
                expires_cell = expires_value

            table.add_row(
                badge.get("category") or "Course",
                badge.get("title") or "<untitled>",
                badge.get("issuer") or "<unknown>",
                badge.get("issued_at") or "Unknown",
                expires_cell,
                badge.get("badge_url") or "",
            )

        console.print(table)
        return

    emit(f"Credly badges ({len(badges)}):", console)
    for badge in badges:
        title = badge["title"]
        issuer = badge["issuer"]
        issued = badge["issued_at"] or "Unknown issue date"
        expires = badge["expires_at"]
        details = f"Issued {issued}"
        if expires:
            details += f", expires {expires}"
        share_url = badge.get("badge_url")
        category = badge.get("category") or "Course"
        prefix = f"[{category}] "
        if share_url:
            derived_note = " (derived)" if badge.get("expires_is_derived") else ""
            emit(f"- {prefix}{title} [{issuer}] — {details}{derived_note} — {share_url}", console)
        else:
            derived_note = " (derived)" if badge.get("expires_is_derived") else ""
            emit(f"- {prefix}{title} [{issuer}] — {details}{derived_note}", console)


def iso_date_value(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    date_part = trimmed[:10]
    if len(date_part) != 10:
        return None
    return f"{date_part}{DATE_SUFFIX}"


def build_cinode_training_records(trainings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for training in trainings:
        meta = training_metadata(training)
        title = (meta.get("title") or meta.get("name") or "").strip()
        if not title:
            continue
        records.append(
            {
                "title": title,
                "issuer": (meta.get("issuer") or "").strip(),
                "completed": (meta.get("completed") or "").strip(),
                "expires": (meta.get("expires") or "").strip(),
                "category": categorize_learning_item(title),
                "description": (
                    (meta.get("translation") or {}).get("description")
                    or training.get("description")
                    or ""
                ),
                "training_id": training.get("id"),
                "training_type": training.get("trainingType"),
                "raw_training": training,
                "meta": meta,
            }
        )
    return records


def build_creation_payload_from_badge(record: Dict[str, Any]) -> Dict[str, Any]:
    title = record.get("title") or "Unnamed training"
    issuer = record.get("issuer") or ""
    category = record.get("category") or categorize_learning_item(title)
    payload: Dict[str, Any] = {
        "trainingType": 1 if category == "Certification" else 0,
        "title": title,
        "name": title,
        "issuer": issuer,
        "provider": issuer or record.get("issuer") or "",
        "saveTo": "Profile",
    }

    issued_iso = iso_date_value(record.get("issued_at"))
    if issued_iso:
        payload["completedWhen"] = issued_iso
        payload["completedDate"] = issued_iso
        payload["completionDate"] = issued_iso
        payload["date"] = issued_iso
        if issued_iso[:4].isdigit():
            payload["year"] = int(issued_iso[:4])

    expires_iso = iso_date_value(record.get("expires_at"))
    if expires_iso:
        payload["expiresWhen"] = expires_iso
        payload["expirationDate"] = expires_iso
        payload["expireDate"] = expires_iso

    if record.get("expires_is_derived") and record.get("expires_at"):
        issued_txt = record.get("issued_at") or "unknown issue date"
        payload["description"] = (
            f"Expiry auto-derived: set to {record['expires_at']} (two years after issue date {issued_txt})."
        )

    return payload


def create_missing_trainings(
    access_token: str,
    company_id: int,
    company_user_id: int,
    records: List[Dict[str, Any]],
    console: ConsoleType,
) -> bool:
    if not records:
        emit("No missing Credly badges to add in Cinode.", console, style="green")
        return False

    emit_blank(console)
    emit("Planned Cinode creations:", console, style="bold cyan")

    if console and Table is not None:
        table = Table(box=box.SIMPLE)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Issued", style="white", no_wrap=True)
        table.add_column("Expires", style="white", no_wrap=True)
        for record in records:
            title = record.get("title") or "<unknown>"
            category = record.get("category") or categorize_learning_item(title)
            expires_display = record.get("expires_at") or ""
            if record.get("expires_is_derived") and expires_display:
                expires_cell = Text(expires_display, style="orange3")
            else:
                expires_cell = expires_display
            table.add_row(
                category,
                title,
                record.get("issued_at") or "N/A",
                expires_cell,
            )
        console.print(table)
    else:
        for record in records:
            title = record.get("title") or "<unknown>"
            category = record.get("category") or categorize_learning_item(title)
            issued = record.get("issued_at") or "N/A"
            expires = record.get("expires_at") or ""
            extra = f", expires {expires}" if expires else ""
            derived_note = " (derived expiry)" if record.get("expires_is_derived") else ""
            emit(f"  - [{category}] {title} (issued {issued}{extra}){derived_note}", console)

    confirm = input("Create these trainings in Cinode? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        emit("Aborted; no trainings were created.", console, style="yellow")
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}/profile/trainings"

    creations = 0
    for record in records:
        payload = build_creation_payload_from_badge(record)
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 201):
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(
                f"Failed to create training '{record.get('title', 'Unnamed')}' - {detail}"
            )
        emit(f"Created Cinode training: {record.get('title')}", console, style="green")
        creations += 1

    return creations > 0


def apply_title_renames(
    access_token: str,
    company_id: int,
    company_user_id: int,
    mismatches: List[Dict[str, Any]],
    console: ConsoleType,
) -> bool:
    actionable: List[Dict[str, Any]] = []

    for entry in mismatches or []:
        cinode_record = entry.get("cinode_record") or {}
        credly_record = entry.get("credly_record") or {}
        raw_training = cinode_record.get("raw_training") or {}
        training_id = raw_training.get("id") or raw_training.get("profileTrainingId")
        if not training_id:
            continue

        new_title = (credly_record.get("title") or entry.get("credly_title") or "").strip()
        current_title = (cinode_record.get("title") or entry.get("cinode_title") or "").strip()
        if not new_title or not current_title or new_title == current_title:
            continue

        actionable.append(
            {
                "training_id": training_id,
                "raw_training": raw_training,
                "cinode_record": cinode_record,
                "credly_record": credly_record,
                "current_title": current_title,
                "new_title": new_title,
            }
        )

    if not actionable:
        emit("No actionable title mismatches were found for renaming.", console, style="yellow")
        return False

    emit_blank(console)
    emit("Planned Cinode title updates (Credly → Cinode):", console, style="bold cyan")
    if console and Table is not None:
        table = Table(box=box.SIMPLE)
        table.add_column("Training ID", style="white", no_wrap=True)
        table.add_column("Current title", style="red")
        table.add_column("Credly title", style="green")
        for item in actionable:
            table.add_row(
                str(item["training_id"]),
                item["current_title"],
                item["new_title"],
            )
        console.print(table)
    else:
        for item in actionable:
            emit(f"  - '{item['current_title']}' → '{item['new_title']}'", console)

    confirm = input("Apply these title updates in Cinode? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        emit("Aborted; no title updates were applied.", console, style="yellow")
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    updates_applied = 0
    for item in actionable:
        cinode_record = item["cinode_record"]
        credly_record = item["credly_record"]
        raw_training = item["raw_training"]
        meta = cinode_record.get("meta") or {}

        current = {
            "completed": cinode_record.get("completed"),
            "expires": cinode_record.get("expires"),
            "year": raw_training.get("year"),
            "issuer": cinode_record.get("issuer") or meta.get("issuer") or raw_training.get("issuer") or raw_training.get("provider"),
            "title": item["current_title"],
            "name": meta.get("name") or item["current_title"],
        }

        update = {
            "training": raw_training,
            "meta": meta,
            "changes": {
                "title": item["new_title"],
                "name": item["new_title"],
            },
            "current": current,
        }

        payload = build_update_payload(update)

        training_id = item["training_id"]
        url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}/profile/trainings/{training_id}"
        response = requests.put(url, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 204):
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(
                f"Failed to update training '{item['current_title']}' (ID {training_id}) - {detail}"
            )

        updates_applied += 1
        emit(
            f"Updated Cinode training {training_id}: '{item['current_title']}' → '{item['new_title']}'",
            console,
            style="green",
        )

    return updates_applied > 0


def apply_expiry_updates(
    access_token: str,
    company_id: int,
    company_user_id: int,
    mismatches: List[Dict[str, Any]],
    console: ConsoleType,
) -> bool:
    actionable: List[Dict[str, Any]] = []

    for entry in mismatches or []:
        if not (entry.get("needs_expiry_update") or entry.get("needs_description_update")):
            continue

        cinode_record = entry.get("cinode_record") or {}
        credly_record = entry.get("credly_record") or {}
        raw_training = cinode_record.get("raw_training") or {}
        training_id = raw_training.get("id") or raw_training.get("profileTrainingId")
        if not training_id:
            continue

        desired_expires = entry.get("credly_expires") if entry.get("needs_expiry_update") else None
        expected_description = entry.get("expected_description") or ""
        current_description = entry.get("cinode_description") or ""
        new_description: Optional[str] = None

        if entry.get("needs_description_update") and expected_description:
            stripped_current = current_description.strip()
            if expected_description not in stripped_current:
                if "Expiry auto-derived" in stripped_current:
                    substituted = re.sub(
                        r"Expiry auto-derived:[^\n]*",
                        expected_description,
                        stripped_current,
                        count=1,
                    )
                    if expected_description in substituted:
                        new_description = substituted
                    else:
                        new_description = f"{stripped_current}\n\n{expected_description}"
                elif stripped_current:
                    new_description = f"{stripped_current}\n\n{expected_description}"
                else:
                    new_description = expected_description
        elif entry.get("needs_description_update"):
            new_description = current_description.strip() or None

        if desired_expires is None and new_description is None:
            continue

        meta = cinode_record.get("meta") or {}
        actionable.append(
            {
                "training_id": training_id,
                "raw_training": raw_training,
                "cinode_record": cinode_record,
                "credly_record": credly_record,
                "new_expires": desired_expires,
                "new_description": new_description,
                "current_description": current_description,
                "meta": meta,
            }
        )

    if not actionable:
        return False

    emit_blank(console)
    emit("Planned Cinode expiry/description updates:", console, style="bold cyan")
    if console and Table is not None:
        table = Table(box=box.SIMPLE)
        table.add_column("Training ID", style="white", no_wrap=True)
        table.add_column("Title", style="yellow")
        table.add_column("Current expiry", style="white", no_wrap=True)
        table.add_column("New expiry", style="white", no_wrap=True)
        table.add_column("Description", style="white")
        for item in actionable:
            title = item["cinode_record"].get("title") or item["cinode_record"].get("meta", {}).get("title") or "<untitled>"
            current_expiry = item["cinode_record"].get("expires") or ""
            new_expiry = item.get("new_expires") or current_expiry
            desc_status = "Update" if item.get("new_description") else "(unchanged)"
            table.add_row(
                str(item["training_id"]),
                title,
                current_expiry,
                new_expiry,
                desc_status,
            )
        console.print(table)
    else:
        for item in actionable:
            title = item["cinode_record"].get("title") or "<untitled>"
            current_expiry = item["cinode_record"].get("expires") or ""
            new_expiry = item.get("new_expires") or current_expiry
            needs_desc = " with description note update" if item.get("new_description") else ""
            emit(
                f"  - {title}: {current_expiry or 'N/A'} → {new_expiry or 'N/A'}{needs_desc}",
                console,
            )

    confirm = input("Apply these expiry/description updates in Cinode? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        emit("Aborted; no expiry updates were applied.", console, style="yellow")
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    updates_applied = 0
    for item in actionable:
        cinode_record = item["cinode_record"]
        raw_training = item["raw_training"]
        meta = item["meta"]
        changes: Dict[str, Any] = {}

        if item.get("new_expires") is not None:
            changes["expiresWhen"] = item["new_expires"]
        if item.get("new_description") is not None:
            changes["description"] = item["new_description"]

        current = {
            "completed": cinode_record.get("completed"),
            "expires": cinode_record.get("expires"),
            "year": raw_training.get("year"),
            "issuer": cinode_record.get("issuer")
            or meta.get("issuer")
            or raw_training.get("issuer")
            or raw_training.get("provider"),
            "title": cinode_record.get("title") or meta.get("title") or raw_training.get("title"),
            "name": meta.get("name") or cinode_record.get("title") or raw_training.get("name"),
            "description": item.get("current_description") or "",
        }

        update_payload = {
            "training": raw_training,
            "meta": meta,
            "changes": changes,
            "current": current,
        }

        payload = build_update_payload(update_payload)
        if item.get("new_description") is not None:
            payload["description"] = item["new_description"]

        training_id = item["training_id"]
        url = f"{API_BASE_URL}/companies/{company_id}/users/{company_user_id}/profile/trainings/{training_id}"
        response = requests.put(url, headers=headers, json=payload, timeout=30)
        if response.status_code not in (200, 204):
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(
                f"Failed to update training '{cinode_record.get('title', 'Unnamed')}' (ID {training_id}) - {detail}"
            )

        updates_applied += 1
        emit(
            f"Updated Cinode training {training_id}: expiry {'adjusted' if item.get('new_expires') else 'unchanged'}; description {'updated' if item.get('new_description') else 'unchanged'}.",
            console,
            style="green",
        )

    return updates_applied > 0


def compare_credly_and_cinode(
    credly_records: List[Dict[str, Any]],
    cinode_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    credly_keys: List[Set[str]] = [generate_match_keys(rec.get("title", ""), rec.get("issuer", "")) for rec in credly_records]
    cinode_keys: List[Set[str]] = [generate_match_keys(rec.get("title", ""), rec.get("issuer", "")) for rec in cinode_records]

    cinode_key_index: Dict[str, List[int]] = {}
    for idx, keys in enumerate(cinode_keys):
        for key in keys:
            cinode_key_index.setdefault(key, []).append(idx)

    matched_pairs: List[tuple[int, int]] = []
    matched_cinode: Set[int] = set()
    matched_credly: Set[int] = set()

    def similarity_score(cred_idx: int, cinode_idx: int) -> float:
        cred = credly_records[cred_idx]
        cinode = cinode_records[cinode_idx]
        cred_keys_set = credly_keys[cred_idx]
        cinode_keys_set = cinode_keys[cinode_idx]
        intersection = len(cred_keys_set & cinode_keys_set)
        ratio = SequenceMatcher(None, (cred.get("title") or "").lower(), (cinode.get("title") or "").lower()).ratio()
        cred_numbers = extract_numeric_tokens(cred.get("title") or "")
        cinode_numbers = extract_numeric_tokens(cinode.get("title") or "")
        if cred_numbers and cinode_numbers and cred_numbers.isdisjoint(cinode_numbers):
            return 0.0
        category_bonus = 0.2 if cred.get("category") == cinode.get("category") else 0
        return intersection * 5 + ratio + category_bonus

    for cred_idx, cred_record in enumerate(credly_records):
        keys = credly_keys[cred_idx]
        candidate_indices: Set[int] = set()
        for key in keys:
            candidate_indices.update(cinode_key_index.get(key, []))

        best_idx: Optional[int] = None
        best_score = 0.0
        for cinode_idx in candidate_indices:
            if cinode_idx in matched_cinode:
                continue
            score = similarity_score(cred_idx, cinode_idx)
            if score > best_score:
                best_idx = cinode_idx
                best_score = score

        if best_idx is None:
            # fallback using fuzzy ratio
            for cinode_idx, cinode_record in enumerate(cinode_records):
                if cinode_idx in matched_cinode:
                    continue
                ratio = SequenceMatcher(None, (cred_record.get("title") or "").lower(), (cinode_record.get("title") or "").lower()).ratio()
                cred_numbers = extract_numeric_tokens(cred_record.get("title") or "")
                cinode_numbers = extract_numeric_tokens(cinode_record.get("title") or "")
                if cred_numbers and cinode_numbers and cred_numbers.isdisjoint(cinode_numbers):
                    continue
                if ratio >= 0.92:
                    score = similarity_score(cred_idx, cinode_idx)
                    if score > best_score:
                        best_idx = cinode_idx
                        best_score = score

        if best_idx is not None and best_score > 0:
            matched_pairs.append((cred_idx, best_idx))
            matched_cinode.add(best_idx)
            matched_credly.add(cred_idx)

    credly_only = [credly_records[idx] for idx in range(len(credly_records)) if idx not in matched_credly]
    cinode_only = [cinode_records[idx] for idx in range(len(cinode_records)) if idx not in matched_cinode]

    mismatched_dates: List[Dict[str, Any]] = []
    title_mismatches: List[Dict[str, Any]] = []
    expiry_mismatches: List[Dict[str, Any]] = []

    for cred_idx, cinode_idx in matched_pairs:
        credly_record = credly_records[cred_idx]
        cinode_record = cinode_records[cinode_idx]
        credly_date = (credly_record.get("issued_at") or "").strip()
        cinode_date = (cinode_record.get("completed") or "").strip()
        credly_expires = (credly_record.get("expires_at") or "").strip()
        cinode_expires = (cinode_record.get("expires") or "").strip()
        cinode_description = (cinode_record.get("description") or "").strip()

        expected_description: Optional[str] = None
        has_description_note = False
        if credly_record.get("expires_is_derived") and credly_expires:
            issued_txt = credly_record.get("issued_at") or "unknown issue date"
            expected_description = (
                f"Expiry auto-derived: set to {credly_expires} (two years after issue date {issued_txt})."
            )
            if expected_description in cinode_description:
                has_description_note = True
            elif "Expiry auto-derived" in cinode_description:
                has_description_note = expected_description in cinode_description

        if credly_date and credly_date != cinode_date:
            mismatched_dates.append(
                {
                    "title": credly_record.get("title", ""),
                    "category": credly_record.get("category") or categorize_learning_item(credly_record.get("title", "")),
                    "credly_issued": credly_date,
                    "cinode_completed": cinode_date,
                    "cinode_expires": cinode_record.get("expires", ""),
                    "credly_record": credly_record,
                    "cinode_record": cinode_record,
                }
            )

        cred_title = (credly_record.get("title") or "").strip()
        cinode_title = (cinode_record.get("title") or "").strip()
        if cred_title and cinode_title and cred_title != cinode_title:
            title_mismatches.append(
                {
                    "category": credly_record.get("category") or categorize_learning_item(cred_title),
                    "credly_title": cred_title,
                    "cinode_title": cinode_title,
                    "cinode_record": cinode_record,
                    "credly_record": credly_record,
                }
            )

        needs_expiry_update = bool(credly_expires and credly_expires != cinode_expires)
        needs_description_update = bool(expected_description and not has_description_note)
        if credly_expires and (needs_expiry_update or needs_description_update):
            expiry_mismatches.append(
                {
                    "category": credly_record.get("category") or categorize_learning_item(cred_title),
                    "credly_title": cred_title,
                    "credly_expires": credly_expires,
                    "cinode_expires": cinode_expires,
                    "derived": bool(credly_record.get("expires_is_derived")),
                    "needs_expiry_update": needs_expiry_update,
                    "needs_description_update": needs_description_update,
                    "expected_description": expected_description,
                    "cinode_description": cinode_description,
                    "credly_record": credly_record,
                    "cinode_record": cinode_record,
                }
            )

    return {
        "missing_in_cinode": credly_only,
        "missing_in_credly": cinode_only,
        "mismatched_dates": mismatched_dates,
        "title_mismatches": title_mismatches,
        "expiry_mismatches": expiry_mismatches,
    }


def print_comparison_summary(comparison: Dict[str, Any], console: ConsoleType) -> None:
    missing_in_cinode = comparison.get("missing_in_cinode") or []
    missing_in_credly = comparison.get("missing_in_credly") or []
    mismatched_dates = comparison.get("mismatched_dates") or []
    title_mismatches = comparison.get("title_mismatches") or []
    expiry_mismatches = comparison.get("expiry_mismatches") or []

    def render_simple_list(title: str, rows: List[str]) -> None:
        if not rows:
            return
        emit_blank(console)
        emit(title, console, style="bold yellow")
        for row in rows:
            emit(f"  - {row}", console)

    if missing_in_cinode:
        if console and Table is not None:
            emit_blank(console)
            table = Table(title="Credly badges not present in Cinode trainings", box=box.SIMPLE)
            table.add_column("Category", style="cyan", no_wrap=True)
            table.add_column("Title", style="red")
            for entry in missing_in_cinode:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                table.add_row(category, title)
            console.print(table)
        else:
            rows = []
            for entry in missing_in_cinode:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                rows.append(f"[{category}] {title}")
            render_simple_list("Credly badges not present in Cinode trainings:", rows)

    if missing_in_credly:
        if console and Table is not None:
            emit_blank(console)
            table = Table(title="Cinode trainings not present in Credly badges", box=box.SIMPLE)
            table.add_column("Category", style="cyan", no_wrap=True)
            table.add_column("Title", style="magenta")
            for entry in missing_in_credly:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                table.add_row(category, title)
            console.print(table)
        else:
            rows = []
            for entry in missing_in_credly:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                rows.append(f"[{category}] {title}")
            render_simple_list("Cinode trainings not present in Credly badges:", rows)

    if mismatched_dates:
        if console and Table is not None:
            emit_blank(console)
            table = Table(title="Items with differing issue/completion dates", box=box.SIMPLE)
            table.add_column("Category", style="cyan", no_wrap=True)
            table.add_column("Title", style="yellow")
            table.add_column("Credly issued", style="white", no_wrap=True)
            table.add_column("Cinode completed", style="white", no_wrap=True)
            table.add_column("Expires", style="white", no_wrap=True)
            for entry in mismatched_dates:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                table.add_row(
                    category,
                    title,
                    entry.get("credly_issued") or "N/A",
                    entry.get("cinode_completed") or "N/A",
                    entry.get("cinode_expires") or "",
                )
            console.print(table)
        else:
            rows = []
            for entry in mismatched_dates:
                title = entry.get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                credly_date = entry.get("credly_issued") or "N/A"
                cinode_date = entry.get("cinode_completed") or "N/A"
                expires = entry.get("cinode_expires")
                extra = f", expires {expires}" if expires else ""
                rows.append(f"[{category}] {title}: Credly {credly_date} vs Cinode {cinode_date}{extra}")
            render_simple_list("Items with differing issue/completion dates:", rows)

    if title_mismatches:
        if console and Table is not None:
            emit_blank(console)
            table = Table(title="Potential title renames detected", box=box.SIMPLE)
            table.add_column("Category", style="cyan", no_wrap=True)
            table.add_column("Cinode title", style="yellow")
            table.add_column("Credly title", style="green")
            for entry in title_mismatches:
                category = entry.get("category") or categorize_learning_item(entry.get("credly_title", ""))
                table.add_row(
                    category,
                    entry.get("cinode_title") or "<unknown>",
                    entry.get("credly_title") or "<unknown>",
                )
            console.print(table)
        else:
            rows = []
            for entry in title_mismatches:
                category = entry.get("category") or categorize_learning_item(entry.get("credly_title", ""))
                cinode_title = entry.get("cinode_title") or "<unknown>"
                credly_title = entry.get("credly_title") or "<unknown>"
                rows.append(f"[{category}] Cinode '{cinode_title}' → Credly '{credly_title}'")
            render_simple_list("Potential title renames detected:", rows)

    if expiry_mismatches:
        if console and Table is not None:
            emit_blank(console)
            table = Table(title="Items with differing expiry dates", box=box.SIMPLE)
            table.add_column("Category", style="cyan", no_wrap=True)
            table.add_column("Title", style="yellow")
            table.add_column("Credly expires", style="white", no_wrap=True)
            table.add_column("Cinode expires", style="white", no_wrap=True)
            table.add_column("Derived", style="white", no_wrap=True)
            table.add_column("Desc note", style="white", no_wrap=True)
            for entry in expiry_mismatches:
                derived_flag = "Yes" if entry.get("derived") else "No"
                title = entry.get("credly_title") or entry.get("cinode_record", {}).get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                credly_value = entry.get("credly_expires") or ""
                if entry.get("derived") and credly_value and Text is not None:
                    credly_cell = Text(credly_value, style="orange3")
                else:
                    credly_cell = credly_value
                note_status = "Missing" if entry.get("needs_description_update") else "OK"
                table.add_row(
                    category,
                    title,
                    credly_cell,
                    entry.get("cinode_expires") or "",
                    derived_flag,
                    note_status,
                )
            console.print(table)
        else:
            rows = []
            for entry in expiry_mismatches:
                title = entry.get("credly_title") or entry.get("cinode_record", {}).get("title") or "<untitled>"
                category = entry.get("category") or categorize_learning_item(title)
                derived_note = " (derived)" if entry.get("derived") else ""
                desc_note = " - description note missing" if entry.get("needs_description_update") else ""
                rows.append(
                    f"[{category}] {title}: Credly {entry.get('credly_expires') or 'N/A'} vs Cinode {entry.get('cinode_expires') or 'N/A'}{derived_note}{desc_note}"
                )
            render_simple_list("Items with differing expiry dates:", rows)

    if not (missing_in_cinode or missing_in_credly or mismatched_dates or title_mismatches or expiry_mismatches):
        emit_blank(console)
        emit("No differences between Credly badges and Cinode trainings were detected.", console, style="green")


def resolve_user_by_query(user_index: Dict[int, Dict], query: str) -> Optional[int]:
    trimmed = query.strip()
    if not trimmed:
        return None

    lowered = trimmed.lower()

    if trimmed.isdigit():
        user_id = int(trimmed)
        if user_id in user_index:
            return user_id

    exact_matches: list[int] = []
    partial_matches: list[int] = []

    for user_id, entry in user_index.items():
        first = (entry.get("firstName") or "").strip()
        last = (entry.get("lastName") or "").strip()

        candidates = {
            first.lower(),
            last.lower(),
            f"{first} {last}".strip().lower(),
            f"{last} {first}".strip().lower(),
        }

        if lowered in candidates:
            exact_matches.append(user_id)
            continue

        if any(lowered in value for value in candidates if value):
            partial_matches.append(user_id)

    if len(exact_matches) == 1:
        return exact_matches[0]

    unique_partial = [user_id for user_id in partial_matches if user_id not in exact_matches]
    if not exact_matches and len(unique_partial) == 1:
        return unique_partial[0]

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select a Cinode user and list their social / public profile links",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the extracted social links as JSON",
    )
    parser.add_argument(
        "--user",
        "-u",
        dest="user_query",
        help="Pre-filter users by name or ID and auto-select if a single match is found",
    )
    parser.add_argument(
        "--add-missing",
        action="store_true",
        help="Create Credly badges that are missing from Cinode after comparison",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose Credly badge details and diagnostics",
    )
    parser.add_argument(
        "--sync-titles",
        action="store_true",
        help="Update Cinode training titles/names to match Credly data",
    )
    args = parser.parse_args()

    global SUPPRESS_OUTPUT
    SUPPRESS_OUTPUT = args.json

    console: ConsoleType = Console() if (Console is not None and not args.json) else None

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

    selected_user_id: Optional[int] = None
    if args.user_query:
        selected_user_id = resolve_user_by_query(user_index, args.user_query)
        if selected_user_id is None:
            emit(
                f"No unique match for '--user {args.user_query}'. Launching interactive selection...",
                console,
                style="yellow",
            )

    if selected_user_id is None:
        selected_user_id = prompt_for_user(
            user_index,
            quick_query=args.user_query,
            auto_confirm_single=bool(args.user_query),
        )
    if selected_user_id is None:
        emit("No user selected.", console, style="red")
        return

    user_entry = user_index[selected_user_id]
    full_name = " ".join(
        part for part in [user_entry.get("firstName"), user_entry.get("lastName")] if part
    )
    display_name = full_name or user_entry.get("name") or "<unknown>"
    if not args.json:
        emit(f"Processing Cinode user {display_name} (ID: {selected_user_id})", console, style="bold cyan")

    user_payload = fetch_user_details(access_token, company_id, selected_user_id)
    social_links = extract_social_links(user_payload)

    credly_links = [value for value in social_links.values() if TARGET_SUBSTRING.lower() in value.lower()]
    badge_records: List[Dict[str, Any]] = []
    for link in credly_links:
        badge_records.extend(fetch_badges_from_credly(link, console=console))

    profile_payload = fetch_user_profile(access_token, company_id, selected_user_id)
    cinode_trainings_raw = extract_trainings(profile_payload)
    cinode_records = build_cinode_training_records(cinode_trainings_raw)

    comparison = compare_credly_and_cinode(badge_records, cinode_records)

    if args.json:
        output = {
            "user": user_index[selected_user_id],
            "credly_links": credly_links,
            "badges": badge_records,
            "cinode_trainings": cinode_records,
            "comparison": comparison,
        }
        print(json.dumps(output, indent=2, default=json_default))
        return

    if args.debug:
        print_social_links(user_entry, social_links, console)
    badge_count = len(badge_records)
    if credly_links and not badge_records:
        emit("No badges retrieved from Credly.", console, style="yellow")
    elif not credly_links:
        emit("No Credly link was found for this user.", console, style="yellow")
    elif args.debug:
        print_badge_summary(badge_records, console)
    else:
        emit(f"Credly badges fetched: {badge_count} (use --debug to show details)", console)

    emit_blank(console)
    emit(f"Cinode trainings fetched: {len(cinode_records)}", console, style="bold")
    print_comparison_summary(comparison, console)

    created = False
    if getattr(args, "add_missing", False):
        missing_records = comparison.get("missing_in_cinode") or []
        created = create_missing_trainings(access_token, company_id, selected_user_id, missing_records, console)

    renames_applied = False
    if (args.sync_titles or args.add_missing) and comparison.get("title_mismatches"):
        renames_applied = apply_title_renames(
            access_token,
            company_id,
            selected_user_id,
            comparison.get("title_mismatches") or [],
            console,
        )

    expiry_updates_applied = False
    if args.add_missing and comparison.get("expiry_mismatches"):
        expiry_updates_applied = apply_expiry_updates(
            access_token,
            company_id,
            selected_user_id,
            comparison.get("expiry_mismatches") or [],
            console,
        )

    if created or renames_applied or expiry_updates_applied:
        emit_blank(console)
        emit("Re-fetching Cinode trainings for verification...", console, style="bold cyan")
        profile_payload = fetch_user_profile(access_token, company_id, selected_user_id)
        updated_trainings_raw = extract_trainings(profile_payload)
        updated_cinode_records = build_cinode_training_records(updated_trainings_raw)
        emit(f"Updated Cinode trainings fetched: {len(updated_cinode_records)}", console, style="bold")
        updated_comparison = compare_credly_and_cinode(badge_records, updated_cinode_records)
        remaining_missing = updated_comparison.get("missing_in_cinode") or []
        cinode_only = updated_comparison.get("missing_in_credly") or []
        has_other_discrepancies = bool(
            updated_comparison.get("mismatched_dates")
            or updated_comparison.get("title_mismatches")
            or updated_comparison.get("expiry_mismatches")
        )
        print_comparison_summary(updated_comparison, console)
        if not remaining_missing and not has_other_discrepancies:
            emit(
                "Verification: Cinode now mirrors Credly for badge presence, titles, and expiry notes.",
                console,
                style="green",
            )
        elif remaining_missing:
            emit(
                "Verification warning: Some Credly badges are still missing in Cinode after creation.",
                console,
                style="yellow",
            )
        elif has_other_discrepancies:
            emit(
                "Verification complete, but additional discrepancies remain for review.",
                console,
                style="yellow",
            )
        elif cinode_only:
            emit(
                "Verification note: Titles now align with Credly; Cinode still lists additional trainings that do not have Credly badges (expected if you track extra courses).",
                console,
            )



if __name__ == "__main__":
    main()
