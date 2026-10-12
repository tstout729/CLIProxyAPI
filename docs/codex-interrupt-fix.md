# Codex response interruption

## Acceptance criteria

- Forward native `response.interrupt` during generation without waiting for completion.
- Preserve its bytes, response ID and mode on the same upstream socket and account.
- Support the default configuration and optional steering mode independently.
- Relay `response.incomplete` with reason `interrupted` as a normal terminal event.
- Keep discarded text and tool calls out of the completion and transcript-repair caches.
- Ignore late controls when a successor uses a different transport.
- Reuse the connection for the next response without replaying the interrupted request.
- Handle completion races, disabled accounts and disconnects without leaking goroutines.
- Preserve existing provider routing, bootstrap recovery and steering behavior.

## Protocol evidence

The installed Codex CLI is `0.162.1`. Its matching official source sends
`{"type":"response.interrupt","response_id":"…","mode":"discard_partial_items"}`.
It drains the response until an interrupted terminal event, then continues.
There is no separate acknowledgement to manufacture.

- [Native WebSocket send and drain](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/codex-api/src/endpoint/responses_websocket.rs#L687)
- [Interrupted terminal event](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/codex-api/src/sse/responses.rs#L418)
- [Mid-turn preemption](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/session/turn.rs#L2624)

## Implementation

The downstream socket has one reader and bounded queues for requests and interrupts.
The forwarder matches the interrupt to the visible active response ID.
The executor binds forwarding to the exact authenticated upstream socket.
Default and steering modes use the same interrupt registration without changing routing.

Interrupted output remains authoritative, including an empty output array.
The proxy does not restore discarded partial items or cache discarded tool calls.
Earlier input calls and calls retained by the terminal event remain available.
A matching interrupt on HTTP cancels the request and closes with code `1012`.
It never retries a control frame on another account.

## Verification

Verified October 11, 2026 (Pacific), with Go `1.26.4`:

- Real handler/auth-manager/executor integration tests cover steering on/off,
  bootstrap buffering on/off, zero/partial output, exact control bytes,
  late/stale controls, same-socket next responses, and disabled accounts.
- HTTP integration tests cover both steering modes, stale controls during a
  successor, request cancellation, close `1012`, and no control replay.
- Forwarder regressions cover control-write failure and discarded function/custom
  calls in both staged and passthrough caches. Subsequent repair cannot resurrect them.
- Affected package suites and targeted interruption/steering/duplex race checks pass.
- Server compilation and `git diff --check` pass.
- The repository suite passes with only `TestAdvertiserAndBrowser_Integration` excluded.
  Its multicast discovery failure also reproduces on the unchanged base commit.
  The initial unfiltered suite therefore does not pass in this environment.

Commands:

```sh
go test -count=1 -skip '^TestAdvertiserAndBrowser_Integration$' ./...
go test -race -count=1 -run 'Interrupt|Steering|Duplex|CodexTerminalEmptyIncomplete' ./sdk/api/handlers/openai ./sdk/cliproxy/executor ./internal/runtime/executor ./internal/runtime/executor/helps
go build -o /private/tmp/cliproxy-interrupt-check ./cmd/server
```

## Delivery

- Base: integration commit `36bbb0a535f39ad4a44bbc565dd61e3bf84b6a1c`, matching the installed proxy.
- Feature branch: `fix/codex-response-interrupt-20261011`.
- Integration lane: `integration`, using `release/codex-response-interrupt-20261011`
  for serial combined validation. Git history records the exact source commits.
- Production installation: requires separate deployment authorization.

The prepared binary must be installed and the custom launchd service restarted
before live clients receive the fix. Local test results do not establish deployment.

No schema or credential changes are required.
