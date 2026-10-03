# Optional coding sessions on the home host

In the daily Mac's interactive Terminal, `codex` and `claude` now use the existing `codex-m1` and `claude-m1` launchers. They execute on the daily Mac and send inference to the M1 gateway. `codex-m5` and `claude-m5` use the original direct clients. Sleeping the daily Mac pauses these local jobs. The separate `codex-home` and `claude-home` commands execute the entire coding session on the M1, inside tmux. The daily Mac is just its terminal connection.

```sh
# On the daily Mac, open M1 session 1:
cx
cl

# Open independent numbered sessions in separate Terminal windows:
cx 2
cx 3
cl 2

# Give the agent a task, then close the daily Mac whenever you need to.
# No detach shortcut is required. Repeat the same command to reconnect.

# On the daily Mac, start an independent session in a prepared M1 checkout:
cx 2 /Users/trevorstout/projects/my-prepared-worktree
cl 2 /Users/trevorstout/projects/another-prepared-worktree

# Without a project argument, a new session gets its own scratch directory.
cx 4

# Optionally detach with Ctrl-B followed by D, or just close the daily Mac.
# Reconnect to the exact same running session:
cx 2

# List remote sessions (either launcher shows both agents):
cx list
```

`cx` means Codex on the home M1; `cl` means Claude on the home M1. Bare shortcuts open session 1. Repeat the same number to reconnect. Codex and Claude have separate numbers: `cx 1` and `cl 1` are independent sessions. `cx list` or `cl list` shows both agents, their session numbers/names, process state, connection count, and folders.

Session 1 reuses the existing `main` session when present. It does not rename, restart, or move that live session. The older `codex-home`, `claude-home`, and descriptive session names remain available. If separate `main` and `1` sessions already exist, both are preserved and the list shows `main` separately.

If a host restart removes tmux sessions, session 1 keeps an existing legacy scratch folder when its numbered folder does not exist. Project files are preserved; saved chats still require the agent's resume menu.

A new session starts in its own M1 scratch folder unless you provide a prepared project path. A live session is reused without restarting or resending your prompt. If you explicitly quit a numbered agent, the same command starts a fresh agent. Use its normal resume menu for saved conversations. Descriptive names retain exited output for inspection.

The M1 must remain plugged in, awake, online, and logged in. Leave its lid open; the screen may lock or turn off. A small per-agent caffeinate process prevents idle sleep while an agent runs, but does not prevent lid-close sleep or survive a reboot. tmux preserves a session across SSH disconnects, not host shutdowns. A task can still pause for a question, permission, quota, or tool failure. Detaching does not create a goal or make an interactive agent work indefinitely.

The M1 runs builds, tests, and commands against its own files. A repository on the daily Mac is not automatically available there. Before real project work, fetch the desired branch on the M1 and create an isolated worktree; migrated checkouts may be stale. Use Git to transfer reviewed commits between machines. Do not copy credential folders, customer records, or an entire dirty workspace, and do not let two sessions edit the same worktree. The launchers do not pull, reset, synchronize, or rewrite project files.

Use positive session numbers starting at 1. Descriptive names can still contain letters, numbers, hyphens, and underscores. Repeat the same agent and number/name to reattach. A conflicting project directory is rejected. Arguments after `--` launch a new agent; when reconnecting to a live session, they are not submitted again:

```sh
codex-home investigation my-prepared-worktree -- -m gpt-6.1-sol
claude-home investigation my-prepared-worktree -- --model claude-haiku-4-5-20251001
codex-home --detach background my-prepared-worktree -- "Investigate the test failures and implement a fix."
```

Descriptive names retain exited terminal output for inspection. Numbered sessions and the legacy default restart an exited agent when you reconnect. To resume a saved conversation after an exit or host reboot, launch a fresh session in the same project with `-- resume --last` for Codex, or `-- --continue` for Claude. This resumes the agent's saved transcript; it cannot restore a terminated process. An existing live local-Mac chat is not transferred by these commands.

## Installation and authentication

### Daily Mac command defaults

| Terminal command | Inference route | Coding host |
| --- | --- | --- |
| `codex` / `claude` | M1 proxy | Daily M5 |
| `codex-m5` / `claude-m5` | Original direct client | Daily M5 |
| `codex-home` / `claude-home` | M1 proxy | Home M1 |
| `cx [NUMBER]` / `cl [NUMBER]` | M1 proxy | Home M1 |

Install `scripts/daily-proxy-shell.sh` at `~/.config/cliproxyapi-custom/daily-proxy-shell.sh` on the daily Mac. Source it at the end of `~/.zshrc`, `~/.bashrc`, and `~/.bash_profile`:

```sh
if [ -r "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh" ]; then
  . "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh"
fi
```

Open a new Terminal window after installation. To update an existing zsh window, run `source ~/.zshrc` once.

These functions call the existing M1 scripts. The backup functions use `command` to bypass the defaults and launch the original native binaries. Proxy settings remain in child processes. Native configuration, sign-ins, and managed binary links remain in place. Proxy errors remain visible; choose the direct backup explicitly.

Existing `claudex` and `claudek` functions must call `command claude` for their configured provider routes. Otherwise the new `claude` function would select the M1 proxy for those older routes. The daily Mac's two existing calls were updated accordingly.

This changes interactive Terminal commands. It does not change Codex desktop settings or the M1's command defaults. [Official OpenAI documentation](https://learn.chatgpt.com/docs/config-file/config-advanced) describes how per-invocation provider settings remain separate from stored client configuration.

### Home-session helper

Install `scripts/codex-home-short.sh` as `~/.local/bin/cx` and `scripts/claude-home-short.sh` as `~/.local/bin/cl` on each Mac. Set both executable. They call that Mac's existing full home launchers and default to number 1. Their executable paths avoid the daily Mac's plain `codex` and `claude` shell defaults.

`scripts/home-coding-session.py` has three modes:

- `client codex|claude`: use SSH to create or attach on the host.
- `host codex|claude`: manage the host's dedicated `cliproxy-home` tmux socket.
- `run codex|claude -- ARGS`: execute the agent against the host gateway.

Install the script as `~/.local/bin/home-coding-session` on both machines. The daily-Mac wrapper selects `client` mode, sets `HOME_CODING_SSH_HOST`, and optionally `HOME_CODING_SSH_KEY`. Set `HOME_CODING_REMOTE_SCRIPT` for another installed host path. Set `HOME_CODING_REMOTE_PYTHON` to the installed interpreter when the non-interactive SSH path cannot find it. These Macs use `/opt/homebrew/bin/python3`; macOS's `/usr/bin/python3` can depend on Xcode selection/license state. `CPA_CODEX_MODEL` can select the default Codex model; normal agent model flags override it.

The host needs Python 3, tmux, Codex, and Claude Code. It reads the inference key at runtime from `~/.config/cliproxyapi-custom/client-api-key`, requiring private permissions, and connects to `http://127.0.0.1:8318`. `CPA_PROXY_KEY_FILE` and `CPA_PROXY_URL` customize those host-side paths. The proxy key stays in the agent environment, never the SSH command or tmux arguments. No provider OAuth files or OpenAI API key are copied.

The dedicated tmux socket preserves existing sessions and configuration. Its private `home-tmux.conf` inherits the existing theme, keeps exited panes visible, and labels its status bar `HOME HOST`. Dates and times use `America/Los_Angeles`.

For phone access, the same session can be reached with a private SSH connection while the phone is on Tailscale. Native app remote interfaces are separate: Claude Remote Control excludes custom gateway authentication and custom base URLs; Codex desktop supports SSH projects, but phone pairing and gateway compatibility need their own setup and verification.

References: [tmux persistence](https://github.com/tmux/tmux/wiki/Getting-Started#attaching-and-detaching), [Claude Remote Control](https://code.claude.com/docs/en/remote-control), [Codex remote connections](https://learn.chatgpt.com/docs/remote-connections).
