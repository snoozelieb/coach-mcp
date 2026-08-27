# MCP Server Best Practices

## Quick Reference

### Server Naming
- **Python**: `{service}_mcp` (e.g., `slack_mcp`)
- **Node/TypeScript**: `{service}-mcp-server` (e.g., `slack-mcp-server`)

### Tool Naming
- Use snake_case with service prefix
- Format: `{service}_{action}_{resource}`
- Example: `slack_send_message`, `github_create_issue`

### Response Formats
- Support both JSON and Markdown formats
- JSON for programmatic processing
- Markdown for human readability

### Pagination
- Always respect `limit` parameter
- Return `has_more`, `next_offset`, `total_count`
- Default to 20-50 items

### Transport
- **Streamable HTTP**: For remote servers, multi-client scenarios (stateless per spec 2026-07-28)
- **stdio**: For local integrations, command-line tools
- The legacy HTTP+SSE transport is formally Deprecated (SEP-2596, year-long offramp) — never offer it for new servers

### Protocol Lifecycle (spec 2026-07-28)
- No `initialize` handshake, no protocol-level sessions
- Every request carries protocol version + client capabilities in `_meta`
- Servers MUST implement `server/discover`
- All results carry a required `resultType` (`"complete"` / `"input_required"`)

---

## Server Naming Conventions

Follow these standardized naming patterns:

**Python**: Use format `{service}_mcp` (lowercase with underscores)
- Examples: `slack_mcp`, `github_mcp`, `jira_mcp`

**Node/TypeScript**: Use format `{service}-mcp-server` (lowercase with hyphens)
- Examples: `slack-mcp-server`, `github-mcp-server`, `jira-mcp-server`

The name should be general, descriptive of the service being integrated, easy to infer from the task description, and without version numbers.

---

## Tool Naming and Design

### Tool Naming

1. **Use snake_case**: `search_users`, `create_project`, `get_channel_info`
2. **Include service prefix**: Anticipate that your MCP server may be used alongside other MCP servers
   - Use `slack_send_message` instead of just `send_message`
   - Use `github_create_issue` instead of just `create_issue`
3. **Be action-oriented**: Start with verbs (get, list, search, create, etc.)
4. **Be specific**: Avoid generic names that could conflict with other servers

### Tool Design

- Tool descriptions must narrowly and unambiguously describe functionality
- Descriptions must precisely match actual functionality
- Provide tool annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
- Keep tool operations focused and atomic

---

## Response Formats

All tools that return data should support multiple formats:

### JSON Format (`response_format="json"`)
- Machine-readable structured data
- Include all available fields and metadata
- Consistent field names and types
- Use for programmatic processing

### Markdown Format (`response_format="markdown"`, typically default)
- Human-readable formatted text
- Use headers, lists, and formatting for clarity
- Convert timestamps to human-readable format
- Show display names with IDs in parentheses
- Omit verbose metadata

---

## Pagination

For tools that list resources:

- **Always respect the `limit` parameter**
- **Implement pagination**: Use `offset` or cursor-based pagination
- **Return pagination metadata**: Include `has_more`, `next_offset`/`next_cursor`, `total_count`
- **Never load all results into memory**: Especially important for large datasets
- **Default to reasonable limits**: 20-50 items is typical

Example pagination response:
```json
{
  "total": 150,
  "count": 20,
  "offset": 0,
  "items": [...],
  "has_more": true,
  "next_offset": 20
}
```

---

## Transport Options

### Streamable HTTP

**Best for**: Remote servers, web services, multi-client scenarios

**Characteristics (spec 2026-07-28)**:
- Stateless request/response — no sessions, no `Mcp-Session-Id` header, no sticky routing; a tool call is a single self-contained HTTP request
- POST requests REQUIRE the standard headers `Mcp-Method` and `Mcp-Name` (SEP-2243) so intermediaries (load balancers, gateways, WAFs) can route and filter without parsing bodies; missing/mismatched headers are rejected 400 with `HeaderMismatch` (`Mcp-Name` applies to `tools/call`, `resources/read`, `prompts/get`)
- SSE stream resumability and `Last-Event-ID` redelivery are removed: a broken response stream loses the in-flight request and the client MUST re-issue it with a new request ID — design tools to tolerate re-issue (idempotency matters more than ever)
- The HTTP GET endpoint and `resources/subscribe` are replaced by a single long-lived `subscriptions/listen` POST stream for opted-in server-to-client change notifications; request-scoped `notifications/progress` still flows on the request's own response stream

**Use when**:
- Serving multiple clients simultaneously
- Deploying as a cloud service
- Integration with web applications

### stdio

**Best for**: Local integrations, command-line tools

**Characteristics**:
- Standard input/output stream communication
- Simple setup, no network configuration needed
- Runs as a subprocess of the client

**Use when**:
- Building tools for local development environments
- Integrating with desktop applications
- Single-user, single-session scenarios

**Note**: stdio servers should NOT log to stdout (use stderr for logging). Logging to stderr is also the spec-recommended migration off the deprecated MCP Logging feature (SEP-2577).

### Transport Selection

| Criterion | stdio | Streamable HTTP |
|-----------|-------|-----------------|
| **Deployment** | Local | Remote |
| **Clients** | Single | Multiple |
| **Complexity** | Low | Medium |
| **Change notifications** | No | Opt-in via `subscriptions/listen` |

---

## Protocol Lifecycle and State (spec 2026-07-28)

The 2026-07-28 revision made MCP a stateless request/response protocol:

- **No handshake**: the `initialize`/`notifications/initialized` exchange is removed (SEP-2575). Every request carries protocol version and client capabilities in `_meta` (`io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientCapabilities`); version mismatch returns `UnsupportedProtocolVersionError`.
- **No sessions**: protocol-level sessions and the `Mcp-Session-Id` header are removed (SEP-2567).
- **`server/discover` is mandatory**: it advertises supported protocol versions, capabilities, and identity; clients may call it up-front or use it as a backward-compatibility probe on stdio. Updated SDKs answer it automatically.
- **Cross-call state**: "Servers that need cross-call state use explicit, server-minted handles passed as ordinary tool arguments." Persist state server-side (database, files), mint an id, and accept that id as a normal tool parameter. Never depend on protocol sessions or sticky routing.
- **Interactive input via MRTR**: server-initiated requests (`roots/list`, `sampling/createMessage`, `elicitation/create`) are no longer supported (SEP-2322). A tool needing mid-call user input returns an `InputRequiredResult` (`resultType: "input_required"`); the client retries the original request with `inputResponses`.

## Cacheable List Results (SEP-2549)

- `tools/list`, `prompts/list`, `resources/list`, `resources/read`, and `resources/templates/list` results REQUIRE `ttlMs` and `cacheScope` fields.
- `cacheScope` is `"public"` or `"private"` — it controls whether shared intermediaries may cache the response. There is no safe default: choose `"private"` for anything user-specific.
- Servers SHOULD return tools in deterministic order — this enables client-side caching and improves LLM prompt cache hit rates. Keep tool registration order stable across restarts.

## Deprecated and Removed Features

Under the feature lifecycle policy (SEP-2596, minimum 12-month deprecation window; registry at `/specification/2026-07-28/deprecated`):

| Feature | Status | Migration |
|---------|--------|-----------|
| Roots | Deprecated (SEP-2577) | Pass directories/files via tool parameters, resource URIs, or server config |
| Sampling | Deprecated (SEP-2577) | Call LLM provider APIs directly from the server |
| MCP Logging | Deprecated (SEP-2577) | Log to stderr (stdio) or use OpenTelemetry |
| HTTP+SSE transport | Deprecated (SEP-2596) | Migrate to Streamable HTTP |
| `ping`, `logging/setLevel`, `notifications/roots/list_changed` | Removed | Log level is per-request via `_meta` `io.modelcontextprotocol/logLevel` |

Do not design new servers around deprecated features.

---

## Security Best Practices

### Authentication and Authorization

**OAuth 2.1**:
- Use secure OAuth 2.1 with certificates from recognized authorities
- Validate access tokens before processing requests
- Only accept tokens specifically intended for your server
- Validate the `iss` parameter (RFC 9207) — required by the 2026-07-28 authorization hardening
- RFC 7591 Dynamic Client Registration is deprecated in favor of Client ID Metadata Documents

**API Keys**:
- Store API keys in environment variables, never in code
- Validate keys on server startup
- Provide clear error messages when authentication fails

### Input Validation

- Sanitize file paths to prevent directory traversal
- Validate URLs and external identifiers
- Check parameter sizes and ranges
- Prevent command injection in system calls
- Use schema validation (Pydantic/Zod) for all inputs

### Error Handling

- Don't expose internal errors to clients
- Log security-relevant errors server-side
- Provide helpful but not revealing error messages
- Clean up resources after errors

### DNS Rebinding Protection

For streamable HTTP servers running locally:
- Enable DNS rebinding protection
- Validate the `Origin` header on all incoming connections
- Bind to `127.0.0.1` rather than `0.0.0.0`

---

## Tool Annotations

Provide annotations to help clients understand tool behavior:

| Annotation | Type | Default | Description |
|-----------|------|---------|-------------|
| `readOnlyHint` | boolean | false | Tool does not modify its environment |
| `destructiveHint` | boolean | true | Tool may perform destructive updates |
| `idempotentHint` | boolean | false | Repeated calls with same args have no additional effect |
| `openWorldHint` | boolean | true | Tool interacts with external entities |

**Important**: Annotations are hints, not security guarantees. Clients should not make security-critical decisions based solely on annotations.

---

## Error Handling

- Use standard JSON-RPC error codes. The 2026-07-28 spec partitions the server-error range: `-32000..-32019` is implementation-defined, `-32020..-32099` is reserved for the MCP spec (`HeaderMismatch` -32020, `MissingRequiredClientCapability` -32021, `UnsupportedProtocolVersion` -32022); resource-not-found changed from -32002 to -32602
- Report tool errors within result objects (not protocol-level errors)
- Provide helpful, specific error messages with suggested next steps
- Don't expose internal implementation details
- Clean up resources properly on errors

Example error handling:
```typescript
try {
  const result = performOperation();
  return { content: [{ type: "text", text: result }] };
} catch (error) {
  return {
    isError: true,
    content: [{
      type: "text",
      text: `Error: ${error.message}. Try using filter='active_only' to reduce results.`
    }]
  };
}
```

---

## Testing Requirements

Comprehensive testing should cover:

- **Functional testing**: Verify correct execution with valid/invalid inputs
- **Integration testing**: Test interaction with external systems
- **Security testing**: Validate auth, input sanitization, rate limiting
- **Performance testing**: Check behavior under load, timeouts
- **Error handling**: Ensure proper error reporting and cleanup

---

## Documentation Requirements

- Provide clear documentation of all tools and capabilities
- Include working examples (at least 3 per major feature)
- Document security considerations
- Specify required permissions and access levels
- Document rate limits and performance characteristics
