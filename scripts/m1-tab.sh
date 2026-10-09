#!/bin/sh
# One terminal window = one view of the home host's tab list. Tabs are tmux
# windows in the session group "main" on a dedicated server; m1-sidebar draws
# the list on the left of every tab. Each window gets its own grouped session,
# so windows can show different tabs. Tabs keep running when a window closes;
# the next window shows them again. A window opened while another one is
# showing the list starts on a new tab instead of mirroring that window's tab.
PATH="/opt/homebrew/bin:$PATH"
# A dedicated tmux server, so tab shells never inherit another tool's setup (cmux's).
unset ZDOTDIR
socket="${M1_TABS_SOCKET:-tabs}"
sidebar="${M1_SIDEBAR:-$HOME/.local/bin/m1-sidebar}"
conf="${M1_TABS_CONF:-$HOME/.config/m1-tabs/tmux.conf}"
python="${M1_PYTHON:-/opt/homebrew/bin/python3}"
tm() { tmux -L "$socket" "$@"; }

# Two windows opening at once may both try to create the group; one wins.
tm has-session -t =main 2>/dev/null || tm new-session -d -s main -c "$HOME" 2>/dev/null
tm set -g @m1_sidebar_cmd "$python -I $sidebar" \; source-file "$conf"
# Adopt tabs from the old one-session-per-tab layout and add missing sidebars.
M1_TABS_SOCKET="$socket" "$python" -I "$sidebar" ensure

view="w-$(date +%m%d-%H%M%S)-$$"
if tm list-sessions -F '#{session_group} #{session_attached}' | grep -q '^main [1-9]'; then
  tab=$(M1_TABS_SOCKET="$socket" "$python" -I "$sidebar" new-tab)
  [ -n "$tab" ] && exec tmux -L "$socket" new-session -t main -s "$view" \; set destroy-unattached on \; select-window -t "=$view:$tab"
fi
last=$(tm show -gqv @m1_last_tab)
if [ -n "$last" ] && tm list-windows -t =main -F '#{window_id}' | grep -qx "$last"; then
  exec tmux -L "$socket" new-session -t main -s "$view" \; set destroy-unattached on \; select-window -t "=$view:$last"
fi
exec tmux -L "$socket" new-session -t main -s "$view" \; set destroy-unattached on
