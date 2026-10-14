#!/bin/sh
# Claude Code through this Mac's CLIProxyAPI (127.0.0.1:8318). Used as T3 Code's Claude binary path.
set -eu
ANTHROPIC_BASE_URL="http://127.0.0.1:8318"
ANTHROPIC_AUTH_TOKEN="$(cat "$HOME/.config/cliproxyapi-custom/client-api-key")"
export ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_API_KEY
# When every account is out of usage, the proxy answers 429 with the first
# account's reset. Unattended retry mode waits for it (up to 6 hours) and
# resends, instead of ending the turn. Other errors keep a bounded retry count.
: "${CLAUDE_CODE_RETRY_WATCHDOG:=1}" "${CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS:=21600000}" "${CLAUDE_CODE_MAX_RETRIES:=15}"
export CLAUDE_CODE_RETRY_WATCHDOG CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS CLAUDE_CODE_MAX_RETRIES
exec "$HOME/.local/bin/claude" "$@"
