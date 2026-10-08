# Optional coding sessions on the home host

In the daily Mac's interactive Terminal, bare `codex` and `claude` execute on the daily M5 and use the M1 gateway. Normal flags, subcommands, and text prompts keep that route. Sleeping the daily Mac pauses these local jobs. Add a positive session number: `codex 1` or `claude 1`. The entire numbered session runs on the M1, inside tmux. The daily Mac is just its terminal connection. `codex-m5` and `claude-m5` use the original direct clients on the daily Mac.

```sh
# On the daily Mac, open M1 session 1:
codex 1
claude 1

# Open independent numbered sessions in separate Terminal windows:
codex 2
codex 3
claude 2

# Give the agent a task, then close the daily Mac whenever you need to.
# No detach shortcut is required. Repeat the same command to reconnect.

# On the daily Mac, start an independent session in a prepared M1 checkout:
codex 2 /Users/trevorstout/projects/my-prepared-worktree
claude 2 /Users/trevorstout/projects/another-prepared-worktree

# Without a project argument, a new session gets its own scratch directory.
codex 4

# Optionally detach with Ctrl-B followed by D, or just close the daily Mac.
# Reconnect to the exact same running session:
codex 2

# List remote sessions (either launcher shows both agents):
codex-home list
```

Repeat the same number to reconnect. Codex and Claude have separate numbers: `codex 1` and `claude 1` are independent sessions. `codex-home list` or `claude-home list` shows both agents, their session numbers/names, process state, connection count, and folders. A session number must be the first argument, with no leading zero: `codex 2`, not `codex --detach 2`. Use `codex 2 --detach` if you want to start or reuse a session without attaching.

Session 1 reuses the existing `main` session when present. It does not rename, restart, or move that live session. The older `codex-home`, `claude-home`, `cx`, `cl`, and descriptive session names remain available. If separate `main` and `1` sessions already exist, both are preserved and the list shows `main` separately.

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
| `codex` / `claude` typed in a terminal | M1 proxy | Home M1, a new persistent session each time |
| `claude -p`, subcommands, scripts (no terminal) | M1 proxy, else local proxy | Daily M5 |
| `codex-local` / `claude-local` | M1 proxy, else local proxy | Daily M5 |
| `codex NUMBER` / `claude NUMBER` | M1 proxy | Home M1 |
| `codex-m5` / `claude-m5` | Original direct client | Daily M5 |
| `codex-home` / `claude-home` | M1 proxy | Home M1 |
| `cx [NUMBER]` / `cl [NUMBER]` | M1 proxy | Home M1 |

Typed in a terminal, plain `claude` or `codex` opens a new persistent session on the home host in the current repository, without a status line or banners. The session survives a dropped connection (the terminal reconnects) and closes when the agent exits. A worktree maps to its main checkout, and a folder outside any repository under the home folder opens the `home` session in the home folder. The host must have the same checkout at the same path; otherwise the launcher reports the missing folder. Print mode (`-p`), `--help`/`--version`, maintenance subcommands such as `mcp`, `update` and `exec`, and calls without a terminal stay on the daily Mac. Set `CLIPROXY_HOME_DEFAULT=0` to keep plain commands local. The SSH alias `home` in `~/.ssh/config` names the host; moving to a new machine means changing its `HostName` only.

Screenshots: an agent on the home host cannot read this Mac's clipboard. Run `shot` (install `scripts/home-shot.sh` as `~/.local/bin/shot`) to send the clipboard image, the newest Desktop screenshot, or a named file to `~/Downloads/shots/` on the home host; its path is copied for pasting into the agent.

Install `scripts/daily-proxy-shell.sh` at `~/.config/cliproxyapi-custom/daily-proxy-shell.sh` on the daily Mac. Source it at the end of `~/.zshrc`, `~/.bashrc`, and `~/.bash_profile`:

```sh
if [ -r "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh" ]; then
  . "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh"
fi
```

Open a new Terminal window after installation. To update an existing zsh window, run `source ~/.zshrc` once.

### Local fallback proxy

Install `scripts/proxy-agent.sh` as both `~/.local/bin/claude-m1` and `~/.local/bin/codex-m1`. Each launch checks that the M1 tunnel (`127.0.0.1:18318`) serves at least one model. If it does not, the launcher uses the identical proxy on the daily Mac (`127.0.0.1:8318`, service `io.tstout.cliproxyapi-custom`) and prints a one-line notice. If neither has models, it stops and names the direct `-m5` backup. Set `CLIPROXY_ROUTE=m1` or `CLIPROXY_ROUTE=local` to force a route.

The local proxy needs its own provider sign-ins. Install `scripts/cliproxy-local-login.sh` as `~/.local/bin/cliproxy-local-login` and run it once: it reads the M1's account list and opens each provider login in turn. `cliproxy-local-login status` compares the two account lists. Sign-ins are made separately rather than copying the M1's OAuth files, because providers rotate refresh tokens and two machines sharing one file would sign each other out.

### Terminal apps that wrap `claude`

cmux installs its own `claude` shell function on the first prompt, after `~/.zshrc` has run, which bypassed the proxy. The shell helper restores its function before every prompt and command. The launcher still runs `claude` through cmux's PATH shim, so cmux's hooks and notifications keep working.

cmux also starts and auto-resumes Claude sessions itself, without a shell, using the binary it recorded at launch. Install `scripts/proxy-agent.sh` as `~/.local/bin/claude-proxy` and set `automation.claudeBinaryPath` in `~/.config/cmux/cmux.json` to that path, then run `cmux reload-config`. Under that name the launcher runs the real binary directly, so it cannot loop back through cmux's wrapper. `claude-m5` sets `CLIPROXY_ROUTE=direct`, which the launcher honors, so the direct backup still bypasses the proxy inside cmux.

These functions send a positive integer first argument to the existing home launchers. Other arguments use the existing local proxy launchers. The backup functions use `command` to launch the original native binaries. Proxy settings remain in child processes. Native configuration, sign-ins, and managed binary links remain in place. Proxy or home-session errors remain visible; choose the direct backup explicitly.

Existing `claudex` and `claudek` functions must call `command claude` for their configured provider routes. Otherwise the new `claude` function would select the M1 proxy for those older routes. The daily Mac's two existing calls were updated accordingly.

This changes interactive Terminal commands. It does not change Codex desktop settings or the M1's command defaults. [Official OpenAI documentation](https://learn.chatgpt.com/docs/config-file/config-advanced) describes how per-invocation provider settings remain separate from stored client configuration.

### Home-session helper

The daily shell functions call `~/.local/bin/codex-home` and `~/.local/bin/claude-home` for numbered sessions. Keep these full launchers installed. Optional older shortcuts use `scripts/codex-home-short.sh` as `~/.local/bin/cx` and `scripts/claude-home-short.sh` as `~/.local/bin/cl`. They call that Mac's full home launchers and default to number 1.

`scripts/home-coding-session.py` has three modes:

- `client codex|claude`: use SSH to create or attach on the host.
- `host codex|claude`: manage the host's dedicated `cliproxy-home` tmux socket.
- `run codex|claude -- ARGS`: execute the agent against the host gateway.

Install the script as `~/.local/bin/home-coding-session` on both machines. The daily-Mac wrapper selects `client` mode, sets `HOME_CODING_SSH_HOST`, and optionally `HOME_CODING_SSH_KEY`. Set `HOME_CODING_REMOTE_SCRIPT` for another installed host path. Set `HOME_CODING_REMOTE_PYTHON` to the installed interpreter when the non-interactive SSH path cannot find it. These Macs use `/opt/homebrew/bin/python3`; macOS's `/usr/bin/python3` can depend on Xcode selection/license state. `CPA_CODEX_MODEL` can select the default Codex model; normal agent model flags override it.

The host needs Python 3, tmux, Codex, and Claude Code. It reads the inference key at runtime from `~/.config/cliproxyapi-custom/client-api-key`, requiring private permissions, and connects to `http://127.0.0.1:8318`. `CPA_PROXY_KEY_FILE` and `CPA_PROXY_URL` customize those host-side paths. The proxy key stays in the agent environment, never the SSH command or tmux arguments. No provider OAuth files or OpenAI API key are copied.

The dedicated tmux socket preserves existing sessions and configuration. Its private `home-tmux.conf` inherits the existing theme, keeps exited panes visible, and labels its status bar `HOME HOST`. Dates and times use `America/Los_Angeles`.

For phone access, the same session can be reached with a private SSH connection while the phone is on Tailscale. Native app remote interfaces are separate: Claude Remote Control excludes custom gateway authentication and custom base URLs; Codex desktop supports SSH projects, but phone pairing and gateway compatibility need their own setup and verification.

References: [tmux persistence](https://github.com/tmux/tmux/wiki/Getting-Started#attaching-and-detaching), [Claude Remote Control](https://code.claude.com/docs/en/remote-control), [Codex remote connections](https://learn.chatgpt.com/docs/remote-connections).

### Dashboard app

`macos/CLIProxyDashboard/build.sh` builds `CLI Proxy.app` into `/Applications`. It opens the management dashboard in its own window. View > This Mac (⌘1) shows the local fallback proxy; View > M1 (⌘2) shows the main gateway through the tunnel. View > Copy Management Key (⌘K) copies the key for the dashboard shown, for its sign-in screen. Use the dashboard's OAuth page to sign accounts into a proxy; provider pages open in Safari. The Quota page shows each account's usage.
