# Cinode Utility Scripts

This directory contains helper scripts that integrate with the Cinode API to inspect company data, reconcile user trainings with Credly badges, and keep profile records up to date. The tooling is aimed at interactive housekeeping for a single company tenant.

## Prerequisites

- Python 3.8 or newer (the repo currently uses Python 3.14 via a local virtual environment).
- Required Python packages:
  - `requests`
  - `rich` (optional but recommended for colorized console output)
- `credentials.txt` in this directory with the expected Cinode API keys and company identifiers.

## Key Scripts

### `update_cinode_from_credly.py`
Fetch a user profile, locate their public Credly link, retrieve badges, and compare them against Cinode trainings. Supports:
- Rich-rendered tables (when `rich` is installed).
- JSON export of the collected data (`--json`).
- Creating missing trainings from Credly (`--add-missing`).
- Renaming Cinode trainings to match Credly titles (`--sync-titles`).
- Derived expiry handling for Fortinet courses, including optional description-note updates.

### `sync_trainings_from_badges.py`
Batch-oriented sync that reads exported badge CSVs, determines which Cinode trainings to create or update, and pushes the changes. Useful when you have structured badge exports rather than live Credly access.

### `compare_trainings_and_badges.py`
Common comparison utilities shared by multiple scripts. Provides title normalization, CSV import helpers, and structured discrepancy reports between badge datasets and Cinode trainings.

### `get_cinode_user_trainings.py`
Interactive browser for a user’s trainings. Can dump details, group by type or issuer, and export to CSV for auditing.

### Supporting Scripts
- `get_cinode_token.py`, `get_cinode_teams.py`, `get_cinode_company.py`, `get_cinode_team_members.py`, `get_cinode_trainings.py`, `get_cinode_user_profile.py`, `get_cinode_user_skills.py`: helper modules and small utilities that authenticate, fetch shared data, and build reusable indexes for the higher-level workflows.

## Usage Notes

1. Ensure `credentials.txt` contains `CINODE_COMPANY_ID` and the required API tokens.
2. (Optional) Activate the project virtual environment: `source ../.venv/bin/activate`.
3. Install dependencies: `pip install requests rich`.
4. Run the desired script, e.g. `python update_cinode_from_credly.py --user "lastname" --add-missing`.

Most scripts are interactive—they prompt before creating or updating data. Review the console output carefully before confirming any write operations.
