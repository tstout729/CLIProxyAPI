# Home host, interactive shells: `claude` and `codex` (including cmux's
# wrappers in remote workspaces) resolve to launchers that use this machine's
# CLIProxyAPI. The inference key only enters the agent's own environment.
case ":$PATH:" in *":$HOME/.config/cliproxyapi-custom/shims:"*) ;; *) export PATH="$HOME/.config/cliproxyapi-custom/shims:$PATH" ;; esac
