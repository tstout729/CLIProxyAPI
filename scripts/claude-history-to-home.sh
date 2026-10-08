#!/bin/sh
# Copy Claude Code conversations from this Mac to the home host so
# `claude --resume` there lists them. Newer copies replace older ones; files
# that exist only on the home host are never touched. Memory folders are left
# to the agent sync.
set -eu
host="${HOME_CODING_SSH_ALIAS:-home}"
rsync -a --update --exclude 'memory' --exclude 'memory/**' \
  "$HOME/.claude/projects/" "$host:.claude/projects/"
printf 'Conversations copied to %s. Run claude --resume there to pick one up.\n' "$host"
