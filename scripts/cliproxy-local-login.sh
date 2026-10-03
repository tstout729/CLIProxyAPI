#!/bin/sh
# Sign provider accounts into the fallback proxy on this Mac.
#
# Each login opens the provider's OAuth page in the browser and saves the
# result in this Mac's auth directory; the running local service picks it up
# automatically. Sign in with the same accounts the M1 serves. With no
# argument, this walks through every login the M1 uses.
#
#   cliproxy-local-login            # all M1 logins, one after another
#   cliproxy-local-login claude     # one more Claude account
#   cliproxy-local-login codex      # one more Codex account
#   cliproxy-local-login status     # compare M1 and local accounts
set -eu

config_dir="${CLIPROXY_CONFIG_DIR:-$HOME/.config/cliproxyapi-custom}"
config="$config_dir/config.yaml"
binary="${CLIPROXY_BINARY:-$HOME/.local/bin/cliproxyapi-custom}"
m1_url="${CLIPROXY_M1_URL:-http://127.0.0.1:18318}"
local_url="${CLIPROXY_LOCAL_URL:-http://127.0.0.1:8318}"

# Prints "provider email" for each account the proxy at $1 holds, using the management key in $2.
accounts() {
  [ -r "$2" ] || return 0
  printf 'Authorization: Bearer %s\n' "$(cat "$2")" |
    curl -fsS -m 5 -H @- "$1/v0/management/auth-files" 2>/dev/null |
    /usr/bin/python3 -c 'import json, sys
for f in json.load(sys.stdin).get("files", []):
    print(f.get("provider") or f.get("type"), f.get("email") or f.get("account") or "")' |
    sort || true
}

login() {
  printf '\n== %s login: sign in as %s ==\n' "$1" "${2:-the account you want}"
  "$binary" --config "$config" "-$1-login"
}

show_status() {
  printf 'M1 accounts:\n'
  accounts "$m1_url" "$config_dir/m1-management-key" | sed 's/^/  /'
  printf 'Local accounts:\n'
  accounts "$local_url" "$config_dir/management-key" | sed 's/^/  /'
}

case "${1:-all}" in
  claude|codex) login "$1" ;;
  status) show_status ;;
  all)
    m1_accounts="$(accounts "$m1_url" "$config_dir/m1-management-key")"
    if [ -z "$m1_accounts" ]; then
      printf 'Could not read the M1 account list; use: cliproxy-local-login claude|codex\n' >&2
      exit 1
    fi
    local_accounts="$(accounts "$local_url" "$config_dir/management-key")"
    printf '%s\n' "$m1_accounts" | while read -r provider email; do
      if printf '%s\n' "$local_accounts" | grep -qx "$provider $email"; then
        printf 'Already signed in locally: %s %s\n' "$provider" "$email"
      else
        login "$provider" "$email" </dev/tty
      fi
    done
    show_status
    ;;
  *)
    printf 'usage: cliproxy-local-login [all|claude|codex|status]\n' >&2
    exit 2
    ;;
esac
