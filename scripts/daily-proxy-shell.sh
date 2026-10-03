# Source this file from the daily Mac's interactive shell configuration.
# The existing M1 launchers scope gateway settings to their child processes.

codex() {
  "$HOME/.local/bin/codex-m1" "$@"
}

claude() {
  "$HOME/.local/bin/claude-m1" "$@"
}

# `command` bypasses these functions and uses the original installed clients.
codex-m5() {
  command codex "$@"
}

claude-m5() {
  command claude "$@"
}
