"""Interactive first-run setup — thin shim over coach.setup_wizard.

Installed copies have the console script ``garmin-coach-setup``; a source
checkout can run this file. The shim binds the resolved data dir itself so
it stays patchable (tests/test_packaging.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.config import DATA_DIR  # noqa: E402
from coach import setup_wizard as _impl  # noqa: E402


def run_setup() -> None:
    _impl.run_setup(data_dir=DATA_DIR)


def main() -> None:
    run_setup()


if __name__ == "__main__":
    main()
