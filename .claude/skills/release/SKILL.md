---
name: release
description: Release garmin-coach-mcp — version triple-bump, changelog, the two-account GitHub push/tag dance, and what the tag-triggered CI does (PyPI trusted publishing, GitHub Release, MCP Registry via OIDC). Use when cutting a version or verifying a release landed.
---

# Releasing garmin-coach-mcp

Full runbook: `docs/PUBLISHING.md`. This skill is the operational checklist with
the machine-specific facts that are NOT in the runbook.

## Preflight

1. `git branch --show-current` must be `main`, tree clean, up to date with origin.
2. Full suite green locally with the venv interpreter:
   `.venv/Scripts/python.exe -m pytest -q`, and `python -m ruff check .` clean
   (CI runs the lint job and the suite on Python 3.12/3.13/3.14).
3. `CHANGELOG.md` `[Unreleased]` section describes what ships.
4. Nothing personal in the tree: `git status --short` shows no `data/*`
   (except `methodology.json`), `.garth/`, `.env`, `*.log`, `test_fixtures.json`
   or `data-backups/`. The review docs under `docs/*_REVIEW_*.md` that name
   unfixed findings stay untracked.

## 1. Version triple-bump (test-enforced)

Bump ALL THREE, or `tests/test_packaging.py` fails:

- `pyproject.toml` → `version`
- `server.json` → **both** `version` fields (top level AND `packages[0]`)
- `CHANGELOG.md` → retitle `[Unreleased]` to `## [x.y.z] - YYYY-MM-DD`

Run `.venv/Scripts/python.exe -m pytest tests/test_packaging.py -q` to confirm,
then commit (no AI attribution lines in the message — user rule).

## 2. Push + tag — the auth dance (IMPORTANT)

The machine has two GitHub accounts (`snoozelieb` owns this repo; the default
`gh` account is a work account that cannot push here or create releases). The
repo has a local `credential.helper = !gh auth git-credential`. The user does
NOT want browser auth flows (password manager). Run these via the **Bash tool,
NOT PowerShell, as bare single commands — compounding them with `&&` or `;`
gets the call denied**:

```bash
gh auth switch --user snoozelieb
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
gh auth switch    # ALWAYS switch back afterwards (no --user needed: toggles to the other account)
```

Pushing a tag is outward-facing: PyPI cannot take a version back and the
registry refuses duplicates. Tag only when the user has said so.

## 3. What the tag triggers (`.github/workflows/release.yml`)

1. **build** — verifies the tag matches `pyproject.toml`, builds sdist + wheel.
2. **publish-to-pypi** — trusted publishing (OIDC, environment `pypi`, no
   token; PEP 740 attestations). The trusted publisher on pypi.org is
   constrained to that environment.
3. **github-release** — creates the GitHub Release with the `CHANGELOG.md`
   excerpt for the version and the artifacts. The local `gh` default is the
   work account, which **cannot** create releases here — that is why CI does it.
4. **publish-to-mcp-registry** — after PyPI: downloads `mcp-publisher` at the
   pinned `MCP_PUBLISHER_VERSION`, checks `MCP_PUBLISHER_SHA256` and the
   Sigstore bundle, `login github-oidc`, then `publish` with retries while
   PyPI metadata lags. Pinned v1.8.5 is the latest publisher on 2026-10-27
   (SHA verified); the four `actions/*@v4`/`@v5` steps are Node 20 actions
   and emit deprecation notices on Node 24 runners — bump to checkout@v7,
   setup-python@v7, upload-artifact@v7, download-artifact@v8 when convenient. No secret anywhere. Independent of the release job: a
   registry failure never blocks the release, and re-running just that job is
   safe. Bumping the publisher = update both pins together
   (`docs/PUBLISHING.md`). The registry checks the `mcp-name:` marker in the
   README as published on PyPI — it is already there; do not remove it.

## 4. Verify

- Actions → Release run: all four jobs green (the registry job is the one to
  watch on a first run after a workflow change).
- https://pypi.org/project/garmin-coach-mcp/ shows the new version.
- GitHub Release exists with wheel + sdist.
- Registry: `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.snoozelieb/coach-mcp&version=latest"`
  lists the version (`/v0.1/` is the stable API; without `version=latest`
  superseded versions come back first). A lagging registry version is cosmetic — installs come
  from PyPI.
- MCP clients must reconnect to see a changed tool surface, and the server
  must be restarted after ANY code change under `coach/`: it is a
  long-running process of this checkout (Claude Desktop: quit and reopen;
  Claude Code: `/mcp` reconnect). A process started before an update keeps
  the old modules in memory; the startup-import test only guarantees it
  cannot half-load the new ones (2026-10-27: a stale process crashed the
  push with an ImportError).

## 5. Manual registry fallback (only if the CI job fails)

From the repo root (where `server.json` lives), the user runs the interactive
steps with the `!` prefix so output lands in the session:

```bash
mcp-publisher login github     # device-code flow — user-run (interactive)
mcp-publisher publish
```

`./mcp-publisher.exe publish` is classifier-blocked for the assistant, so it is
always user-run. The local `mcp-publisher.exe` is gitignored; keep it in sync
with the pinned CI version when you use it.
