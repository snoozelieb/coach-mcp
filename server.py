"""Thin shim for repo checkouts — the server lives in coach/server.py.

`python server.py` keeps working; installed deployments use the
`coach-mcp` console script (see [project.scripts] in pyproject.toml).
"""
from coach.server import main, mcp  # noqa: F401 — mcp re-exported for tests/tooling

if __name__ == "__main__":
    main()
