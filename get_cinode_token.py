"""Fetch an OAuth access token from the Cinode API using values in credentials.txt."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import requests

CREDENTIALS_FILE = Path(__file__).with_name("credentials.txt")
TOKEN_CACHE_FILE = Path(__file__).with_name("cinode_token.json")
TOKEN_URL = "https://api.cinode.com/token"
REQUIRED_KEYS = ("CINODE_ACCESS_ID", "CINODE_ACCESS_SECRET")


def load_credentials(path: Path) -> Dict[str, str]:
    """Parse a simple KEY="value" credentials file into a dictionary."""
    if not path.exists():
        raise FileNotFoundError(f"Credentials file not found: {path}")

    credentials: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, sep, value = line.partition("=")
        if not sep:
            continue

        cleaned = value.strip().strip('"').strip("'")
        credentials[key.strip()] = cleaned

    missing = [key for key in REQUIRED_KEYS if not credentials.get(key)]
    if missing:
        raise ValueError(f"Missing required credentials entries: {', '.join(missing)}")

    return credentials


def request_access_token(credentials: Dict[str, str]) -> Dict[str, object]:
    """Request an access token using Cinode's Personal API Account flow."""

    response = requests.get(
        TOKEN_URL,
        auth=(credentials["CINODE_ACCESS_ID"], credentials["CINODE_ACCESS_SECRET"]),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def persist_token(payload: Dict[str, object], path: Path) -> None:
    """Write token details to disk so other scripts can reuse them."""

    stamped_payload = dict(payload)
    stamped_payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(stamped_payload, indent=2), encoding="utf-8")


def main() -> None:
    credentials = load_credentials(CREDENTIALS_FILE)
    token_payload = request_access_token(credentials)
    persist_token(token_payload, TOKEN_CACHE_FILE)

    # Print JSON so it can be piped into tools like jq if desired.
    print(json.dumps(token_payload, indent=2))


if __name__ == "__main__":
    main()
