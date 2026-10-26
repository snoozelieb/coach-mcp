"""Manual Garmin token recovery — thin shim over coach.garmin_login.

Installed copies have the console script ``garmin-coach-login``; a source
checkout can run this file. Handles MFA interactively and saves tokens
where the server reads them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.garmin_login import main  # noqa: E402

if __name__ == "__main__":
    main()
