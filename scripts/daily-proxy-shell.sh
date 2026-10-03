# Source this file from the daily Mac's interactive shell configuration.
# The proxy launchers scope gateway settings to their child processes and fall
# back from the M1 to this Mac's proxy. A positive integer as the first
# argument selects a persistent M1 session.

_cliproxy_define_functions() {
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
}
_cliproxy_define_functions

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
