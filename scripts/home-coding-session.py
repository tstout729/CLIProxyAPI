#!/usr/bin/env python3
"""Launch gateway-backed coding sessions locally or persistently over SSH."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time


SOCKET = "cliproxy-home"
# Keep agents feeling like a plain local terminal: Shift+Enter and other
# modified keys, copy to the laptop's clipboard, no Escape delay, long scrollback.
NATIVE_FEEL = [
    "set -s extended-keys on",
    "set -as terminal-features 'xterm*:extkeys'",
    "set -s set-clipboard on",
    "set -g allow-passthrough on",
    "set -s escape-time 0",
    "set -g history-limit 100000",
    # Full color (Claude dims to 256 colors otherwise) and agent tab titles.
    'set -g default-terminal "tmux-256color"',
    "set -as terminal-features ',xterm*:RGB'",
    "set -as terminal-features ',*256col*:RGB'",
    "set-environment -g COLORTERM truecolor",
    "set -g set-titles on",
    "set -g set-titles-string '#T'",
    "set -g allow-rename on",
]
PREFIX = "home-"


def format_sessions(output):
    records = []
    for line in output.splitlines():
        fields = line.split("\t", 3)
        if len(fields) == 4:
            match = re.fullmatch(r"home-(codex|claude)-(.+)", fields[0])
            if match:
                records.append((fields[0], match[1], match[2], *fields[1:]))
    names = {row[0] for row in records}
    rows = []
    for name, agent, number, dead, attached, directory in records:
        if number == "main" and f"{PREFIX}{agent}-1" not in names:
            number = "1"
        rows.append((agent.capitalize(), number, "exited" if dead == "1" else "running", attached, directory))
    if not rows:
        return "No home coding sessions yet."
    rows.sort(key=lambda row: (row[0], (0, int(row[1])) if row[1].isascii() and row[1].isdigit() else (1, row[1])))
    rows.insert(0, ("Agent", "Session", "Process", "Connected", "M1 folder"))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join("  ".join(value.ljust(widths[i]) for i, value in enumerate(row[:4])) + "  " + row[4] for row in rows)


def fail(message):
    raise SystemExit(message)


def environment():
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", env.get("PATH", os.defpath)]
    )
    env["TZ"] = "America/Los_Angeles"
    return env


def run_agent(agent, arguments):
    env = environment()
    key_path = Path(env.get("CPA_PROXY_KEY_FILE", "~/.config/cliproxyapi-custom/client-api-key")).expanduser()
    if not key_path.is_file() or stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        fail("The host inference key must exist and be private (mode 0600).")
    key = key_path.read_text().strip()
    if not key:
        fail("The host inference key is empty.")
    executable = shutil.which(agent, path=env["PATH"])
    if not executable:
        fail(f"Install {agent} on the host before starting this session.")
    base = env.get("CPA_PROXY_URL", "http://127.0.0.1:8318").rstrip("/")
    if agent == "codex":
        env["CLIPROXY_API_KEY"] = key
        arguments = [
            "-c", 'model_provider="cliproxy_home"',
            "-c", 'model_providers.cliproxy_home.name="Home CLI Proxy"',
            "-c", "model_providers.cliproxy_home.base_url=" + json.dumps(base + "/v1"),
            "-c", 'model_providers.cliproxy_home.env_key="CLIPROXY_API_KEY"',
            "-c", 'model_providers.cliproxy_home.wire_api="responses"',
            "-c", "model_providers.cliproxy_home.requires_openai_auth=false",
            "-c", "model_providers.cliproxy_home.supports_websockets=true",
            *arguments,
        ]
        model = env.get("CPA_CODEX_MODEL")
        if model:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
                fail("Invalid CPA_CODEX_MODEL.")
            arguments = ["-c", f'model="{model}"', *arguments]
    else:
        env["ANTHROPIC_BASE_URL"] = base
        env["ANTHROPIC_AUTH_TOKEN"] = key
        env.pop("ANTHROPIC_API_KEY", None)
    # Prevent idle sleep while the agent lives. Closing the host lid still sleeps it.
    if sys.platform == "darwin":
        subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    os.execvpe(executable, [executable, *arguments], env)


def tmux_command():
    env = environment()
    executable = shutil.which("tmux", path=env["PATH"])
    if not executable:
        fail("Install tmux on the coding host first.")
    root = Path.home() / ".config/cliproxyapi-custom"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = root / "home-tmux.conf"
    if not config.exists():
        lines = []
        if (Path.home() / ".tmux.conf").is_file():
            lines.append("source-file " + shlex.quote(str(Path.home() / ".tmux.conf")))
        lines.extend([
            "set -g remain-on-exit on",
            'set -g status-right "HOME HOST · #S · %H:%M"',
            *NATIVE_FEEL,
        ])
        with config.open("x") as output:
            config.chmod(0o600)
            output.write("\n".join(lines) + "\n")
    return [executable, "-L", SOCKET, "-f", str(config)], env


def host_session(agent, options, arguments):
    command, env = tmux_command()

    def tmux(*args, check=False):
        return subprocess.run(command + list(args), env=env, capture_output=True, text=True, check=check)

    def launch_command():
        launch = [sys.executable, str(Path(__file__).resolve()), "run", agent, "--", *arguments]
        if env.get("CPA_CODEX_MODEL"):
            launch = ["/usr/bin/env", "CPA_CODEX_MODEL=" + env["CPA_CODEX_MODEL"], *launch]
        return launch

    if options.name == "list":
        result = tmux("list-sessions", "-F", "#{session_name}\t#{pane_dead}\t#{session_attached}\t#{pane_start_path}")
        print(format_sessions(result.stdout) if result.returncode == 0 else "No home coding sessions yet.")
        return
    if not options.name or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", options.name):
        fail("Choose a session name using letters, digits, hyphens, or underscores.")
    session = f"{PREFIX}{agent}-{options.name}"
    target = "=" + session
    exists = tmux("has-session", "-t", target).returncode == 0
    # Session 1 reuses an existing default without renaming or restarting it.
    # If both old names exist, preserve each session's original identity.
    if not exists and options.name in ("main", "1"):
        alternate = f"{PREFIX}{agent}-{'1' if options.name == 'main' else 'main'}"
        alternate_exists = tmux("has-session", "-t", "=" + alternate).returncode == 0
        session = alternate if alternate_exists else f"{PREFIX}{agent}-1"
        target = "=" + session
        exists = alternate_exists
    pane_target = target + ":"
    directory = None
    if options.project:
        directory = Path(options.project).expanduser()
        if not directory.is_absolute():
            directory = Path.home() / "projects" / directory
        directory = directory.resolve()
        if not directory.is_dir():
            fail(f"The project folder does not exist on the host: {directory}. Clone or prepare it first.")
    if exists:
        saved = tmux("display-message", "-p", "-t", pane_target, "#{pane_start_path}", check=True).stdout.strip()
        if directory and str(directory) != saved:
            fail(f"This session already uses {saved}; choose another session name for {directory}.")
        directory = Path(saved)
        # A pane whose agent exited is never useful; restart it in place.
        if tmux("display-message", "-p", "-t", pane_target, "#{pane_dead}", check=True).stdout.strip() == "1":
            tmux("respawn-pane", "-t", pane_target, "-c", str(directory), "--", *launch_command(), check=True)
            print("The previous agent exited; starting it again. Saved conversations remain available in the agent's resume menu.")
        elif arguments:
            print("Reconnecting to the existing session; launch arguments are not submitted again.")
    else:
        if directory is None:
            directory = Path.home() / "projects/home-coding" / session
            legacy = Path.home() / "projects/home-coding" / f"{PREFIX}{agent}-main"
            if options.name in ("main", "1") and not directory.exists() and legacy.is_dir():
                directory = legacy
            directory.mkdir(parents=True, exist_ok=True)
        created = tmux("new-session", "-d", "-s", session, "-n", agent, "-c", str(directory), "--", *launch_command())
        if created.returncode != 0 and tmux("has-session", "-t", target).returncode != 0:
            fail(created.stderr.strip())
        if created.returncode == 0 and getattr(options, "fresh", False):
            # A one-off session closes when its agent exits instead of lingering.
            tmux("set-option", "-t", pane_target, "remain-on-exit", "off")
            # Look like the agent running in a plain terminal: no tmux status line.
            tmux("set-option", "-t", pane_target, "status", "off")
        # A simultaneous creator must not let us attach to a different project.
        saved = tmux("display-message", "-p", "-t", pane_target, "#{pane_start_path}", check=True).stdout.strip()
        if str(directory) != saved:
            fail(f"A concurrent session uses {saved}; choose another session name.")
    if not getattr(options, "fresh", False):
        print(f"Running on the HOME HOST: {session}\nFiles: {directory}\nYou can close this laptop; work stays on the home host. Run the same command to reconnect.\nOptional detach: Ctrl-B, then D.", flush=True)
    if not options.detach:
        os.execvpe(command[0], command + ["attach-session", "-t", target], env)


def client_session(agent, options, arguments):
    host = os.environ.get("HOME_CODING_SSH_HOST")
    if not host or host.startswith("-"):
        fail("Set HOME_CODING_SSH_HOST to your SSH host or user@host.")
    script = os.environ.get("HOME_CODING_REMOTE_SCRIPT", ".local/bin/home-coding-session")
    remote = [script, "host", agent]
    if options.detach:
        remote.append("--detach")
    if getattr(options, "fresh", False):
        remote.append("--fresh")
    remote.append(options.name)
    if options.project:
        remote.append(options.project)
    remote.extend(["--", *arguments])
    remote_python = os.environ.get("HOME_CODING_REMOTE_PYTHON")
    if remote_python:
        remote.insert(0, remote_python)
    if os.environ.get("CPA_CODEX_MODEL"):
        remote = ["env", "CPA_CODEX_MODEL=" + os.environ["CPA_CODEX_MODEL"], *remote]
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    identity = os.environ.get("HOME_CODING_SSH_KEY")
    if identity:
        ssh.extend(["-o", "IdentitiesOnly=yes", "-i", str(Path(identity).expanduser())])
    if options.name != "list" and not options.detach:
        if not sys.stdin.isatty():
            fail("Attach from an interactive Terminal, or use --detach.")
        ssh.append("-t")
    if not getattr(options, "fresh", False):
        print(f"Connecting to {host}. The coding process and project files stay on that host.", flush=True)
    if options.name == "list" or options.detach:
        os.execvp("ssh", [*ssh, host, shlex.join(remote)])
    attach_with_reconnect(ssh, host, remote, remote[:len(remote) - len(arguments)])


# ssh exits 255 when the connection fails or drops; tmux exits 0 on detach.
SSH_CONNECTION_ERROR = 255
# A connection that lasted this long reached the host, so later failures are network drops.
CONNECTED_SECONDS = 5


def attach_with_reconnect(ssh, host, remote, reattach):
    """Keep the terminal open across network drops; the agent keeps running on the host."""
    connected = False
    command = remote
    probe = [option for option in ssh if option != "-t"]
    while True:
        started = time.monotonic()
        code = subprocess.run([*ssh, host, shlex.join(command)]).returncode
        if code != SSH_CONNECTION_ERROR:
            raise SystemExit(code)
        connected = connected or time.monotonic() - started >= CONNECTED_SECONDS
        if not connected:
            raise SystemExit(code)
        # Leave tmux's alternate screen, mouse, and paste modes behind the dead connection.
        sys.stdout.write("\033[?1049l\033[?25h\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[0m\n")
        # Reconnect without the launch arguments so nothing is submitted twice.
        command = reattach
        try:
            print("Connection to the home host was lost. The agent is still running there.\n"
                  "Reconnecting automatically; press Ctrl-C to stop trying.", flush=True)
            while True:
                time.sleep(3)
                if subprocess.run([*probe, host, "true"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0:
                    break
        except KeyboardInterrupt:
            print()
            raise SystemExit(130)
        print("Reconnected.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["client", "host", "run"])
    parser.add_argument("agent", choices=["codex", "claude"])
    parser.add_argument("--detach", action="store_true", help="Start without attaching")
    parser.add_argument("--fresh", action="store_true", help="Close the new session when its agent exits")
    parser.add_argument("name", nargs="?", default="main", help="Session number/name (default: session 1), or 'list'")
    parser.add_argument("project", nargs="?", help="Existing host project name or absolute path")
    raw = sys.argv[1:]
    boundary = raw.index("--") if "--" in raw else len(raw)
    options = parser.parse_intermixed_args(raw[:boundary])
    arguments = raw[boundary + 1:]
    if options.mode == "run":
        run_agent(options.agent, arguments)
    elif not options.name:
        parser.print_help()
    elif options.mode == "host":
        host_session(options.agent, options, arguments)
    else:
        client_session(options.agent, options, arguments)


if __name__ == "__main__":
    main()
