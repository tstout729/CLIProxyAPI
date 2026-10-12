# Finish automatic Mac development cleanup

Requested October 11, 2026 (Pacific). Continue the unfinished local `dev-reaper`
draft and Adobe Creative Cloud cleanup. Installation on this Mac is authorized.

## Acceptance criteria

- A LaunchAgent checks every five minutes and survives login/restart.
- Development servers stop after 24 hours of observed inactivity, or three hours
  under macOS memory pressure. First observation starts a new idle timer.
- Open connections, meaningful server CPU activity, running coding agents in the
  same project, and explicit keep rules preserve a server.
- Production servers, databases, terminal sessions, agents, and permanent host
  services are excluded. Failed inspection never permits termination.
- Termination rechecks process identity and connections, uses graceful shutdown,
  and preserves sibling servers. Status, dry-run, state, and logs work.
- Focused tests and a disposable live listener verify cleanup and protection.
- Source is committed, pushed, and integrated without changing the proxy service.
- Adobe startup remains disabled. Native full removal is attempted only with
  administrator access; any missing access is reported with a concrete handoff.

## Current state

- Found the unfinished draft at `~/.local/bin/dev-reaper`; no LaunchAgent existed.
- Adobe GUI services are disabled. The root installer helper remains idle.
- `sudo -n true` requires a password. Full Adobe uninstall requires Trevor's
  administrator authentication; the native uninstaller is installed.
- Source ownership: CLIProxyAPI `scripts/`, the existing home-host utility source.
- Worktree: `CLIProxyAPI-dev-reaper`, branch `feat/macos-dev-reaper-20261011`.
- Guarded detection and graceful shutdown implemented. Review found and fixed
  incidental-command matches, native Claude detection, generic cwd boundaries,
  compiler-child activity, and activity discovered during the final recheck.
- 24 tests passed, including six disposable live listener tests and a regression
  for an agent exiting during cwd inspection. Required Go server build passed.
- LaunchAgent installed. Installed source hash matched tested source; periodic
  state records prove successful checks. Busy-host ps/lsof timeouts skip cleanup
  safely. Latest loaded-job readback: 18 runs, last exit code 0, 300-second interval.
- Remaining: latest-source installation, commit/push/integrate, canonical record.
- Prepared 59-second silent guide. Automatic approval review blocked sending
  the narration to ElevenLabs; narration waits for explicit permission.
