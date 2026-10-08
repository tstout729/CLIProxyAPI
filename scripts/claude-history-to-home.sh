#!/bin/sh
# Copy this Mac's agent work to the home host, where the business brain
# captures it and `claude --resume` lists it: Claude Code conversations, the
# last few days of Codex sessions, and ~/docs. Newer copies replace older ones;
# files that exist only on the home host are never touched. Memory folders are
# left to the agent sync.
set -eu
host="${HOME_CODING_SSH_ALIAS:-home}"
codex_days="${HOME_SYNC_CODEX_DAYS:-3}"

rsync -a --update --exclude 'memory' --exclude 'memory/**' \
  "$HOME/.claude/projects/" "$host:.claude/projects/"

if [ -d "$HOME/.codex/sessions" ]; then
  list=$(mktemp)
  trap 'rm -f "$list"' EXIT
  (cd "$HOME/.codex/sessions" && find . -type f -name '*.jsonl' -mtime "-$codex_days") > "$list"
  rsync -a --update --files-from="$list" "$HOME/.codex/sessions/" "$host:.codex/sessions/"
fi

if [ -d "$HOME/docs" ]; then
  rsync -a --update "$HOME/docs/" "$host:docs/"
fi

printf '%s Synced Claude, recent Codex sessions and ~/docs to %s.\n' \
  "$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M %Z')" "$host"
