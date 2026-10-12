# Automatic development-server cleanup

`dev-reaper` checks this user's development servers every five minutes. It runs
with background priority through LaunchAgent `io.tstout.dev-reaper`.

An idle server receives SIGTERM after 24 hours without observed use. Under macOS
memory pressure, the cutoff is three hours. The idle clock starts when the service
first sees the server. A gap longer than 15 minutes starts a new clock, so sleep,
reboots, and failed checks do not count as verified idle time.

An open TCP connection, at least one second of server or child CPU use between checks,
or a running Claude/Codex process in the project resets the clock. An open browser
tab normally holds an HMR connection. Ports and project paths in the keep file
also protect a server. CPU polling and connection snapshots can miss brief use
between checks; use a keep rule for a preview that must remain available.

The service matches development commands only. A renamed `next-server` must have
an ancestor that explicitly runs `next dev`. Production servers, Postgres,
terminal sessions, coding agents, CLI Proxy, Tailscale, and other host services
are excluded. A failed process/socket/cwd check skips cleanup. Before shutdown,
the service checks connections, protection, and process start identity again.
It signals only the server and its verified children; parents and sibling servers
remain running. A server that refuses SIGTERM requires manual inspection.

Orphaned Next.js telemetry flushes receive SIGTERM after ten minutes.
No project files, database data, or caches are deleted.

## Inspect and control

```sh
dev-reaper --status
dev-reaper --dry-run
tail -30 ~/Library/Logs/dev-reaper.log
```

Status and dry-run do not advance the saved idle clock. Logs use Pacific time.
The state file is `~/.config/dev-reaper/state.json`.

To keep a preview, add one port or path substring per line to
`~/.config/dev-reaper/keep`. The next check reads the changes.

```text
# Examples
3010
/projects/preview-that-must-stay-up
```

To pause or resume automatic cleanup:

```sh
launchctl bootout gui/$(id -u)/io.tstout.dev-reaper
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.tstout.dev-reaper.plist
```

## Install and test

```sh
/opt/homebrew/bin/python3 scripts/test_dev_reaper.py
/opt/homebrew/bin/python3 scripts/install-dev-reaper.py --load
```

The installer backs up replaced files under `~/.config/dev-reaper/backups/`.
It preserves keep rules. Use `--home <temporary-folder>` without `--load` to test
installation separately. This installer does not restart or deploy CLI Proxy.

## Adobe Creative Cloud

The October 11 cleanup disabled Adobe's user startup services. Complete removal
uses Adobe's native uninstaller, which requires administrator authentication:

```sh
open '/Applications/Utilities/Adobe Creative Cloud/Utils/Creative Cloud Uninstaller.app'
```

Choose **Uninstall** and complete the macOS administrator prompt. If Adobe says
another installed Adobe app requires Creative Cloud, review those apps before
removing their shared components. Disabled startup services can remain disabled
while full removal waits. This cleanup does not cancel an Adobe subscription.
