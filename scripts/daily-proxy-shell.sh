# Source this file from the daily Mac's interactive shell configuration.
# The existing M1 launchers scope gateway settings to their child processes.
# A positive integer as the first argument selects a persistent M1 session.

codex() {
  case "${1-}" in
    ''|*[!0-9]*|0*) "$HOME/.local/bin/codex-m1" "$@" ;;
    *) "$HOME/.local/bin/codex-home" "$@" ;;
  esac
}

claude() {
  case "${1-}" in
    ''|*[!0-9]*|0*) "$HOME/.local/bin/claude-m1" "$@" ;;
    *) "$HOME/.local/bin/claude-home" "$@" ;;
  esac
}

# `command` bypasses these functions and uses the original installed clients.
codex-m5() {
  command codex "$@"
}

claude-m5() {
  command claude "$@"
}
