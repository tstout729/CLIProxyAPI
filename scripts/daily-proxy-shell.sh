# Source this file from the daily Mac's interactive shell configuration.
# The proxy launchers scope gateway settings to their child processes and fall
# back from the M1 to this Mac's proxy. A positive integer as the first
# argument selects a persistent M1 session.
#
# Typed interactively, plain `claude` and `codex` open a new persistent session
# on the home host in the current project, so the agent and its files live
# there. The session survives a dropped connection and closes when the agent
# exits. Scripted and maintenance calls (print mode, subcommands, no
# terminal) stay on this Mac. CLIPROXY_HOME_DEFAULT=0 keeps everything local;
# `claude-local` and `codex-local` run one session here.

# Succeeds when this call should run on the home host. $1 is the agent.
_cliproxy_home_route() {
  _cliproxy_agent=$1
  shift
  [ "${CLIPROXY_HOME_DEFAULT:-1}" = 0 ] && return 1
  if [ "${CLIPROXY_ASSUME_TTY:-}" != 1 ]; then
    [ -t 0 ] && [ -t 1 ] || return 1
  fi
  for _cliproxy_arg in "$@"; do
    case "$_cliproxy_arg" in
      -p|--print|-h|--help|-v|--version|--output-format|--output-format=*|--input-format|--input-format=*) return 1 ;;
    esac
  done
  case "${1-}" in
    ''|-*) return 0 ;;
  esac
  # Subcommands manage this Mac's install or run non-interactively.
  if [ "$_cliproxy_agent" = claude ]; then
    case "$1" in
      mcp|config|doctor|update|upgrade|install|migrate-installer|setup-token|auth|plugin|plugins|agents|remote-control|rc) return 1 ;;
    esac
  else
    case "$1" in
      exec|e|login|logout|mcp|mcp-server|app-server|proto|completion|debug|apply|a|cloud|sandbox|features|help|generate-ts|responses-api-proxy|stdio-to-uds) return 1 ;;
    esac
  fi
  return 0
}

# Opens a new home session for the current project: the repository's main
# checkout under $HOME (worktrees map to their repository), otherwise $HOME.
_cliproxy_home_open() {
  _cliproxy_agent=$1
  shift
  _cliproxy_dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || _cliproxy_dir=
  case "$_cliproxy_dir" in
    */.git) _cliproxy_dir=${_cliproxy_dir%/.git} ;;
    *) _cliproxy_dir=$(git rev-parse --show-toplevel 2>/dev/null) || _cliproxy_dir= ;;
  esac
  case "$_cliproxy_dir" in
    "$HOME"/?*) _cliproxy_name=$(basename "$_cliproxy_dir" | LC_ALL=C tr -c 'A-Za-z0-9_\n-' '-' | cut -c1-40) ;;
    *) _cliproxy_dir=$HOME _cliproxy_name=home ;;
  esac
  # Each call gets its own session; the name lets a dropped connection rejoin it.
  _cliproxy_name="$_cliproxy_name-$(TZ=America/Los_Angeles date +%m%d-%H%M%S)"
  "$HOME/.local/bin/$_cliproxy_agent-home" --fresh "$_cliproxy_name" "$_cliproxy_dir" -- "$@"
}

_cliproxy_define_functions() {
  codex() {
    case "${1-}" in
      ''|*[!0-9]*|0*) ;;
      *) "$HOME/.local/bin/codex-home" "$@"; return ;;
    esac
    if _cliproxy_home_route codex "$@"; then
      _cliproxy_home_open codex "$@"
    else
      "$HOME/.local/bin/codex-m1" "$@"
    fi
  }

  claude() {
    case "${1-}" in
      ''|*[!0-9]*|0*) ;;
      *) "$HOME/.local/bin/claude-home" "$@"; return ;;
    esac
    if _cliproxy_home_route claude "$@"; then
      _cliproxy_home_open claude "$@"
    else
      "$HOME/.local/bin/claude-m1" "$@"
    fi
  }
}
_cliproxy_define_functions

# Run one session on this Mac through the proxy, skipping the home host.
claude-local() {
  "$HOME/.local/bin/claude-m1" "$@"
}

codex-local() {
  "$HOME/.local/bin/codex-m1" "$@"
}

# Terminal apps such as cmux install their own `claude` function after startup
# files run, which skipped the proxy. Reclaim the name before each prompt. The
# launcher's `exec claude` still reaches the app's PATH shim, so its hooks work.
if [ -n "${ZSH_VERSION-}" ]; then
  _cliproxy_reclaim() {
    [[ "${functions[claude]-}" == *claude-m1* ]] || _cliproxy_define_functions
  }
  # preexec covers the first command, which runs before a precmd-ordered fix.
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _cliproxy_reclaim
  add-zsh-hook preexec _cliproxy_reclaim
elif [ -n "${BASH_VERSION-}" ]; then
  _cliproxy_reclaim() {
    case "$(declare -f claude 2>/dev/null)" in
      *claude-m1*) ;;
      *) _cliproxy_define_functions ;;
    esac
  }
  case ";${PROMPT_COMMAND-};" in
    *";_cliproxy_reclaim;"*) ;;
    *) PROMPT_COMMAND="_cliproxy_reclaim${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
  esac
fi

# `command` bypasses these functions and uses the original installed clients.
codex-m5() {
  command codex "$@"
}

# Inside cmux, `command claude` reaches cmux's wrapper and then claude-proxy,
# which honors the direct route.
claude-m5() {
  CLIPROXY_ROUTE=direct command claude "$@"
}
