"""
Playwright-based Garmin SSO login fallback.

Used when Garmin's Cloudflare protection blocks the standard garth SSO flow
with 429 errors. Opens a real browser to complete SSO, extracts the ticket,
then uses garth's existing OAuth exchange to get proper tokens.

Requires: pip install playwright && playwright install chromium
"""

import logging
import os
import re
from urllib.parse import parse_qs, urlparse

from garth.sso import exchange, get_oauth1_token
from garth.auth_tokens import OAuth1Token, OAuth2Token

logger = logging.getLogger(__name__)

SSO_SIGNIN_URL = (
    "https://sso.garmin.com/sso/signin"
    "?id=gauth-widget"
    "&embedWidget=true"
    "&gauthHost=https://sso.garmin.com/sso/embed"
    "&service=https://sso.garmin.com/sso/embed"
    "&source=https://sso.garmin.com/sso/embed"
    "&redirectAfterAccountLoginUrl=https://sso.garmin.com/sso/embed"
    "&redirectAfterAccountCreationUrl=https://sso.garmin.com/sso/embed"
)

LOGIN_TIMEOUT_MS = 120_000  # 120s for user to handle MFA/Cloudflare


def playwright_sso_login(garth_client) -> tuple[OAuth1Token, OAuth2Token]:
    """Perform Garmin SSO login via a real browser and return OAuth tokens.

    Opens a visible Chromium window, navigates to the Garmin SSO page,
    fills credentials, waits for login to complete (including MFA if needed),
    extracts the SSO ticket, and exchanges it for OAuth tokens via garth.

    Args:
        garth_client: A garth.http.Client instance (from Garmin().garth)

    Returns:
        Tuple of (OAuth1Token, OAuth2Token) ready to use with garth

    Raises:
        ImportError: If playwright is not installed
        RuntimeError: If ticket extraction fails
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright is required for browser-based Garmin login.\n"
            "Install with: pip install playwright && playwright install chromium"
        )

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")

    ticket = None

    def capture_ticket(response):
        nonlocal ticket
        if "ticket=" in response.url:
            parsed = urlparse(response.url)
            qs = parse_qs(parsed.query)
            if "ticket" in qs:
                ticket = qs["ticket"][0]
                logger.info("Captured SSO ticket from redirect")

    logger.info("Opening browser for Garmin SSO login...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.on("response", capture_ticket)

        page.goto(SSO_SIGNIN_URL, wait_until="networkidle")

        # Fill login form
        page.fill('input[name="username"]', email)
        page.fill('input[name="password"]', password)
        page.click('#login-btn-signin')

        logger.info("Credentials submitted. Waiting for login to complete...")

        # Wait for either success page or MFA prompt
        try:
            page.wait_for_function(
                """() => {
                    const title = document.title;
                    const mfa = document.querySelector('input[name="mfa-code"]');
                    return title === 'Success' || mfa !== null;
                }""",
                timeout=30_000,
            )
        except Exception:
            logger.warning("Timed out waiting for initial response. "
                          "Check the browser window for Cloudflare challenges.")

        # Handle MFA if needed
        if page.query_selector('input[name="mfa-code"]'):
            logger.info(
                "MFA required — please enter the code in the browser window. "
                "Waiting up to 120 seconds..."
            )
            page.wait_for_function(
                "() => document.title === 'Success'",
                timeout=LOGIN_TIMEOUT_MS,
            )

        # If response interception didn't capture the ticket, try page HTML
        if not ticket:
            html = page.content()
            m = re.search(r'embed\?ticket=([^"]+)"', html)
            if m:
                ticket = m.group(1)
                logger.info("Captured SSO ticket from page HTML")

        browser.close()

    if not ticket:
        raise RuntimeError(
            "Could not extract SSO ticket from browser login. "
            "Check that login completed successfully."
        )

    logger.info("Exchanging SSO ticket for OAuth tokens...")
    oauth1 = get_oauth1_token(ticket, garth_client)
    oauth2 = exchange(oauth1, garth_client)
    logger.info("Browser login complete — tokens obtained successfully")

    return oauth1, oauth2
