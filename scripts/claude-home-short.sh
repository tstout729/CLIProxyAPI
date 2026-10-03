#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then
  set -- 1
fi
exec "$HOME/.local/bin/claude-home" "$@"
