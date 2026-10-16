# Usage-limit reporting

The `weekly-reset-first` selector reports the observed usage limits that block accounts considered for the requested model. Session affinity uses the same report. Accounts excluded by routing, model support, or request restrictions are outside that count.

The inference error message contains the requested model, aggregate weekly and five-hour counts, and each blocked account's applicable limits and reset times. Window counts can overlap. An account blocked by both windows counts once in each total.

For example:

> All 4 accounts considered for this request have exhausted observed usage for model claude-sonnet-4.5. Limits reached: weekly 3 of 4; five-hour 1 of 4 (counts may overlap). Account 1: account-wide weekly limit, reset Oct 16, 2026 2:00 AM PDT. Account 2: account-wide weekly limit, reset Oct 16, 2026 3:00 AM PDT. Account 3: account-wide weekly limit, reset Oct 16, 2026 4:00 AM PDT. Account 4: account-wide five-hour limit, reset Oct 16, 2026 5:00 AM PDT. Earliest known reset: Oct 16, 2026 2:00 AM PDT.

## Reset and scope meanings

Reset times use `America/Los_Angeles`, with the year and applicable PST or PDT abbreviation. Displayed times round up to the next minute; machine reset values retain their existing precision. Each blocking window retains its own reset. When multiple windows block one account, its recovery is the latest blocking reset. If any blocking reset is missing, that account's recovery is unknown.

The earliest known reset is the minimum known account recovery. It is an observed estimate, not a promise of upstream availability. An account with unknown recovery can recover sooner.

Scope labels are `account-wide`, `Sonnet`, `Opus`, `Fable`, or `scope unknown`. Unknown window names display `usage limit (window unknown)`. Arbitrary provider scope and window text never appears in the report. Unrecognized model display text falls back to `the requested model`.

`Account 1` and later labels belong only to this response. They do not identify management records or remain stable across requests. Reports contain no credential IDs, account indices, emails, labels, or filenames.

## Error contracts and limits

When all considered accounts have exhausted observed usage and at least one account has a known recovery, the existing 429 `model_cooldown` response remains. `Retry-After`, `reset_at`, and the unified reset header keep their existing meanings.

When other accounts have ordinary cooldowns, the message separates those cooldowns from observed exhaustion. Their usage windows remain unknown. The ordinary cooldown's existing retry timing, status, and upstream cause remain unchanged, even when shorter than the observed usage resets. The message also states the ordinary cooldown retry duration and labels quota dates as known usage resets.

If every recovery is unknown, the existing unavailable error remains. The report still includes known blocker details. It adds no retry or reset hint. Claude's existing unavailable handling returns 503.

Blocker evidence comes from the same capacity evaluation that excludes an account. This preserves the existing freshness, registration-epoch, model-scope, and passed-reset rules without a second policy scan. The error stores rendered text, so later cache updates cannot change it. Claude retains the full breakdown because it is inside `error.message`.

Selection priorities, reserves, affinity, retries, usage polling, and management schemas are unchanged. No network calls occur while constructing the report.

## Design choice

The private `weeklySelection` carries the eligible accounts and typed blocker evidence from `capacity`. The error stores the rendered report. This keeps selection and error messages tied to the same observation without retaining credential objects.

The design review chose this extension over a broader availability report that would also own reserves and cooldown decisions. A second cache scan for diagnostics was rejected because a refresh could change the evidence between exclusion and reporting. Mixed failures attach the report to a copy of the existing error, preserving its retry contract.
