"""Packaging-path guards (Lane A).

Pins the behavior that makes coach-mcp installable via pip/uvx without
breaking the run-from-checkout workflow:

1. Data/token dir resolution precedence: COACH_DATA_DIR env var > git
   checkout data/ > per-user data dir (no third-party deps).
2. Defaults bootstrap: check_setup() creates a missing data dir and seeds
   the packaged coach/defaults/methodology.json — never overwriting.
3. The packaged default stays byte-for-byte in sync (JSON-equal) with the
   tracked data/methodology.json so the two copies cannot drift.
4. Entry points: coach.server:main exists (console script target), the root
   server.py shim still exposes mcp/main, pyproject declares the script.
5. The setup wizard writes to the RESOLVED data dir, not a repo-relative one.
6. Registry/release plumbing (Lane C): server.json validity + consistency
   with pyproject, release.yml trusted-publishing wiring, and version
   agreement across pyproject / server.json / CHANGELOG.
"""
import importlib
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

import coach.config as config
import coach.parsers as parsers

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Resolution precedence
# ---------------------------------------------------------------------------

class TestDataDirResolution:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv('COACH_DATA_DIR', str(tmp_path / 'custom'))
        assert config.resolve_data_dir() == tmp_path / 'custom'

    def test_env_var_beats_checkout(self, tmp_path, monkeypatch):
        """Even with the checkout data/ present, the env var wins."""
        assert config._CHECKOUT_DATA_DIR.is_dir()  # we ARE in a checkout
        monkeypatch.setenv('COACH_DATA_DIR', str(tmp_path))
        assert config.resolve_data_dir() == tmp_path

    def test_checkout_layout_when_no_env(self, monkeypatch):
        monkeypatch.delenv('COACH_DATA_DIR', raising=False)
        assert config.resolve_data_dir() == REPO_ROOT / 'data'

    def test_user_dir_when_no_checkout(self, tmp_path, monkeypatch):
        monkeypatch.delenv('COACH_DATA_DIR', raising=False)
        monkeypatch.setattr(config, '_CHECKOUT_DATA_DIR',
                            tmp_path / 'no-such-dir')
        resolved = config.resolve_data_dir()
        assert resolved == config.user_data_dir()
        assert resolved.name == 'coach-mcp'

    def test_user_dir_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, 'platform', 'win32')
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'LocalAppData'))
        assert config.user_data_dir() == \
            tmp_path / 'LocalAppData' / 'coach-mcp'

    def test_user_dir_macos(self, monkeypatch):
        monkeypatch.setattr(sys, 'platform', 'darwin')
        assert config.user_data_dir() == \
            Path.home() / 'Library' / 'Application Support' / 'coach-mcp'

    def test_user_dir_linux_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
        assert config.user_data_dir() == tmp_path / 'xdg' / 'coach-mcp'

    def test_user_dir_linux_default(self, monkeypatch):
        monkeypatch.setattr(sys, 'platform', 'linux')
        monkeypatch.delenv('XDG_DATA_HOME', raising=False)
        assert config.user_data_dir() == \
            Path.home() / '.local' / 'share' / 'coach-mcp'


class TestTokenDirResolution:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv('COACH_TOKEN_DIR', str(tmp_path / 'tok'))
        assert config.resolve_token_dir() == str(tmp_path / 'tok')

    def test_checkout_mode_uses_repo_garth(self, monkeypatch):
        monkeypatch.delenv('COACH_TOKEN_DIR', raising=False)
        assert config.resolve_token_dir(REPO_ROOT / 'data') == \
            str(REPO_ROOT / '.garth')

    def test_user_mode_uses_garmin_tokens(self, tmp_path, monkeypatch):
        monkeypatch.delenv('COACH_TOKEN_DIR', raising=False)
        assert config.resolve_token_dir(tmp_path) == \
            str(config.user_data_dir() / 'garmin-tokens')

    def test_token_dir_is_str_for_garth(self):
        """garth requires a str token store, not a Path."""
        assert isinstance(config.TOKEN_DIR, str)


# ---------------------------------------------------------------------------
# 2. Defaults bootstrap (check_setup)
# ---------------------------------------------------------------------------

class TestDefaultsBootstrap:
    def test_bootstrap_creates_dir_and_seeds_methodology(
            self, sandbox_data_dir, monkeypatch):
        fresh = sandbox_data_dir / 'fresh' / 'data'
        monkeypatch.setattr(parsers, 'DATA_DIR', fresh)

        assert parsers.check_setup() is False  # personal files still missing

        assert fresh.is_dir()
        seeded = json.loads(
            (fresh / 'methodology.json').read_text(encoding='utf-8'))
        packaged = json.loads(
            (config.DEFAULTS_DIR / 'methodology.json').read_text(
                encoding='utf-8'))
        assert seeded == packaged

    def test_bootstrap_never_overwrites_existing_methodology(
            self, sandbox_data_dir, monkeypatch):
        custom = {'pillars': {'strength_sessions_per_week': 9}}
        (sandbox_data_dir / 'methodology.json').write_text(
            json.dumps(custom), encoding='utf-8')
        monkeypatch.setattr(parsers, 'DATA_DIR', sandbox_data_dir)

        parsers.check_setup()

        assert json.loads(
            (sandbox_data_dir / 'methodology.json').read_text(
                encoding='utf-8')) == custom

    def test_check_setup_true_when_personal_files_exist(
            self, sandbox_data_dir, monkeypatch):
        monkeypatch.setattr(parsers, 'DATA_DIR', sandbox_data_dir)
        for name in ('athlete.json', 'training_config.json'):
            (sandbox_data_dir / name).write_text('{}', encoding='utf-8')

        assert parsers.check_setup() is True

    def test_bootstrap_is_idempotent(self, sandbox_data_dir, monkeypatch):
        monkeypatch.setattr(parsers, 'DATA_DIR', sandbox_data_dir)
        parsers.bootstrap_data_dir()
        first = (sandbox_data_dir / 'methodology.json').read_bytes()
        parsers.bootstrap_data_dir()
        assert (sandbox_data_dir / 'methodology.json').read_bytes() == first


# ---------------------------------------------------------------------------
# 3. Packaged default stays in sync with the tracked data/methodology.json
# ---------------------------------------------------------------------------

class TestDefaultsInSync:
    def test_packaged_methodology_matches_tracked_copy(self):
        packaged = json.loads(
            (REPO_ROOT / 'coach' / 'defaults' / 'methodology.json')
            .read_text(encoding='utf-8'))
        tracked = json.loads(
            (REPO_ROOT / 'data' / 'methodology.json')
            .read_text(encoding='utf-8'))
        assert packaged == tracked, (
            'coach/defaults/methodology.json drifted from '
            'data/methodology.json — update BOTH together (the defaults copy '
            'ships in the wheel; the data/ copy drives the checkout).'
        )


# ---------------------------------------------------------------------------
# 4. Entry points
# ---------------------------------------------------------------------------

class TestEntryPoints:
    def test_coach_server_module_has_main_and_mcp(self):
        mod = importlib.import_module('coach.server')
        assert callable(mod.main)
        assert hasattr(mod, 'mcp')

    def test_root_shim_still_works(self):
        """`python server.py` from a checkout must keep working."""
        mod = importlib.import_module('server')
        assert hasattr(mod, 'mcp')
        assert callable(mod.main)

    def test_pyproject_declares_console_script_and_build_system(self):
        py = tomllib.loads(
            (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        assert py['project']['scripts']['garmin-coach-mcp'] == 'coach.server:main'
        # coach-mcp ships as a convenience alias to the same entry point.
        assert py['project']['scripts']['coach-mcp'] == 'coach.server:main'
        assert py['build-system']['build-backend'] == 'hatchling.build'
        # coach/defaults/*.json ships because the whole package is included
        assert py['tool']['hatch']['build']['targets']['wheel']['packages'] \
            == ['coach']

    def test_main_exits_1_when_setup_incomplete(self, monkeypatch):
        import coach.server as srv
        monkeypatch.setattr(srv, 'check_setup', lambda: False)
        with pytest.raises(SystemExit) as exc:
            srv.main()
        assert exc.value.code == 1

    def test_main_runs_mcp_with_normalized_transport(self, monkeypatch):
        import coach.server as srv
        seen = {}
        monkeypatch.setattr(srv, 'check_setup', lambda: True)
        monkeypatch.setattr(
            srv.mcp, 'run', lambda transport: seen.setdefault('t', transport))
        monkeypatch.setenv('COACH_TRANSPORT', ' HTTP ')
        monkeypatch.delenv('COACH_CODE_MODE', raising=False)

        srv.main()

        assert seen['t'] == 'http'


# ---------------------------------------------------------------------------
# 5. Setup wizard writes to the resolved data dir
# ---------------------------------------------------------------------------

def _load_wizard():
    path = REPO_ROOT / 'scripts' / 'setup_wizard.py'
    spec = importlib.util.spec_from_file_location('setup_wizard', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSetupWizard:
    def test_wizard_binds_resolved_data_dir(self):
        """The wizard must use coach.config's resolution, not its own
        repo-relative path (under the test sandbox, config.DATA_DIR is the
        sandbox — a repo-relative recomputation would NOT match)."""
        wizard = _load_wizard()
        assert wizard.DATA_DIR == config.DATA_DIR

    def test_run_setup_writes_to_resolved_dir(self, tmp_path, monkeypatch):
        wizard = _load_wizard()
        target = tmp_path / 'resolved' / 'data'
        monkeypatch.setattr(wizard, 'DATA_DIR', target)
        # Accept every default the wizard offers
        monkeypatch.setattr('builtins.input', lambda prompt='': '')

        wizard.run_setup()

        for name in ('athlete.json', 'training_config.json',
                     'weekly_plan.json', 'coaching_log.json'):
            assert (target / name).exists(), f'{name} not created in {target}'
        athlete = json.loads(
            (target / 'athlete.json').read_text(encoding='utf-8'))
        assert athlete['personal']['age'] == 30  # wizard default accepted


# ---------------------------------------------------------------------------
# 6. Registry/release plumbing (Lane C)
# ---------------------------------------------------------------------------

def _load_pyproject():
    return tomllib.loads(
        (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


def _load_server_json():
    return json.loads(
        (REPO_ROOT / 'server.json').read_text(encoding='utf-8'))


class TestServerJson:
    """server.json drives the official MCP Registry listing."""

    def test_parses_and_declares_registry_schema(self):
        sj = _load_server_json()
        assert sj['$schema'].startswith(
            'https://static.modelcontextprotocol.io/schemas/')

    def test_namespace_and_package_consistent_with_pyproject(self):
        sj = _load_server_json()
        py = _load_pyproject()
        assert sj['name'] == 'io.github.snoozelieb/coach-mcp'
        pkg = sj['packages'][0]
        assert pkg['registryType'] == 'pypi'
        assert pkg['identifier'] == py['project']['name']
        # uvx <identifier> must resolve to the console script of that name.
        assert pkg['identifier'] in py['project']['scripts']
        assert sj['repository']['url'] == py['project']['urls']['Repository']

    def test_stdio_transport_with_uvx_runtime(self):
        pkg = _load_server_json()['packages'][0]
        assert pkg['transport']['type'] == 'stdio'
        assert pkg['runtimeHint'] == 'uvx'

    def test_environment_variables_declared(self):
        pkg = _load_server_json()['packages'][0]
        env = {e['name']: e for e in pkg['environmentVariables']}
        assert set(env) >= {'GARMIN_EMAIL', 'GARMIN_PASSWORD',
                            'COACH_DATA_DIR'}
        assert env['GARMIN_EMAIL']['isRequired'] is True
        assert env['GARMIN_PASSWORD']['isRequired'] is True
        assert env['GARMIN_PASSWORD']['isSecret'] is True
        assert env['COACH_DATA_DIR'].get('isRequired') is False


class TestReleaseWorkflow:
    """release.yml must publish via trusted publishing (no token in repo)."""

    WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'release.yml'

    def test_references_trusted_publishing_and_pypi_environment(self):
        # Text-level pins that hold even without a YAML parser installed.
        text = self.WORKFLOW.read_text(encoding='utf-8')
        assert 'pypa/gh-action-pypi-publish@release/v1' in text
        assert 'id-token: write' in text
        assert 'name: pypi' in text

    def test_parses_as_yaml_with_expected_structure(self):
        # pyyaml is a transitive dep (jsonschema-path) — skip, don't error,
        # if it ever drops out; the text-level test above still guards.
        yaml = pytest.importorskip('yaml')
        wf = yaml.safe_load(self.WORKFLOW.read_text(encoding='utf-8'))
        # YAML 1.1 loads the bare `on:` key as boolean True.
        triggers = wf.get('on', wf.get(True))
        assert triggers['push']['tags'] == ['v*']
        publish = wf['jobs']['publish-to-pypi']
        assert publish['environment']['name'] == 'pypi'
        assert publish['permissions'] == {'id-token': 'write'}
        uses = [s.get('uses', '') for s in publish['steps']]
        assert any(u.startswith('pypa/gh-action-pypi-publish@release/v1')
                   for u in uses)


class TestVersionConsistency:
    def test_pyproject_server_json_changelog_agree(self):
        py_version = _load_pyproject()['project']['version']
        sj = _load_server_json()
        assert sj['version'] == py_version
        assert sj['packages'][0]['version'] == py_version
        changelog = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        match = re.search(r'^## \[(\d+\.\d+\.\d+)\]', changelog, re.M)
        assert match, 'CHANGELOG.md has no "## [x.y.z]" version header'
        assert match.group(1) == py_version, (
            'CHANGELOG.md latest version differs from pyproject — add a '
            'changelog section for the release before tagging.')
