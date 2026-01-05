import os
import logging
from garminconnect import Garmin
from dotenv import load_dotenv

from config import TOKEN_DIR

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache the client instance to avoid repeated logins
_cached_client: Garmin = None


def get_garmin_client(force_refresh: bool = False) -> Garmin:
    """
    Authenticates with Garmin using the new 'Garth' backend.
    Returns an authenticated Garmin client.

    Uses a cached instance to avoid repeated logins within the same session.

    Args:
        force_refresh: If True, forces a fresh login even if cached.
    """
    global _cached_client

    # Return cached client if available and not forcing refresh
    if _cached_client is not None and not force_refresh:
        return _cached_client

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    client = Garmin(email, password)

    # 1. Try to load saved tokens from the directory
    try:
        if os.path.exists(TOKEN_DIR):
            logger.info("Loading saved session...")
            client.garth.load(TOKEN_DIR)

            # Verify the session is valid by trying a lightweight call
            client.get_user_summary(date.today().isoformat())
            logger.info("Session loaded successfully!")
            _cached_client = client
            return client
    except Exception as e:
        logger.warning(f"Session invalid or expired: {e}. Logging in fresh...")

    # 2. If load failed (or no tokens), perform a full login
    try:
        client.login()

        # 3. Save the new tokens for next time
        if not os.path.exists(TOKEN_DIR):
            os.makedirs(TOKEN_DIR)

        client.garth.dump(TOKEN_DIR)
        logger.info("New session saved.")

    except Exception as e:
        logger.error(f"Login failed! Check credentials. Error: {e}")
        raise

    _cached_client = client
    return client