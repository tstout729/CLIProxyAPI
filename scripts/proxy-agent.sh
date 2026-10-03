#!/bin/sh
# Launch Claude Code or Codex through CLIProxyAPI, preferring the M1 gateway.
#
# Install this file as ~/.local/bin/claude-m1 and ~/.local/bin/codex-m1; the
# installed name selects the agent. Each launch probes the M1 tunnel first and
# falls back to the identical proxy on this Mac when the M1 has no usable
# models. CLIPROXY_ROUTE=m1 or CLIPROXY_ROUTE=local forces one route, and
# CLIPROXY_ROUTE=direct skips the proxy.
#
# Also install it as ~/.local/bin/claude-proxy and set cmux's Claude Binary
# Path to that file. cmux then starts and auto-resumes sessions through the
# proxy; under that name the script runs the real binary instead of looking
# `claude` up in PATH, which would loop back into cmux's wrapper.
set -eu

config_dir="${CLIPROXY_CONFIG_DIR:-$HOME/.config/cliproxyapi-custom}"
m1_url="${CLIPROXY_M1_URL:-http://127.0.0.1:18318}"
local_url="${CLIPROXY_LOCAL_URL:-http://127.0.0.1:8318}"
probe_timeout="${CLIPROXY_PROBE_TIMEOUT:-3}"

claude_exec=claude
case "$(basename "$0")" in
  claude-proxy) agent=claude; claude_exec="${CLIPROXY_CLAUDE_BIN:-$HOME/.local/bin/claude}" ;;
  claude*) agent=claude ;;
  codex*) agent=codex ;;
  *) agent="${CLIPROXY_AGENT:-}" ;;
esac
if [ -z "$agent" ]; then
  printf 'proxy-agent: install as claude-m1 or codex-m1\n' >&2
  exit 2
fi

# Succeeds when the proxy at $1 accepts the key in $2 and serves at least one model.
proxy_ready() {
  [ -r "$2" ] || return 1
  printf 'Authorization: Bearer %s\n' "$(cat "$2")" |
    curl -fsS -m "$probe_timeout" -H @- "$1/v1/models" 2>/dev/null |
    grep -q '"id"'
}

use_m1() { base_url="$m1_url"; key_file="$config_dir/m1-client-api-key"; }
use_local() { base_url="$local_url"; key_file="$config_dir/client-api-key"; }

if [ "${CLIPROXY_ROUTE:-}" = direct ]; then
  unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLIPROXY_SELECTED
  [ "$agent" = claude ] && exec "$claude_exec" "$@"
  exec codex "$@"
fi

# An outer launcher already chose the route; cmux's wrapper re-enters here.
if [ -n "${CLIPROXY_SELECTED:-}" ] && [ -n "${ANTHROPIC_BASE_URL:-}" ] && [ "$agent" = claude ]; then
  exec "$claude_exec" "$@"
fi

case "${CLIPROXY_ROUTE:-auto}" in
  m1) use_m1 ;;
  local) use_local ;;
  *)
    use_m1
    if ! proxy_ready "$base_url" "$key_file"; then
      use_local
      if proxy_ready "$base_url" "$key_file"; then
        printf '%s: M1 proxy unreachable; using the local proxy on this Mac.\n' "$agent" >&2
      else
        printf '%s: neither the M1 proxy (%s) nor the local proxy (%s) has usable models.\n' "$agent" "$m1_url" "$local_url" >&2
        printf 'Sign in locally with cliproxy-local-login, or run %s-m5 for the direct client.\n' "$agent" >&2
        exit 1
      fi
    fi
    ;;
esac

if [ ! -r "$key_file" ]; then
  printf 'Proxy inference key is missing: %s\n' "$key_file" >&2
  exit 1
fi

if [ "$agent" = claude ]; then
  ANTHROPIC_BASE_URL="$base_url"
  ANTHROPIC_AUTH_TOKEN="$(cat "$key_file")"
  CLIPROXY_SELECTED=1
  export ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN CLIPROXY_SELECTED
  unset ANTHROPIC_API_KEY
  exec "$claude_exec" "$@"
fi

CLIPROXY_API_KEY="$(cat "$key_file")"
export CLIPROXY_API_KEY
exec codex \
  -c 'model_provider="cliproxy_m1"' \
  -c 'model_providers.cliproxy_m1.name="CLI Proxy"' \
  -c "model_providers.cliproxy_m1.base_url=\"$base_url/v1\"" \
  -c 'model_providers.cliproxy_m1.env_key="CLIPROXY_API_KEY"' \
  -c 'model_providers.cliproxy_m1.wire_api="responses"' \
  -c 'model_providers.cliproxy_m1.requires_openai_auth=false' \
  -c 'model_providers.cliproxy_m1.supports_websockets=true' \
  "$@"
