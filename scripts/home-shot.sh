#!/bin/sh
# Send a screenshot to the home host so an agent running there can read it.
# Uses the image on the clipboard, else the newest Desktop screenshot, or the
# file given as $1. Copies the image's path on the home host to the clipboard,
# ready to paste into a home-host Claude or Codex session.
set -eu

host="${HOME_CODING_SSH_ALIAS:-home}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
name="shot-$(TZ=America/Los_Angeles date +%Y%m%d-%H%M%S).png"

if [ $# -gt 0 ]; then
  source=$1
  case "$source" in
    *.*) name="shot-$(TZ=America/Los_Angeles date +%Y%m%d-%H%M%S).${source##*.}" ;;
  esac
else
  source="$work/$name"
  if ! osascript \
      -e "set out to open for access (POSIX file \"$source\") with write permission" \
      -e 'try' \
      -e 'write (the clipboard as «class PNGf») to out' \
      -e 'end try' \
      -e 'close access out' >/dev/null 2>&1 || [ ! -s "$source" ]; then
    source=$(ls -t "$HOME"/Desktop/Screenshot* 2>/dev/null | head -n 1 || true)
  fi
fi

if [ -z "$source" ] || [ ! -s "$source" ]; then
  printf 'No image on the clipboard or Desktop. Usage: shot [file]\n' >&2
  exit 1
fi

remote_dir=$(ssh "$host" 'mkdir -p "$HOME/Downloads/shots" && cd "$HOME/Downloads/shots" && pwd')
scp -q "$source" "$host:$remote_dir/$name"
printf '%s' "$remote_dir/$name" | pbcopy
printf 'Sent to %s: %s (path copied; paste it into the agent)\n' "$host" "$remote_dir/$name"
