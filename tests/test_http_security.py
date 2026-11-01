"""HTTP transport protection: the bearer token, the Host/Origin checks and
the loopback-only waiver, from the environment through server.main() to
FastMCP.run(). No socket is opened: FastMCP.run is recorded, not called."""
import asyncio

import fastmcp
import pytest

import coach.http_security as hs
import coach.server as server_mod
from coach.mcp_app import mcp

TOKEN = 'k7Qx2m9Lp4Vw8Rt1Zs6Yb3Nh5Jc0Fd'


class TestLoopback:
    @pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '::1', '[::1]', 'LOCALHOST', '127.0.0.2'])
    def test_loopback_names_and_addresses(self, host):
        assert hs.is_loopback_host(host)

    @pytest.mark.parametrize('host', ['0.0.0.0', '192.168.1.5', '10.0.0.1', '::', 'coach.local', ''])
    def test_everything_else_is_not(self, host):
        assert not hs.is_loopback_host(host)


class TestSettings:
    def test_http_without_a_token_is_refused_with_the_fix_named(self):
        with pytest.raises(hs.HttpSecurityError) as exc:
            hs.http_security({})
        message = str(exc.value)
        assert 'COACH_HTTP_TOKEN' in message and 'COACH_HTTP_INSECURE' in message and 'Bearer' in message

    def test_insecure_waiver_applies_to_loopback_only(self):
        settings = hs.http_security({'COACH_HTTP_INSECURE': '1'})
        assert settings.token is None and settings.loopback and not settings.authenticated
        with pytest.raises(hs.HttpSecurityError, match='other machines can reach'):
            hs.http_security({'COACH_HTTP_INSECURE': '1', 'FASTMCP_HOST': '0.0.0.0'})

    def test_a_non_loopback_bind_needs_a_token(self):
        with pytest.raises(hs.HttpSecurityError, match='COACH_HTTP_TOKEN'):
            hs.http_security({'FASTMCP_HOST': '192.168.1.5'})
        settings = hs.http_security({'FASTMCP_HOST': '192.168.1.5', 'COACH_HTTP_TOKEN': TOKEN})
        assert settings.authenticated and settings.host == '192.168.1.5' and not settings.loopback

    def test_short_tokens_are_refused(self):
        with pytest.raises(hs.HttpSecurityError, match='16'):
            hs.http_security({'COACH_HTTP_TOKEN': 'short'})

    def test_token_is_stripped_and_port_parsed(self):
        settings = hs.http_security({'COACH_HTTP_TOKEN': f'  {TOKEN} \n', 'FASTMCP_PORT': '8765'})
        assert settings.token == TOKEN and settings.port == 8765 and settings.host == '127.0.0.1'

    def test_bad_port_is_an_error(self):
        with pytest.raises(hs.HttpSecurityError, match='FASTMCP_PORT'):
            hs.http_security({'COACH_HTTP_TOKEN': TOKEN, 'FASTMCP_PORT': 'eighty'})

    def test_the_bind_values_win_over_the_environment(self):
        # FastMCP merges FASTMCP_HOST/PORT from the environment AND its own .env
        # file; the check must see the host FastMCP will bind, not a guess.
        with pytest.raises(hs.HttpSecurityError, match='other machines can reach'):
            hs.http_security({'COACH_HTTP_INSECURE': '1'}, host='0.0.0.0')
        settings = hs.http_security({'COACH_HTTP_TOKEN': TOKEN, 'FASTMCP_HOST': '127.0.0.1'},
                                    host='192.168.1.5', port=9000)
        assert (settings.host, settings.port, settings.loopback) == ('192.168.1.5', 9000, False)

    def test_allowed_hosts_and_origins_are_comma_lists(self):
        settings = hs.http_security({'COACH_HTTP_TOKEN': TOKEN,
                                     'COACH_HTTP_ALLOWED_HOSTS': 'coach.lan, 192.168.1.5:8000 ,',
                                     'COACH_HTTP_ALLOWED_ORIGINS': 'https://app.example'})
        assert settings.allowed_hosts == ('coach.lan', '192.168.1.5:8000')
        assert settings.allowed_origins == ('https://app.example',)


class TestRunKwargs:
    def test_the_validated_bind_and_strict_checks_reach_run(self):
        settings = hs.http_security({'COACH_HTTP_TOKEN': TOKEN}, host='127.0.0.1', port=8765)
        assert hs.run_kwargs(settings) == {'host': '127.0.0.1', 'port': 8765, 'host_origin_protection': True}

    def test_extra_hosts_and_origins_are_forwarded_as_lists(self):
        settings = hs.http_security({'COACH_HTTP_TOKEN': TOKEN, 'COACH_HTTP_ALLOWED_HOSTS': 'a,b',
                                     'COACH_HTTP_ALLOWED_ORIGINS': 'https://x'})
        assert hs.run_kwargs(settings) == {'host': '127.0.0.1', 'port': 8000, 'host_origin_protection': True,
                                           'allowed_hosts': ['a', 'b'], 'allowed_origins': ['https://x']}


class TestTokenVerifier:
    def test_only_the_configured_token_verifies(self):
        verifier = hs.token_verifier(TOKEN)
        granted = asyncio.run(verifier.verify_token(TOKEN))
        assert granted is not None and granted.client_id == hs.CLIENT_ID
        assert asyncio.run(verifier.verify_token(TOKEN[:-1])) is None
        assert asyncio.run(verifier.verify_token('')) is None


class TestDescribe:
    def test_names_the_protection_in_force(self):
        settings = hs.http_security({'COACH_HTTP_TOKEN': TOKEN, 'COACH_HTTP_ALLOWED_HOSTS': 'coach.lan'})
        line = hs.describe(settings, 'http')
        assert 'bearer token' in line and 'Host and Origin' in line and 'coach.lan' in line
        waived = hs.describe(hs.http_security({'COACH_HTTP_INSECURE': '1'}), 'http')
        assert 'NO TOKEN' in waived and 'loopback' in waived


@pytest.fixture
def entry_point(monkeypatch):
    """server.main() with setup passing and FastMCP.run recorded, not started."""
    runs = []
    monkeypatch.setattr(server_mod, 'check_setup', lambda: True)
    monkeypatch.setattr(mcp, 'run', lambda **kwargs: runs.append(kwargs))
    monkeypatch.setattr(mcp, 'auth', None)
    monkeypatch.setattr(fastmcp.settings, 'host', '127.0.0.1')   # what FastMCP would bind
    monkeypatch.setattr(fastmcp.settings, 'port', 8000)
    for name in ('COACH_TRANSPORT', 'COACH_HTTP_TOKEN', 'COACH_HTTP_INSECURE', 'COACH_CODE_MODE',
                 'FASTMCP_HOST', 'FASTMCP_PORT', 'COACH_HTTP_ALLOWED_HOSTS', 'COACH_HTTP_ALLOWED_ORIGINS'):
        monkeypatch.delenv(name, raising=False)
    return runs


class TestEntryPoint:
    def test_stdio_runs_as_before(self, entry_point):
        server_mod.main()
        assert entry_point == [{'transport': 'stdio', 'show_banner': False}]
        assert mcp.auth is None

    def test_http_without_a_token_exits_before_listening(self, entry_point, monkeypatch, capsys):
        monkeypatch.setenv('COACH_TRANSPORT', 'http')
        with pytest.raises(SystemExit) as exc:
            server_mod.main()
        assert exc.value.code == 1
        assert entry_point == []
        out, err = capsys.readouterr()
        assert out == '' and 'COACH_HTTP_TOKEN' in err      # stdout stays JSON-RPC only

    @pytest.mark.parametrize('transport', ['http', 'streamable-http', 'sse'])
    def test_http_with_a_token_installs_the_verifier_and_strict_checks(
            self, entry_point, monkeypatch, capsys, transport):
        monkeypatch.setenv('COACH_TRANSPORT', transport)
        monkeypatch.setenv('COACH_HTTP_TOKEN', TOKEN)
        server_mod.main()
        assert entry_point == [{'transport': transport, 'show_banner': False, 'host': '127.0.0.1',
                                'port': 8000, 'host_origin_protection': True}]
        assert TOKEN in mcp.auth.tokens
        out, err = capsys.readouterr()
        assert out == '' and 'bearer token' in err

    def test_extra_hosts_reach_the_run_call(self, entry_point, monkeypatch):
        monkeypatch.setenv('COACH_TRANSPORT', 'http')
        monkeypatch.setenv('COACH_HTTP_TOKEN', TOKEN)
        monkeypatch.setattr(fastmcp.settings, 'host', '0.0.0.0')   # FastMCP's settings are the bind
        monkeypatch.setenv('COACH_HTTP_ALLOWED_HOSTS', '192.168.1.5:8000')
        server_mod.main()
        assert entry_point[0]['allowed_hosts'] == ['192.168.1.5:8000']

    def test_a_reachable_bind_without_a_token_never_listens(self, entry_point, monkeypatch, capsys):
        monkeypatch.setenv('COACH_TRANSPORT', 'http')
        monkeypatch.setattr(fastmcp.settings, 'host', '0.0.0.0')   # FastMCP's settings are the bind
        monkeypatch.setenv('COACH_HTTP_INSECURE', '1')
        with pytest.raises(SystemExit):
            server_mod.main()
        assert entry_point == [] and mcp.auth is None
        assert 'other machines can reach' in capsys.readouterr().err

    def test_the_host_fastmcp_would_bind_is_the_one_checked(self, entry_point, monkeypatch, capsys):
        # FASTMCP_HOST set only where FastMCP reads it (its settings / .env), not in
        # the process environment: the waiver must still be refused and the run
        # call must bind exactly what was checked.
        monkeypatch.setattr(fastmcp.settings, 'host', '0.0.0.0')
        monkeypatch.setenv('COACH_TRANSPORT', 'http')
        monkeypatch.setenv('COACH_HTTP_INSECURE', '1')
        with pytest.raises(SystemExit):
            server_mod.main()
        assert entry_point == [] and 'other machines can reach' in capsys.readouterr().err
        monkeypatch.setenv('COACH_HTTP_TOKEN', TOKEN)
        server_mod.main()
        assert entry_point[0]['host'] == '0.0.0.0' and TOKEN in mcp.auth.tokens

    def test_insecure_loopback_runs_without_a_verifier(self, entry_point, monkeypatch):
        monkeypatch.setenv('COACH_TRANSPORT', 'http')
        monkeypatch.setenv('COACH_HTTP_INSECURE', '1')
        server_mod.main()
        assert entry_point[0]['host_origin_protection'] is True
        assert mcp.auth is None
