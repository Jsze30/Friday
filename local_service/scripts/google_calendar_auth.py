"""One-shot OAuth bootstrap for Google Calendar.

Run once after setting GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET in
local_service/.env:

    cd local_service && uv run python scripts/google_calendar_auth.py

Opens a browser, asks for consent, and writes a refresh token to
~/Library/Application Support/Friday/google_calendar.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from src.google_auth import SCOPES, TOKEN_PATH, client_config, save_credentials  # noqa: E402


def main() -> None:
    flow = InstalledAppFlow.from_client_config(client_config(), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    save_credentials(creds)
    print(f"Token saved to {TOKEN_PATH}")
    svc_email = creds.id_token.get("email") if creds.id_token else None
    if svc_email:
        print(f"Authorized as {svc_email}")


if __name__ == "__main__":
    main()
