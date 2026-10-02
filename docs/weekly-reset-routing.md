# Weekly reset routing and account affinity

This fork can prioritize Claude and Codex OAuth accounts by their next weekly reset. New threads use the eligible account whose observed weekly balance expires first. The server fetches usage itself; the management console does not drive routing.

```yaml
routing:
  strategy: weekly-reset-first
  session-affinity: true
  session-affinity-ttl: 168h
  session-affinity-subagents: true
upstream:
  codex:
    upstream-websockets: true
```

Manual credential priority remains an explicit override for new threads. An existing thread stays on its bound account across supported models, even if another account's reset is now earlier or its priority is higher. A disabled, unavailable, unsupported, or exhausted bound account falls back and rebinds. Accounts already tried by the request's existing retry path are excluded. Known five-hour exhaustion also makes an account ineligible. Claude family and Codex additional model limits apply only to their matching models.

Claude observations use `/api/oauth/usage`; Codex observations use `/backend-api/wham/usage`, through the existing provider executors and proxy configuration. Four workers bound concurrent usage requests. Polling starts with the service, refreshes about every five minutes, and refreshes after an observed reset within the next 15-second scan. Failed requests retain the last good observation for 15 minutes. Unknown or stale reset dates rank after current observations; they remain eligible fallback. A passed reset becomes unknown until the provider confirms fresh capacity. Monthly and code-review windows never become weekly routing dates. Explicit provider capacity refusal still excludes an account when percentages are absent.

Codex OAuth generation uses upstream WebSockets by default for HTTP, SSE, and WebSocket clients. `upstream.codex.upstream-websockets: false` disables this provider-wide; a credential's explicit `websockets: false` also preserves HTTP. API-key credentials retain their existing explicit WebSocket setting. The existing HTTP fallback on an unsupported WebSocket upgrade and replay protections remain in place. Native execution sessions reuse their upstream socket. Ordinary HTTP/SSE threads preserve account and prompt-cache identity; they do not receive an artificial persistent socket lifecycle.

Thread bindings, quota observations, and sockets are in memory and are rebuilt after restarting the service. Bindings use a sliding TTL. Explicit session headers, `prompt_cache_key`, and existing client/session identity detection are retained. This preserves the account and identity needed for provider prompt caching; the provider controls cache eligibility, retention, and hit rates.

`GET /v8/management/routing/weekly-status` is protected by the existing management authentication. It reports the active strategy and safe account indexes, statuses, observed balances, capacity buckets, and reset/observation timestamps in Pacific time. It excludes raw auth IDs, filenames, emails, tokens, account IDs, and provider response bodies. Historical balances can appear in a stale status; the selector does not use stale dates for ranking.
