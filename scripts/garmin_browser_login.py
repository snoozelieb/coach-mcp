"""
Manual browser login to Garmin Connect.

Use when Cloudflare blocks automated login (persistent 429 errors).
Opens a browser window for you to log in, then saves OAuth tokens
to .garth/ for the MCP server to use.

Usage: python scripts/garmin_browser_login.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from garminconnect import Garmin
from coach.config import TOKEN_DIR
from coach.playwright_auth import playwright_sso_login


def main():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        print("Error: GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")
        sys.exit(1)

    print("Starting browser-based Garmin login...")
    client = Garmin(email, password)

    oauth1, oauth2 = playwright_sso_login(client.client)
    client.client.oauth1_token = oauth1
    client.client.oauth2_token = oauth2

    os.makedirs(TOKEN_DIR, exist_ok=True)
    client.client.dump(TOKEN_DIR)

    print(f"Tokens saved to {TOKEN_DIR}")
    print("You can now start the MCP server — it will use these tokens.")


if __name__ == "__main__":
    main()
