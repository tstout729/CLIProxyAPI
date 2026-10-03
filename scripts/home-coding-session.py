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


SOCKET = "cliproxy-home"
PREFIX = "home-"


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
        ])
        with config.open("x") as output:
            config.chmod(0o600)
            output.write("\n".join(lines) + "\n")
    return [executable, "-L", SOCKET, "-f", str(config)], env


def host_session(agent, options, arguments):
    command, env = tmux_command()

    def tmux(*args, check=False):
        return subprocess.run(command + list(args), env=env, capture_output=True, text=True, check=check)

    if options.name == "list":
        result = tmux("list-sessions", "-F", "#{session_name} | #{pane_start_path} | #{session_attached} connected")
        print(result.stdout.strip() if result.returncode == 0 else "No home coding sessions yet.")
        return
    if not options.name or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", options.name):
        fail("Choose a session name using letters, digits, hyphens, or underscores.")
    session = f"{PREFIX}{agent}-{options.name}"
    target = "=" + session
    pane_target = target + ":"
    exists = tmux("has-session", "-t", target).returncode == 0
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
        if arguments:
            print("Reconnecting to the existing session; launch arguments are not submitted again.")
    else:
        if directory is None:
            directory = Path.home() / "projects/home-coding" / session
            directory.mkdir(parents=True, exist_ok=True)
        launch = [sys.executable, str(Path(__file__).resolve()), "run", agent, "--", *arguments]
        if env.get("CPA_CODEX_MODEL"):
            launch = ["/usr/bin/env", "CPA_CODEX_MODEL=" + env["CPA_CODEX_MODEL"], *launch]
        created = tmux("new-session", "-d", "-s", session, "-n", agent, "-c", str(directory), "--", *launch)
        if created.returncode != 0 and tmux("has-session", "-t", target).returncode != 0:
            fail(created.stderr.strip())
        # A simultaneous creator must not let us attach to a different project.
        saved = tmux("display-message", "-p", "-t", pane_target, "#{pane_start_path}", check=True).stdout.strip()
        if str(directory) != saved:
            fail(f"A concurrent session uses {saved}; choose another session name.")
    print(f"Running on the HOME HOST: {session}\nFiles: {directory}\nDetach: Ctrl-B, then D. Reconnect with the same command.", flush=True)
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
    remote.append(options.name)
    if options.project:
        remote.append(options.project)
    remote.extend(["--", *arguments])
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
    print(f"Connecting to {host}. The coding process and project files stay on that host.", flush=True)
    os.execvp("ssh", [*ssh, host, shlex.join(remote)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["client", "host", "run"])
    parser.add_argument("agent", choices=["codex", "claude"])
    parser.add_argument("--detach", action="store_true", help="Start without attaching")
    parser.add_argument("name", nargs="?", help="Session name, or 'list'")
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
