# Home host, interactive shells: `claude` and `codex` (including cmux's
# wrappers) resolve to launchers that use this machine's CLIProxyAPI. The
# inference key only enters the agent's own environment. Sourced last from
# ~/.zshrc and always moved to the front, since an inherited PATH may already
# contain it behind ~/.local/bin.
_cliproxy_shims="$HOME/.config/cliproxyapi-custom/shims"
path=("$_cliproxy_shims" ${path:#$_cliproxy_shims})
unset _cliproxy_shims
