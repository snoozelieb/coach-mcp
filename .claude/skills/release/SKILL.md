---
name: release
description: Cut and publish a garmin-coach-mcp release — version triple-bump, tag push (CI publishes PyPI + GitHub release), MCP registry publish, and the two-account git push auth dance. Use when the user asks to release, publish, cut a version, or bump the package.
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
excerpt. Note: the local `gh` default is the work account, which **cannot**
create releases on this repo — that's why CI does it.

## 3. Verify

- Actions → Release run is green.
- https://pypi.org/project/garmin-coach-mcp/ shows the new version.
- GitHub Release exists with wheel + sdist.

## 4. MCP Registry (manual, user-run)

Separate from CI. From the repo root (where `server.json` lives):

```bash
mcp-publisher login github     # device-code flow — ask the USER to run this (interactive)
mcp-publisher publish
```

Suggest the user runs interactive steps with the `!` prefix so output lands in
the session. Verify:

```bash
curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.snoozelieb/coach-mcp"
```

A lagging registry version is cosmetic — installs come from PyPI. If a
`mcp-publisher.exe` is sitting untracked in the repo root, it was downloaded
for this step; delete it once the publish succeeds.
