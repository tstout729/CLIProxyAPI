#!/bin/bash
# Install this fork beside any existing CLI Proxy service. No provider credentials are copied.
set -euo pipefail
umask 077

if [[ "$(uname -s)" != Darwin ]]; then
  echo "This installer uses macOS launchd. Build the source directly on other platforms." >&2
  exit 1
fi
for tool in go bun python3 git; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Install $tool before running this installer (Go 1.26+ and Bun 1.3.14 recommended)." >&2
    exit 1
  fi
done

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
console_source="${CONSOLE_SOURCE_DIR:-$repo_root/../Cli-Proxy-API-Management-Center}"
console_source="$(cd "$console_source" && pwd)"
install_dir="$HOME/.config/cliproxyapi-custom"
binary_path="$HOME/.local/bin/cliproxyapi-custom"
plist_path="$HOME/Library/LaunchAgents/io.tstout.cliproxyapi-custom.plist"
service_domain="gui/$(id -u)"
service_pid="$(launchctl print "$service_domain/io.tstout.cliproxyapi-custom" 2>/dev/null | awk '/pid =/{print $3; exit}' || true)"
listener_pids="$(lsof -nP -t -iTCP:8318 -sTCP:LISTEN 2>/dev/null || true)"
for listener_pid in $listener_pids; do
  if [[ "$listener_pid" != "$service_pid" ]]; then
    echo "Port 8318 is already used by another process. Choose an available port before installing." >&2
    exit 1
  fi
done
mkdir -p "$install_dir/static" "$install_dir/auths" "$install_dir/logs" "$(dirname "$binary_path")" "$(dirname "$plist_path")"

source_commit="$(git -C "$repo_root" rev-parse HEAD)"
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then source_commit="$source_commit-dirty"; fi
build_date="$(TZ=America/Los_Angeles date +'%Y-%m-%dT%H:%M:%S%z')"
cd "$repo_root"
go build -ldflags "-X main.Version=8.0.0-custom -X main.Commit=$source_commit -X main.BuildDate=$build_date" -o "$install_dir/cliproxyapi.next" ./cmd/server
cd "$console_source"
bun install --frozen-lockfile
VERSION=custom-weekly-reset bun run build
cp "$console_source/dist/index.html" "$install_dir/static/management.html.next"

CPA_INSTALL_DIR="$install_dir" CPA_BINARY_PATH="$binary_path" CPA_PLIST_PATH="$plist_path" python3 - <<'PY'
import json
import os
from pathlib import Path
import plistlib
import secrets

root = Path(os.environ['CPA_INSTALL_DIR'])
config = root / 'config.yaml'
if not config.exists():
    management = secrets.token_hex(32)
    client = secrets.token_hex(32)
    for name, value in [('management-key', management), ('client-api-key', client)]:
        path = root / name
        path.write_text(value + '\n')
        path.chmod(0o600)
    config.write_text(f'''config-version: 8
server:
  host: "127.0.0.1"
  port: 8318
management:
  allow-remote: false
  secret-key: "{management}"
  disable-auto-update-panel: true
  panel-github-repository: "https://github.com/tstout729/Cli-Proxy-API-Management-Center"
access:
  api-keys:
    - "{client}"
oauth:
  auth-dir: {json.dumps(str(root / 'auths'))}
routing:
  strategy: "weekly-reset-first"
  session-affinity: true
  session-affinity-ttl: "168h"
  session-affinity-subagents: true
upstream:
  codex:
    upstream-websockets: true
observability:
  logs:
    logging-to-file: true
    request-log: false
''')
    config.chmod(0o600)
plist = {
    'Label': 'io.tstout.cliproxyapi-custom',
    'ProgramArguments': [os.environ['CPA_BINARY_PATH'], '--config', str(config)],
    'WorkingDirectory': str(root),
    'EnvironmentVariables': {'TZ': 'America/Los_Angeles', 'MANAGEMENT_STATIC_PATH': str(root / 'static')},
    'RunAtLoad': True,
    'KeepAlive': True,
    'StandardOutPath': str(root / 'logs' / 'service.log'),
    'StandardErrorPath': str(root / 'logs' / 'service-error.log'),
}
path = Path(os.environ['CPA_PLIST_PATH'])
with path.open('wb') as output:
    plistlib.dump(plist, output)
path.chmod(0o600)
PY

if [[ -f "$binary_path" ]]; then cp "$binary_path" "$binary_path.previous"; fi
mv "$install_dir/cliproxyapi.next" "$binary_path"
chmod 755 "$binary_path"
mv "$install_dir/static/management.html.next" "$install_dir/static/management.html"
launchctl bootout "$service_domain" "$plist_path" >/dev/null 2>&1 || true
launchctl bootstrap "$service_domain" "$plist_path"
launchctl kickstart "$service_domain/io.tstout.cliproxyapi-custom"

echo "Installed custom CLI Proxy at http://127.0.0.1:8318/management.html#/quota"
echo "Management key: $install_dir/management-key"
echo "Inference API key: $install_dir/client-api-key"
echo "Configuration: $install_dir/config.yaml"
echo "Sign in to providers through OAuth Login in the console."
