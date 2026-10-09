#!/bin/sh
# Terminal command: every window and tab is a shell on the home host (M1),
# over mosh (instant typing, survives Wi-Fi changes and sleep) inside tmux
# (the session keeps running when the tab closes). If the host is
# unreachable, the tab falls back to a local shell and says so. Dropped files
# reach the host through m1-drop-bridge.
PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
export LANG="${LANG:-en_US.UTF-8}"
# Local echo only when the link is slow (mosh's default): on a fast link, guessed
# keystrokes that an agent's screen then redraws differently only flicker.
export MOSH_PREDICTION_DISPLAY=adaptive
# Window titles are the tab's own title, without mosh's "[mosh]" prefix.
export MOSH_TITLE_NOPREFIX=1
# mosh checks the terminal type locally; use a standard one if this Mac lacks its description.
infocmp "${TERM:-dumb}" >/dev/null 2>&1 || export TERM=xterm-256color
set -- mosh --server="env LANG=en_US.UTF-8 /opt/homebrew/bin/mosh-server" home -- "/Users/trevorstout/.local/bin/m1-tab"
# Files dragged into the window are uploaded to the host and pasted as its paths.
bridge="$HOME/.local/bin/m1-drop-bridge"
if [ -x /opt/homebrew/bin/python3 ] && [ -f "$bridge" ]; then
  /opt/homebrew/bin/python3 -I "$bridge" -- "$@"
else
  "$@"
fi
status=$?
[ "$status" -eq 0 ] && exit 0
printf "\n[home host unreachable (mosh exit %s): this tab is a local shell on this Mac]\n" "$status"
exec /bin/zsh -l
