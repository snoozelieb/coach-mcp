---
name: release
description: Cut and publish a garmin-coach-mcp release — version triple-bump, tag push (CI publishes PyPI + GitHub release + MCP registry), and the two-account git push auth dance. Use when the user asks to release, publish, cut a version, or bump the package.
---

# Releasing garmin-coach-mcp

Full runbook: `docs/PUBLISHING.md`. This skill is the operational checklist with
the machine-specific facts that are NOT in the runbook.

## Preflight

1. `git branch --show-current` must be `main`, tree clean, up to date with origin.
2. Full suite green locally: `python -m pytest -q`.
3. `CHANGELOG.md` `[Unreleased]` section describes what ships.

## 1. Version triple-bump (test-enforced)

Bump ALL THREE, or `tests/test_packaging.py` fails:

- `pyproject.toml` → `version`
- `server.json` → **both** `version` fields (top level AND `packages[0]`)
- `CHANGELOG.md` → retitle `[Unreleased]` to `## [x.y.z] - YYYY-MM-DD`

Run `python -m pytest tests/test_packaging.py -q` to confirm, then commit.

## 2. Push + tag — the auth dance (IMPORTANT)

The machine has two GitHub accounts (`snoozelieb` owns this repo; the default
`gh` account is a work account that cannot push here or create releases). The
repo has a local `credential.helper = !gh auth git-credential`. The user does
NOT want browser auth flows (password manager). Run these via the **Bash tool,
NOT PowerShell, and never compound the commands**:

```bash
gh auth switch --user snoozelieb
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
gh auth switch    # ALWAYS switch back afterwards (no --user needed: toggles to the other account)
```

The tag push triggers `.github/workflows/release.yml`: builds sdist+wheel
(verifies tag matches `pyproject.toml`), publishes to PyPI via trusted
publishing (OIDC, no tokens), creates the GitHub Release with the CHANGELOG
excerpt, and registers `server.json` with the MCP Registry (job
`publish-to-mcp-registry`, OIDC — no token). Note: the local `gh` default is
the work account, which **cannot** create releases on this repo — that's why
CI does it.

## 3. Verify

- Actions → Release run is green.
- https://pypi.org/project/garmin-coach-mcp/ shows the new version.
- GitHub Release exists with wheel + sdist.
- The registry lists the version (the job retries up to 5 min while PyPI lags):
  `curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.snoozelieb/coach-mcp"`

## 4. MCP Registry (CI does it — manual fallback only)

The Release workflow's `publish-to-mcp-registry` job registers `server.json`
after the PyPI job succeeds (pinned + Sigstore-verified `mcp-publisher`,
`login github-oidc`, retry while PyPI metadata lags). If that job fails: read
its log, fix the cause, and **re-run the failed job** from the Actions UI
(safe — the registry refuses duplicate versions). Do not re-push the tag: that
re-runs the PyPI job, which rejects duplicates.

Manual fallback (registry outage, OIDC exchange disabled). From the repo root
(where `server.json` lives). Both commands are USER-run: the login is an
interactive device-code flow that must authorize the `snoozelieb` GitHub
account, and the permission classifier blocks `publish` for Claude. Suggest
the `!` prefix or a PowerShell window:

```bash
./mcp-publisher.exe login github
./mcp-publisher.exe publish
```

`mcp-publisher.exe` in the repo root is gitignored and kept for this fallback.
Verify:

```bash
curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.snoozelieb/coach-mcp"
```

A lagging registry version is cosmetic — installs come from PyPI.
