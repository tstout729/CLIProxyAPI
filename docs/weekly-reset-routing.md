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

## Weekly reserve

Weekly-reset-first drains the account whose week ends first. To keep part of an account's week for work outside the proxy, such as a scheduled job on the same subscription, set a reserve:

```yaml
routing:
  strategy: weekly-reset-first
  weekly-reserve-percent: 15
```

The value is a percentage of the weekly limit on the providers' 0-100 scale (`15` means 15% of the week left), not a 0-1 fraction. Default `0` disables the reserve.

- An account whose applicable weekly balance is at or below its reserve is skipped while another available account is above its own reserve.
- If every available account is at or below its reserve, normal ranking applies, so the reserve never fails a request. A request retry that has already tried the other accounts can also use a reserved account.
- Exhausted accounts (0%) stay excluded. Unknown or stale balances are never reserved; such an account is used before a reserved one but still ranks after known balances.
- Only weekly windows count; five-hour windows do not. A model-scoped weekly window (a Claude family such as Opus, or a Codex additional model limit) reserves the account only for matching models.
- Like exhaustion, the reserve applies before manual priority and before thread affinity. A thread bound to an account that reaches its reserve moves to an unreserved account on its next request.
- For one account, add a top-level number to its auth JSON, next to `priority`: `"weekly_reserve_percent": 30`. It overrides the global value; `0` exempts that account. Invalid values (outside 0-100 or not a number) are ignored.

The global value hot-reloads with the config file. Changing it rebuilds the routing selector, which clears in-memory thread bindings, as any routing change does. `GET /v8/management/routing/weekly-status` reports `weekly_reserve_percent` and, per account, `reserve_percent` and `reserved` (account-wide weekly window at or below the reserve).

## When every account is out of usage

A thread moves to another account as soon as its own account runs out. When no account for the model has usage left, the proxy can't switch, so it tells the client when to come back:

- The request gets a 429 rate-limit error, not `503 no auth candidates`. The message names the first reset in Pacific time, for example `All credentials for model claude-opus-5-5 are out of usage until Oct 14 3:10 PM PDT`.
- `Retry-After` gives the seconds until that reset. Claude responses also carry `Anthropic-Ratelimit-Unified-Reset`, the Unix time of the reset, which Claude Code's unattended retry mode waits for. Other cooldowns (every account cooling down after upstream errors) carry the same header.
- An account is usable again only when every exhausted window has reset. For example, an account with both its five-hour and weekly windows at 0% counts from its weekly reset.
- No upstream request is made while every account is exhausted. When the reset passes, the next request is routed normally. Usage polling confirms the new window within about 15 seconds.

The Claude launchers turn on Claude Code's unattended retry mode, so a running turn waits for the reset and then resends the same request instead of ending:

```sh
CLAUDE_CODE_RETRY_WATCHDOG=1                    # wait out 429 and 529 errors
CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS=21600000 # give up after 6 hours of waiting
CLAUDE_CODE_MAX_RETRIES=15                      # other errors keep a bounded retry count
```

`scripts/claude-t3proxy.sh` (T3 Code's Claude binary path, installed as `~/.local/bin/claude-t3proxy`) and `scripts/home-coding-session.py` (shell, cmux and home sessions) set these unless the environment already does. While waiting, Claude Code reports an `api_retry` status about every 30 seconds. A Claude process started before these launchers were installed keeps its old settings until it restarts; without unattended retry mode, Claude Code stops at the first 429 whose reset is more than 60 seconds away.
