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

Typed in a terminal, plain `claude` or `codex` opens a new persistent session on the home host in the current repository, without a status line or banners. The session survives a dropped connection (the terminal reconnects) and closes when the agent exits. A worktree maps to its main checkout, and a folder outside any repository under the home folder opens the `home` session in the home folder. The host must have the same checkout at the same path; otherwise the launcher reports the missing folder. Print mode (`-p`), `--help`/`--version`, maintenance subcommands such as `mcp`, `update` and `exec`, and calls without a terminal stay on the daily Mac. Set `CLIPROXY_HOME_DEFAULT=0` to keep plain commands local.

The terminal attaches over mosh when it is installed on both Macs (`brew install mosh`), so Wi-Fi drops, sleep and network changes pause the screen instead of disconnecting it, and typing echoes locally. Only keystrokes and screen updates cross the network; the agent's API traffic stays on the home host. mosh has no terminal scrollback of its own (scroll inside tmux) and does not pass modified keys such as Shift+Enter; use Option+Enter or `\` then Enter for a newline. `HOME_CODING_TRANSPORT=ssh` restores the SSH route with its automatic reconnect. The SSH alias `home` in `~/.ssh/config` names the host; moving to a new machine means changing its `HostName` only.

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

cmux also starts, restores and wakes hibernated Claude sessions itself, without a shell, using its Claude Binary Path. Install `scripts/cmux-claude-home.py` as `~/.local/bin/claude-cmux-home` and set `automation.claudeBinaryPath` in `~/.config/cmux/cmux.json` to it, then run `cmux reload-config`. Every such session then runs on the home host in a session named after its conversation ID (`cmux-<id>`), so a later restore or wake reattaches to the agent that kept running there instead of starting a second copy. Before a resume it copies that conversation's transcript to the host. It drops cmux's `--settings` hooks, which call this Mac's cmux socket; cmux's sidebar status and Claude notifications therefore do not fire for these sessions. Print mode, `--version`, calls without a terminal, and an unreachable host run the local binary directly and say so. The previous proxy launcher remains at `~/.local/bin/claude-proxy`.

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

## Remote-first with cmux (current setup, 2026-10-09)

This supersedes the laptop-side routing above. The laptop is only the screen; every agent runs on the home host next to CLIProxyAPI (always-on host, Tailscale, mosh, tmux).

- **Daily use:** open cmux NIGHTLY and press Cmd+N. The `m1-workspace` action in `~/.config/cmux/cmux.json` runs `cmux mosh-tmux home --session m1-<time>`, one tmux session per workspace. Type `claude`, `claude resume`, `claude --resume`, `codex` or `codex resume`. Typing echoes locally, drops and sleep reconnect, dragged files upload over cmux's SSH lane. Use Option+Enter for a newline (mosh does not pass Shift+Enter). `terminal.autoResumeAgentSessions` is false so cmux never relaunches agents on the laptop.
- **Host launchers:** `scripts/home-shims/{claude,codex}` live in `~/.config/cliproxyapi-custom/shims/` and run `home-coding-session run`, which points the agent at the local proxy and keeps the key in the agent's environment only. `scripts/home-agent-shell.sh`, sourced from the host's `~/.zshrc`, puts the shims first in interactive shells; cmux's own `claude`/`codex` wrappers (sidebar status, notifications) resolve through PATH and reach them.
- **Host shell and tmux:** `~/.zshenv` appends `/opt/homebrew/bin` so non-interactive SSH finds `mosh-server` and `tmux` (otherwise cmux falls back to SSH). `~/.tmux.conf` hides the status bar and enables full color, extended keys, OSC 52, passthrough, zero Escape delay and long history.
- **Retired on the laptop:** the tunnel, proxy switch and fallback proxy LaunchAgents (plists in `~/Library/LaunchAgents.disabled-2026-10-09/`) and the `daily-proxy-shell.sh` sourcing. Backups: `~/.config/cliproxyapi-custom/backup-2026-10-09-remote-first/` on both Macs.

## Ghostty with a tab sidebar on the home host (current setup, 2026-10-09)

Open Ghostty. Each window lists your tabs down its left side, like cmux, with what each agent is doing: a softly pulsing blue dot while it works, a yellow dot when it needs you (a permission prompt or question), and a green dot when it finished while you were looking elsewhere. Press Cmd+N for a new tab, click a tab to show it, and type `claude` or `codex`. Every tab runs on the home host; nothing agent-related runs on the laptop.

| Keys | Action |
| --- | --- |
| Cmd+N or Cmd+T | New tab (starts in the home folder) |
| Cmd+W, or click a tab's ×, or middle-click it | Close tab (asks first if something other than a shell is running). The × shows on the current tab and the one under the mouse |
| Cmd+1 to Cmd+8, Cmd+9 | Show that tab; Cmd+9 is the last tab |
| Cmd+Shift+] / Cmd+Shift+[ , Ctrl+Tab / Ctrl+Shift+Tab | Next / previous tab |
| Cmd+Shift+U | Show the next tab that needs you or finished while you were away |
| Cmd+Shift+R, or double-click a tab | Rename the tab |
| Drag a tab | Move it up or down the list |
| Right-click a tab | Rename, mark as read, move up or down, close |
| Cmd+Shift+N / Cmd+Shift+W | Another window on the same tabs, opening on a new tab / close the window (tabs keep running) |

- **Laptop:** Ghostty's app-specific config (`~/Library/Application Support/com.mitchellh.ghostty/config.ghostty`, not read by cmux) is a copy of `scripts/m1-ghostty.config`. It sets `command = ~/.local/bin/m1` (`scripts/m1-terminal.sh`, mosh with `MOSH_PREDICTION_DISPLAY=adaptive` (local echo only on a slow link; on a fast one, guessed keystrokes that an agent's screen redraws differently only flicker) and `MOSH_TITLE_NOPREFIX=1`, so window titles lose mosh's "[mosh]" prefix; a local shell if the host is unreachable), `window-save-state = always`, `window-padding-color = extend` (the window margin takes the sidebar panel's color instead of a lighter strip), Shift+Enter as Meta+Enter (mosh does not pass Shift+Enter), and the tab keys above. The keys send the tabs server's prefix, Ctrl+B, plus a key; Ghostty's own tabs are not used. Reload the config with Cmd+Shift+Comma.
- **Host:** `~/.local/bin/m1-tab` (`scripts/m1-tab.sh`) uses a dedicated tmux server (`tmux -L tabs`, started without inherited `ZDOTDIR`). Tabs are the windows of the session group `main`. Each terminal window attaches its own grouped session (`w-*`, removed when the window closes), so two windows can show different tabs. The first window opens on the tab last shown; a window or Ghostty tab opened while another window is showing the list starts on a new tab, so it never mirrors the tab already on screen. On every connection it sources `~/.config/m1-tabs/tmux.conf` (`scripts/m1-tabs.tmux.conf`: keys, mouse and hooks) and runs `m1-sidebar ensure`.
- **Sidebar:** `~/.local/bin/m1-sidebar` (`scripts/m1-sidebar.py`) runs in a 32-column pane on the left of every tab, on a slightly darker panel (colors suit Ghostty's default dark theme; the pane borders take the panel color). Each tab shows its title (a name you gave it, the agent's own title without its status glyph or Codex's trailing `| folder`, or the shell's folder) on up to two lines, then a status line: `Working · 3m`, `Needs you`, `Done · 4m ago` (`just now` under a minute), `Idle · 25m`, or the folder for a shell, plus the folder name when an agent is not in the home folder. Times count whole minutes so nothing ticks every second. Status comes from the agent's footer: Claude Code and Codex show "esc to interrupt" while a turn runs (or "Waiting for N background agents"), and "Esc to cancel" / "Enter to select" on prompts. A tab that stops working while no focused window shows it is marked unread until you look at it; the state lives in window options (`@m1_state`, `@m1_since`, `@m1_unread`), so every window's sidebar agrees. A short pause in the footer does not count as finished (3 s), and a prompt must stay up 1.5 s before the tab says it needs you, so passing menus do not blink the dot. Codex runs as `node`, so any non-shell tab whose screen shows an agent footer counts as an agent. A bell from a background tab also shows as "Needs you". Clicks act on release on the row that was pressed (a menu opened on press would close on release, and a drag must end in the sidebar where it began); a double-click is recorded on the server (`@m1_click`) because the first click usually switches to another tab's sidebar. Hover uses mouse-motion reporting, which tmux and mosh both pass through; leaving the pane sends nothing, so a hover fades after a few seconds. **Stability:** a sidebar repaints only the rows that changed (rows are drawn full width with line wrap off, so it never erases to the end of a line, which would wipe the last cell), and working dots step every 0.4 s, changing one cell each; a 2026-10-09 recording of the same six tabs went from about 11.5 KB/s and 107 sidebar row updates/s to 2.2 KB/s and 24. A new tab (Cmd+N, the sidebar's New tab, or a new window) is created in the background with its sidebar, shown once the sidebar has drawn (`@sidebar_ready`, at most 0.5 s), so it never flashes as a full-width shell that then narrows. A poll that fails (a tab closing mid-read) keeps the last frame; only three failures in a row show the error. A hover fades 3 s after the last mouse motion, or at once when the pointer was last on the outer column. Claude Code itself repaints its whole screen about once a second for its background-agent timers; mosh sends only the cells that change. After installing a new version, run `m1-sidebar restart` to restart every sidebar. Hooks keep a sidebar in every tab, at the right width, and redraw them. A sidebar left alone closes its tab, so exiting the shell or agent closes the tab. Clicks reach the sidebar without moving keyboard focus to it. A crashed sidebar stays on screen with its error (`remain-on-exit`) instead of respawning in a loop; the next `ensure` restarts it.
- **Drag and drop:** drop a screenshot (from the thumbnail after Cmd+Shift+4 or from Finder) or any file into a window and Claude or Codex on the host attaches it. Ghostty pastes a dropped file as its escaped local path, which the host cannot open, so `~/.local/bin/m1` runs mosh through `~/.local/bin/m1-drop-bridge` (`scripts/m1-drop-bridge.py`). When a paste holds only paths to files on the laptop, the bridge uploads them over `ssh home` (a shared connection; about 1 s for a screenshot) to `~/Drops/<date>/<time>-<name>` on the host and pastes those paths instead; typing after the drop waits for the upload. Anything else passes through unchanged; a failed upload pastes the original path. Log: `~/Library/Logs/m1-drop-bridge.log` on the laptop. If a drop still pastes the laptop path and the log says "cannot read dropped … Operation not permitted", give Ghostty Full Disk Access (System Settings > Privacy & Security) and restart it. `~/Drops` is not cleaned automatically.
- **Clipboard:** when Claude, Codex or a shell on the host runs `pbcopy`, the text also lands on the clipboard of the laptop you typed in last. `scripts/home-shims/pbcopy` (installed in `~/.config/cliproxyapi-custom/shims/`, ahead of `/usr/bin` in agent and tab shells) copies on the host as usual, then has tmux send the text to the most recently active terminal on the tabs server (or the tmux server it runs in) as an OSC 52 clipboard escape (`load-buffer -w`); mosh carries it and Ghostty sets the clipboard. Agents copy without a terminal of their own, which is why tmux sends it. mosh only passes the escape when it names the clipboard (`52;c;`), while tmux leaves that field empty, so `m1-tabs.tmux.conf` overrides tmux's `Ms` capability (the `%p1%.0s` consumes the unused parameter; without it nothing is sent). tmux reads capabilities when a terminal connects, so windows open before a change to that line keep the old form until reopened, or reconnect one in place: `tmux -L tabs detach-client -t <client> -E "exec tmux -L tabs new-session -t main -s w-<name> \; set destroy-unattached on \; select-window -t <window>"`. `-pboard find/font/ruler` stays on the host. Copying works one way: the host cannot read the laptop's clipboard.
- **Old layout:** sessions from the earlier one-session-per-tab layout (`t-*`) join the tab list, still running, as soon as no window shows them.
- `home-agent-shell.sh` always moves the proxy launchers to the front of PATH, since an inherited PATH can hold them behind `~/.local/bin`.
- Tests: `/opt/homebrew/bin/python3 -m unittest scripts/test_m1_sidebar.py scripts/test_m1_drop_bridge.py scripts/test_home_pbcopy.py` (run outside tmux, or with `TMUX` unset; it starts a private tmux server).
