# Optional coding sessions on the home host

The existing `codex-m1` and `claude-m1` commands execute on the daily Mac and send inference to the M1 gateway. Sleeping the daily Mac pauses those jobs. The separate `codex-home` and `claude-home` commands execute the entire coding session on the M1, inside tmux. The daily Mac is just its terminal connection.

```sh
# On the daily Mac, start an independent session in a prepared M1 checkout:
codex-home long-task /Users/trevorstout/projects/my-prepared-worktree
claude-home another-task /Users/trevorstout/projects/another-prepared-worktree

# Without a project argument, a new session gets its own scratch directory.
codex-home planning

# Detach with Ctrl-B followed by D, or close the daily Mac.
# Reconnect to the exact same running session:
codex-home long-task

# List remote sessions (either launcher shows both agents):
codex-home list
```

The M1 must remain plugged in, awake, online, and logged in. Leave its lid open; the screen may lock or turn off. A small per-agent caffeinate process prevents idle sleep while an agent runs, but does not prevent lid-close sleep or survive a reboot. tmux preserves a session across SSH disconnects, not host shutdowns. A task can still pause for a question, permission, quota, or tool failure. Detaching does not create a goal or make an interactive agent work indefinitely.

The M1 runs builds, tests, and commands against its own files. A repository on the daily Mac is not automatically available there. Before real project work, fetch the desired branch on the M1 and create an isolated worktree; migrated checkouts may be stale. Use Git to transfer reviewed commits between machines. Do not copy credential folders, customer records, or an entire dirty workspace, and do not let two sessions edit the same worktree. The launchers do not pull, reset, synchronize, or rewrite project files.

Session names accept letters, numbers, hyphens, and underscores. Repeat the same agent and name to reattach. A conflicting project directory is rejected. Arguments after `--` launch a new agent; when reconnecting, they are not submitted again:

```sh
codex-home investigation my-prepared-worktree -- -m gpt-6.1-sol
claude-home investigation my-prepared-worktree -- --model claude-haiku-4-5-20251001
codex-home --detach background my-prepared-worktree -- "Investigate the test failures and implement a fix."
```

Agent exit leaves its terminal output visible. Reconnect to inspect it. To resume a saved conversation after an exit or host reboot, start a fresh tmux name in the same project with `-- resume --last` for Codex, or `-- --continue` for Claude. This resumes the agent's saved transcript; it cannot restore a terminated process. An existing live local-Mac chat is not transferred by these commands.

## Installation and authentication

`scripts/home-coding-session.py` has three modes:

- `client codex|claude`: use SSH to create or attach on the host.
- `host codex|claude`: manage the host's dedicated `cliproxy-home` tmux socket.
- `run codex|claude -- ARGS`: execute the agent against the host gateway.

Install the script as `~/.local/bin/home-coding-session` on both machines. The daily-Mac wrapper selects `client` mode, sets `HOME_CODING_SSH_HOST`, and optionally `HOME_CODING_SSH_KEY`. Set `HOME_CODING_REMOTE_SCRIPT` for another installed host path. `CPA_CODEX_MODEL` can select the default Codex model; normal agent model flags override it.

The host needs Python 3, tmux, Codex, and Claude Code. It reads the inference key at runtime from `~/.config/cliproxyapi-custom/client-api-key`, requiring private permissions, and connects to `http://127.0.0.1:8318`. `CPA_PROXY_KEY_FILE` and `CPA_PROXY_URL` customize those host-side paths. The proxy key stays in the agent environment, never the SSH command or tmux arguments. No provider OAuth files or OpenAI API key are copied.

The dedicated tmux socket preserves existing sessions and configuration. Its private `home-tmux.conf` inherits the existing theme, keeps exited panes visible, and labels its status bar `HOME HOST`. Dates and times use `America/Los_Angeles`.

For phone access, the same session can be reached with a private SSH connection while the phone is on Tailscale. Native app remote interfaces are separate: Claude Remote Control excludes custom gateway authentication and custom base URLs; Codex desktop supports SSH projects, but phone pairing and gateway compatibility need their own setup and verification.

References: [tmux persistence](https://github.com/tmux/tmux/wiki/Getting-Started#attaching-and-detaching), [Claude Remote Control](https://code.claude.com/docs/en/remote-control), [Codex remote connections](https://learn.chatgpt.com/docs/remote-connections).
