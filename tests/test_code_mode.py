"""COACH_CODE_MODE=1 must never advertise an execute tool that cannot run.

FastMCP imports the Monty sandbox lazily at execute time, so a missing
``pydantic_monty`` used to surface only on the first call. The server probes
for the module before enabling the transform.
"""
import importlib.util

import pytest
from fastmcp import FastMCP

import coach.server as server


def test_code_mode_stays_off_without_the_sandbox(monkeypatch, caplog):
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == 'pydantic_monty':
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, 'find_spec', fake_find_spec)
    monkeypatch.setattr(server, 'mcp', FastMCP('probe'))  # never touch the shared instance

    with caplog.at_level('WARNING'):
        assert server._enable_code_mode() is False

    assert 'Monty sandbox' in caplog.text
    assert server.CODE_MODE_INSTALL_HINT in caplog.text


async def test_code_mode_registers_meta_tools_when_sandbox_present(monkeypatch):
    if importlib.util.find_spec('pydantic_monty') is None:
        pytest.skip('pydantic-monty not installed (pip install "fastmcp[code-mode]")')

    probe = FastMCP('probe')
    monkeypatch.setattr(server, 'mcp', probe)

    assert server._enable_code_mode() is True

    names = {t.name for t in await probe.list_tools()}
    assert 'execute_coaching_tools' in names
    assert 'search_tools' in names
