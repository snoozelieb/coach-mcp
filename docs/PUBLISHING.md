# Publishing coach-mcp

Pushing a `v*` tag runs `.github/workflows/release.yml`, which:

1. builds sdist + wheel (after verifying the tag matches `pyproject.toml`),
2. publishes to PyPI via **trusted publishing** (OIDC — no API token anywhere),
3. creates a GitHub Release with the artifacts and the matching
   `CHANGELOG.md` excerpt,
4. registers `server.json` with the official MCP Registry — OIDC again: the
   job authenticates as the repository owner, so there is no token to leak
   (see [MCP Registry (automatic)](#mcp-registry-automatic)).

## One-time setup (completed before v1.0.0 — kept for reference)

> Both steps are done: the trusted publisher is registered on PyPI and the
> mcp-name marker is in README.md. Nothing here recurs per release.

### a. Configure the PyPI trusted publisher

The project does not exist on PyPI yet, so register a **pending publisher**:

1. Log in at <https://pypi.org> → account menu → **Publishing**
   (<https://pypi.org/manage/account/publishing/>).
2. Under "Add a new pending publisher" (GitHub tab), enter exactly:
   - **PyPI project name**: `garmin-coach-mcp`
   - **Owner**: `snoozelieb`
   - **Repository name**: `coach-mcp`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`
3. In the GitHub repo: **Settings → Environments → New environment**, named
   `pypi` (no secrets or variables needed — it exists so PyPI can pin the
   OIDC claim and you can later add required reviewers as a release gate).

### b. Add the MCP Registry ownership marker to the README

The registry verifies you control the PyPI package by looking for this exact
line (plain text or HTML comment) in the README **as published on PyPI**:

```
mcp-name: io.github.snoozelieb/coach-mcp
```

Add it to `README.md` **before tagging**, or the registry job will fail and a
follow-up PyPI release will be needed.

## Release

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Watch the run under **Actions → Release**. When it finishes, verify
<https://pypi.org/project/garmin-coach-mcp/> and the GitHub Release exist.

## MCP Registry (automatic)

The `publish-to-mcp-registry` job in `release.yml` runs after the PyPI upload
succeeds and does what the manual CLI used to do:

1. checks that both `server.json` version fields equal the tag;
2. downloads `mcp-publisher` at the pinned `MCP_PUBLISHER_VERSION`, verifies
   the archive's SHA-256 against `MCP_PUBLISHER_SHA256` and its Sigstore
   bundle against the registry's own release-workflow identity
   (`cosign verify-blob`), then extracts it into the runner's temp dir;
3. `mcp-publisher login github-oidc` — exchanges the job's GitHub OIDC token
   at the registry, which grants publish rights for `io.github.snoozelieb/*`
   (the repository owner's namespace). No stored credential exists;
4. `mcp-publisher publish`, retrying for up to five minutes while PyPI's
   metadata for the new version is not visible yet. Any other rejection fails
   immediately.

The job is independent of the GitHub Release job, so a registry failure never
blocks the release. Re-running just the failed job is safe: the registry
refuses duplicate versions. Do not re-push the tag — that re-runs the PyPI
job, which rejects duplicates.

### Bumping the pinned publisher

1. Pick a release tag at <https://github.com/modelcontextprotocol/registry/releases>.
2. Copy the `mcp-publisher_linux_amd64.tar.gz` digest from that release's
   `registry_<version>_checksums.txt`.
3. Update `MCP_PUBLISHER_VERSION` and `MCP_PUBLISHER_SHA256` in `release.yml`
   together. Never switch the download to `releases/latest`.

### Manual fallback

If CI cannot publish (registry outage, OIDC exchange disabled), publish from a
machine. Install the CLI (Windows PowerShell; see the
[registry quickstart](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx)
for Linux/macOS):

```powershell
Invoke-WebRequest -Uri "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_windows_amd64.tar.gz" -OutFile "mcp-publisher.tar.gz"
tar xf mcp-publisher.tar.gz mcp-publisher.exe
```

Then, from the repo root (where `server.json` lives):

```bash
mcp-publisher login github     # device-code flow; must authorize the snoozelieb GitHub account
mcp-publisher publish          # validates + publishes server.json
```

Verify either path with:

```bash
curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.snoozelieb/coach-mcp"
```

## Subsequent releases

1. Bump `version` in `pyproject.toml` **and** both `version` fields in
   `server.json` (top level + `packages[0]`); add a `## [x.y.z]` section to
   `CHANGELOG.md` (tests/test_packaging.py enforces all three agree).
2. Tag `vx.y.z`, push the tag.
3. Watch the Release run: PyPI, the GitHub Release and the MCP Registry all
   publish from it.
