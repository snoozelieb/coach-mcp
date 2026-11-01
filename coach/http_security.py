"""HTTP transport protection.

The default transport is stdio: the MCP client starts the server as a child
process and nothing listens on the network. When COACH_TRANSPORT selects an
HTTP transport the server listens on a socket, and anything that can reach
that socket can call every tool (the athlete's health data, the Garmin
calendar). Three things are therefore required before the server listens
(MCP security best practices, spec 2026-07-28):

1. A bearer token. COACH_HTTP_TOKEN (MIN_TOKEN_CHARS or more) is checked on
   every request by FastMCP's StaticTokenVerifier; clients send it as
   ``Authorization: Bearer <token>``. Without a token the server refuses to
   start. COACH_HTTP_INSECURE=1 waives the token for a loopback bind only
   (127.0.0.1, localhost, ::1): other processes on the machine can still
   call it, nothing else can.
2. Host and Origin checks on every request (DNS-rebinding protection: a page
   in the athlete's browser must not be able to reach the server through a
   hostname it controls). FastMCP's HostOriginGuardMiddleware runs in strict
   mode; its default is off. Extra hostnames the server may be addressed by
   go in COACH_HTTP_ALLOWED_HOSTS, browser origins allowed to call it in
   COACH_HTTP_ALLOWED_ORIGINS (comma-separated).
3. A loopback bind by default (FASTMCP_HOST; FastMCP's own default is
   127.0.0.1). A bind other machines can reach is allowed only with a token.
   The host and port checked here are the ones FastMCP will bind: server.main
   passes FastMCP's settings (which merge FASTMCP_HOST/PORT from the
   environment and FastMCP's own .env file), and run_kwargs hands the same
   values back to FastMCP.run(), so the check and the bind cannot diverge.

There is no TLS here: keep HTTP on localhost or behind your own TLS proxy.

Pure functions over an environment mapping, so the tests need no socket.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

HTTP_TRANSPORTS = ('http', 'streamable-http', 'sse')
TOKEN_ENV = 'COACH_HTTP_TOKEN'
INSECURE_ENV = 'COACH_HTTP_INSECURE'
ALLOWED_HOSTS_ENV = 'COACH_HTTP_ALLOWED_HOSTS'
ALLOWED_ORIGINS_ENV = 'COACH_HTTP_ALLOWED_ORIGINS'
HOST_ENV = 'FASTMCP_HOST'
PORT_ENV = 'FASTMCP_PORT'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8000
MIN_TOKEN_CHARS = 16
CLIENT_ID = 'coach-mcp-http-client'
LOOPBACK_NAMES = frozenset({'localhost', 'localhost.localdomain'})
TRUE_WORDS = frozenset({'1', 'true', 'yes', 'on'})
TOKEN_HINT = "python -c \"import secrets; print(secrets.token_urlsafe(32))\""


class HttpSecurityError(RuntimeError):
    """The HTTP transport was requested without the protection it needs."""


@dataclass(frozen=True)
class HttpSecurity:
    host: str
    port: int
    token: str | None
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]

    @property
    def loopback(self) -> bool:
        return is_loopback_host(self.host)

    @property
    def authenticated(self) -> bool:
        return self.token is not None


def is_loopback_host(host: str) -> bool:
    """True for localhost names and loopback addresses (v4 and v6)."""
    bare = str(host or '').strip().strip('[]').lower()
    if bare in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def _truthy(value) -> bool:
    return str(value or '').strip().lower() in TRUE_WORDS


def _csv(value) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value or '').split(',') if part.strip())


def http_security(environ, host: str | None = None, port: int | str | None = None) -> HttpSecurity:
    """The transport settings the environment asks for, or HttpSecurityError
    naming the fix when the HTTP transport would listen unprotected.

    ``host``/``port`` are the values the transport will bind (FastMCP's
    settings); when omitted, FASTMCP_HOST/FASTMCP_PORT are read from
    ``environ`` with FastMCP's defaults."""
    host = str(host or environ.get(HOST_ENV) or '').strip() or DEFAULT_HOST
    port_text = str(port if port is not None else (environ.get(PORT_ENV) or '')).strip() or str(DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        raise HttpSecurityError(f"{PORT_ENV}={port_text!r} is not a port number") from None
    token = str(environ.get(TOKEN_ENV) or '').strip() or None
    loopback = is_loopback_host(host)

    if token is not None and len(token) < MIN_TOKEN_CHARS:
        raise HttpSecurityError(
            f"{TOKEN_ENV} is {len(token)} characters; use at least {MIN_TOKEN_CHARS} "
            f"(for example: {TOKEN_HINT}).")
    if token is None and not loopback:
        raise HttpSecurityError(
            f"COACH_TRANSPORT is an HTTP transport bound to {host}, which other machines can reach, "
            f"and {TOKEN_ENV} is not set. Set {TOKEN_ENV} to a secret of {MIN_TOKEN_CHARS}+ characters "
            f"(clients send it as 'Authorization: Bearer <token>'), or bind to {DEFAULT_HOST}.")
    if token is None and not _truthy(environ.get(INSECURE_ENV)):
        raise HttpSecurityError(
            f"COACH_TRANSPORT is an HTTP transport and {TOKEN_ENV} is not set. Set {TOKEN_ENV} to a "
            f"secret of {MIN_TOKEN_CHARS}+ characters (clients send it as 'Authorization: Bearer <token>'; "
            f"generate one with {TOKEN_HINT}), or set {INSECURE_ENV}=1 to serve {host} without a token "
            "(loopback only). The stdio transport needs neither.")

    return HttpSecurity(host=host, port=port, token=token,
                        allowed_hosts=_csv(environ.get(ALLOWED_HOSTS_ENV)),
                        allowed_origins=_csv(environ.get(ALLOWED_ORIGINS_ENV)))


def run_kwargs(security: HttpSecurity) -> dict:
    """Keyword arguments for FastMCP.run(): the validated bind host and port,
    Host and Origin validated on every request (strict mode), plus the
    configured extra hosts and origins."""
    kwargs: dict = {'host': security.host, 'port': security.port, 'host_origin_protection': True}
    if security.allowed_hosts:
        kwargs['allowed_hosts'] = list(security.allowed_hosts)
    if security.allowed_origins:
        kwargs['allowed_origins'] = list(security.allowed_origins)
    return kwargs


def token_verifier(token: str):
    """FastMCP's static bearer-token verifier holding the one configured token."""
    # Startup-only import (server.main before anything is served): the stdio
    # transport, the common path, never loads the auth providers.
    from fastmcp.server.auth import StaticTokenVerifier
    return StaticTokenVerifier(tokens={token: {'client_id': CLIENT_ID, 'scopes': []}})


def describe(security: HttpSecurity, transport: str) -> str:
    """One stderr line at startup saying what protects the socket."""
    auth = ('bearer token required on every request' if security.authenticated
            else f'NO TOKEN ({INSECURE_ENV}=1, loopback bind only)')
    extra = ''
    if security.allowed_hosts:
        extra += f"; extra allowed hosts: {', '.join(security.allowed_hosts)}"
    if security.allowed_origins:
        extra += f"; allowed origins: {', '.join(security.allowed_origins)}"
    return (f"{transport} transport on {security.host}:{security.port}: {auth}; "
            f"Host and Origin headers validated on every request{extra}")
