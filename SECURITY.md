# Security

coach-mcp is a local, single-user MCP server. There is no backend, no
telemetry, and no account system.

## Where your data lives

| What | Where |
|------|-------|
| Garmin credentials | `GARMIN_EMAIL` / `GARMIN_PASSWORD` env vars in your MCP client config, or a local `.env` in a source checkout. Never written to data files. |
| Garmin OAuth tokens | Local token store: `.garth/garmin_tokens.json` in a source checkout, or `<user-data-dir>/garmin-tokens/` for installed copies (override with `COACH_TOKEN_DIR`). Treat it like a password — it grants access to your Garmin account until it expires. |
| Health & coaching data | Local JSON in your data directory (`COACH_DATA_DIR`, a checkout's `data/`, or the per-user default): athlete profile, plans, fitness history, sleep history, coaching memory. |

All personal files are gitignored; the only data file shipped with the
package is `methodology.json` (generic safety rules and race templates).

## What leaves your machine

1. **Garmin's API** — login and data requests, sent only to Garmin Connect.
2. **Your MCP client's LLM** — whatever the client includes in the
   conversation (tool results contain your health data). That traffic is
   governed by your MCP client and LLM provider, not by this server.
3. **Public web pages** — when the coach researches a race, injury, sport,
   or exercise, the server fetches public pages; the request reveals only
   the search target (e.g. a race name). Only `https` URLs that resolve to
   public addresses are fetched — never loopback, private or link-local
   ranges, on the first request or on any redirect — and each request is
   sent to the address that passed that check (DNS pinning; TLS still
   verifies the hostname), without proxies, with a 2 MB size cap, and the
   text comes back to the model labelled as untrusted data. Names the model
   supplies are URL-encoded into the fixed research hosts' paths.

Nothing else. No analytics, no crash reporting. FastMCP would check PyPI for a newer version at startup; the server disables that banner check.

## Scope notes

- One athlete per data directory by design — there is no auth layer between
  athletes. Run separate instances with separate `COACH_DATA_DIR`s.
- The default transport is stdio (local only): nothing listens on the
  network. The HTTP transports (`COACH_TRANSPORT=http`, `streamable-http`,
  legacy `sse`) refuse to start without `COACH_HTTP_TOKEN` (16+ characters,
  sent by the client as `Authorization: Bearer <token>`); every request's
  `Host` and `Origin` headers are validated (DNS-rebinding protection), with
  `COACH_HTTP_ALLOWED_HOSTS` / `COACH_HTTP_ALLOWED_ORIGINS` for extra names.
  `COACH_HTTP_INSECURE=1` waives the token for a loopback bind only. There
  is no TLS: keep HTTP on localhost or behind your own TLS proxy.

## Reporting a vulnerability

Report security issues via
[GitHub private vulnerability reporting](https://github.com/snoozelieb/coach-mcp/security/advisories/new)
(preferred for anything sensitive), or open a regular issue at
<https://github.com/snoozelieb/coach-mcp/issues> for non-sensitive problems.
This is a spare-time project — expect a best-effort response, typically
within a couple of weeks.
