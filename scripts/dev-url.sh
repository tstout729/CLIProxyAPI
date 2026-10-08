#!/bin/sh
# Print the link Trevor opens on his laptop for a dev server on this machine:
# the machine's Tailscale name and the given port. Usage: dev-url 3003 [/path]
set -eu
port=${1:?usage: dev-url PORT [/path]}
path=${2:-/}
tailscale=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
name=$("$tailscale" status --json 2>/dev/null |
  /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null) || name=
printf 'http://%s:%s%s\n' "${name:-localhost}" "$port" "$path"
