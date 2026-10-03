# Persistent home coding sessions

Acceptance criteria for October 2, 2026 (Pacific):

- [x] Existing daily-Mac `codex-m1` / `claude-m1` keep running locally through the gateway.
- [x] Separate `codex-home` / `claude-home` commands start or reattach coding sessions on the M1 over private SSH.
- [x] Remote clients use the M1's loopback gateway and read its existing private key locally; secrets never appear in arguments or output.
- [x] A detached remote process survives closing its SSH connection; both actual agent clients return PONG on the M1.
- [x] Reconnecting preserves the same live tmux pane/process and never submits the original prompt twice.
- [x] Project paths and arguments are quoted safely; missing or conflicting directories fail clearly.
- [x] Existing tmux sessions, global client settings, migrated project files, and services are preserved.
- [x] Document local versus remote execution, repository preparation, sleep/reboot limits, and remote-control gateway limitations.
- [x] Focused checks and required repository build pass; feature is committed, pushed, and integrated serially.

Dependencies: working Tailscale and key-based SSH, installed Codex/Claude/tmux on M1, powered and awake M1, a current project checkout before real project work. No application release or proxy restart is needed.

Verification: six focused Python checks passed, including detached argument parsing, literal SSH quoting, private-key handling, missing/conflicting project paths, and no prompt resubmission. Required Go build passed. Native Codex and Claude Code ran on M1 through its local gateway and returned PONG. An actual interactive Codex process kept the same PID before disconnect, while detached (zero terminal clients), and after reconnect. It ran `/bin/sleep 15` and replied PONG without an attached client. This demonstrates continued execution, not just saved chat history.

Delivery: the `feat/persistent-home-sessions` unit is committed and pushed to the fork and integrated into `integration`; no `main` release. Personal launchers are installed on both Macs. First project selection is optional and pending; migrated checkouts must be refreshed/prepared before real project work. No real coding task, application release, proxy restart, migration, or existing provider/auth configuration change has occurred. The CLI-created test-folder trust entry and all test tmux sessions were removed after verification.
