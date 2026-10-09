#!/bin/sh
# Terminal command: every window and tab is a shell on the home host (M1),
# over mosh (instant typing, survives Wi-Fi changes and sleep) inside tmux
# (the session keeps running when the tab closes). If the host is
# unreachable, the tab falls back to a local shell and says so.
PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
export LANG="${LANG:-en_US.UTF-8}"
export MOSH_PREDICTION_DISPLAY=always
# mosh checks the terminal type locally; use a standard one if this Mac lacks its description.
infocmp "${TERM:-dumb}" >/dev/null 2>&1 || export TERM=xterm-256color
mosh --server="env LANG=en_US.UTF-8 /opt/homebrew/bin/mosh-server" home -- "/Users/trevorstout/.local/bin/m1-tab"
status=$?
[ "$status" -eq 0 ] && exit 0
printf "\n[home host unreachable (mosh exit %s): this tab is a local shell on this Mac]\n" "$status"
exec /bin/zsh -l
