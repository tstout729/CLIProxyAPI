#!/bin/sh
# One terminal tab = one tmux session on the home host. A new tab reattaches
# the oldest tab session nobody is viewing (after the laptop restarts or the
# terminal relaunches), otherwise it starts a new session.
PATH="/opt/homebrew/bin:$PATH"
# A dedicated tmux server, so tab shells never inherit another tool's setup (cmux's).
unset ZDOTDIR
tm() { tmux -L tabs "$@"; }
lock="${TMPDIR:-/tmp}/m1-tab.lock.$(id -u)"
tries=0
while ! mkdir "$lock" 2>/dev/null; do
  tries=$((tries + 1)); [ "$tries" -gt 50 ] && break; sleep 0.1
done
# Release the lock once tmux has registered this tab as attached.
(sleep 1; rmdir "$lock" 2>/dev/null) &
session=$(tm list-sessions -F '#{session_attached} #{session_created} #{session_name}' 2>/dev/null |
  awk '$1 == 0 && $3 ~ /^t-/' | sort -k2n | head -1 | cut -d' ' -f3)
if [ -n "$session" ]; then
  exec tmux -L tabs attach-session -t "=$session"
fi
exec tmux -L tabs new-session -s "t-$(date +%m%d-%H%M%S)-$$" -c "$HOME"
