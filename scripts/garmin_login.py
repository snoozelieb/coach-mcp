"""
Manual Garmin token recovery via native garminconnect login.

This is the manual recovery path when MCP tools start returning Garmin
auth errors (expired or invalid tokens). It performs a fresh credential
login using the installed garminconnect library (no garth, no
playwright/browser), handles MFA interactively, and persists new tokens
to the same token store the server reads (coach.config.TOKEN_DIR, i.e.
.garth/garmin_tokens.json — loaded by coach/garmin_client.py via
client.login(tokenstore=TOKEN_DIR)).

Usage: python scripts/garmin_login.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from garminconnect import Garmin
from coach.config import TOKEN_DIR


def main() -> None:
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        print("Error: GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")
        sys.exit(1)

    # Force a fresh credential login: don't pass a tokenstore, and make sure
    # garminconnect doesn't silently load stale tokens from GARMINTOKENS.
    os.environ.pop("GARMINTOKENS", None)

    print("Logging in to Garmin Connect (fresh credential login)...")
    garmin = Garmin(email, password, return_on_mfa=True)

    try:
        status, client_state = garmin.login()
        if status == "needs_mfa":
            mfa_code = input("Enter the MFA code Garmin sent you: ").strip()
            garmin.resume_login(client_state, mfa_code)
    except Exception as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    # Persist tokens where the server loads them from.
    garmin.client.dump(TOKEN_DIR)

    # Verify with a cheap read. After a non-MFA login with return_on_mfa=True
    # the profile fields are not populated, so fall back to an explicit
    # authenticated API call to confirm the tokens actually work.
    try:
        name = garmin.get_full_name()
        if not name:
            profile = garmin.client.connectapi("/userprofile-service/socialProfile")
            name = profile.get("fullName") or profile.get("displayName")
    except Exception as e:
        print(f"Tokens saved to {TOKEN_DIR}, but verification call failed: {e}")
        sys.exit(1)

    print(f"Login verified for: {name}")
    print(f"Tokens saved to {TOKEN_DIR}")
    print("You can now restart the MCP server — it will use these tokens.")


if __name__ == "__main__":
    main()
