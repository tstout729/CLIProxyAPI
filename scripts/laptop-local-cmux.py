#!/usr/bin/env python3
"""Make cmux on the laptop a local terminal again; Ghostty keeps the home host.

T3 Code is now the main place for project work on the home host, so cmux goes
back to local terminals for work on the laptop itself (computer use, local
apps). After `apply`:

- cmux's New Workspace (Cmd+N) opens a blank local terminal. The home host
  workspace action stays in the plus-button menu and Command Palette.
- Typed in a local terminal, plain `claude` and `codex` run on the laptop
  through the shared account pool on the M1 (SSH tunnel and proxy switch,
  falling back to the laptop's own proxy). `claude 1` / `codex 1` still open
  persistent sessions on the M1. `claude-m5` / `codex-m5` use the direct
  sign-ins; Claude's built-in computer use needs a claude.ai sign-in, while
  cmux's own computer-use tools attach to agents launched in cmux terminals.
- Claude sessions that cmux starts or restores itself run locally through the
  pool (`automation.claudeBinaryPath` = ~/.local/bin/claude-proxy).

Ghostty is untouched: its windows still open the home host's tabs, and typing
`m1` in any local terminal opens them too.

Run on the laptop from a local terminal such as Terminal.app:

    ssh home cat projects/CLIProxyAPI/scripts/laptop-local-cmux.py > /tmp/laptop-local-cmux.py
    /opt/homebrew/bin/python3 -I /tmp/laptop-local-cmux.py apply

`status` reports without changing anything; `undo` restores the backups.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HOST = "home"
REPOSITORY = "projects/CLIProxyAPI"
SWITCH_URL = "http://127.0.0.1:18320"
TUNNEL_URL = "http://127.0.0.1:18318"
LOCAL_URL = "http://127.0.0.1:8318"
BEGIN = "# >>> laptop-local-cmux >>>"
END = "# <<< laptop-local-cmux <<<"
SHELL_BLOCK = f"""{BEGIN}
# Plain claude/codex run on this Mac through the M1 gateway; `claude 1` opens an
# M1 session (CLIProxyAPI scripts/laptop-local-cmux.py; `undo` removes this).
export CLIPROXY_HOME_DEFAULT=0
[ -r "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh" ] && . "$HOME/.config/cliproxyapi-custom/daily-proxy-shell.sh"
{END}
"""
# Keys a home host action may have used to take over New Workspace.
NEW_WORKSPACE_KEYS = {"cmd+n", "cmd+t"}
BLANK_WORKSPACE = "cmux.newWorkspace"
UNBOUND = {"", "none", "clear", "unbound", "disabled"}


class Paths:
    def __init__(self, home):
        self.home = Path(home)
        self.config = self.home / ".config/cliproxyapi-custom"
        self.state = self.config / "laptop-local-cmux.json"
        self.cmux = self.home / ".config/cmux/cmux.json"
        self.zshrc = self.home / ".zshrc"
        self.agents = self.home / "Library/LaunchAgents"
        self.retired = self.home / "Library/LaunchAgents.disabled-2026-10-09"
        self.bin = self.home / ".local/bin"
        # (installed file, repository source, executable)
        self.launchers = [
            (self.bin / "claude-m1", "scripts/proxy-agent.sh", True),
            (self.bin / "codex-m1", "scripts/proxy-agent.sh", True),
            (self.bin / "claude-proxy", "scripts/proxy-agent.sh", True),
            (self.config / "daily-proxy-shell.sh", "scripts/daily-proxy-shell.sh", False),
        ]


# --- cmux.json (JSON with comments) -------------------------------------------

def strip_comments(text):
    """Remove // and /* */ comments outside strings."""
    output, index, in_string = [], 0, False
    while index < len(text):
        character = text[index]
        if in_string:
            output.append(character)
            if character == "\\" and index + 1 < len(text):
                output.append(text[index + 1])
                index += 2
                continue
            in_string = character != '"'
        elif character == '"':
            in_string = True
            output.append(character)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated /* comment")
            index = end + 2
            continue
        else:
            output.append(character)
        index += 1
    return "".join(output)


def strip_trailing_commas(text):
    """Remove commas that directly precede } or ] outside strings."""
    output, index, in_string = [], 0, False
    while index < len(text):
        character = text[index]
        if in_string:
            output.append(character)
            if character == "\\" and index + 1 < len(text):
                output.append(text[index + 1])
                index += 2
                continue
            in_string = character != '"'
        elif character == '"':
            in_string = True
            output.append(character)
        elif character == ",":
            rest = text[index + 1:].lstrip()
            if not rest.startswith(("}", "]")):
                output.append(character)
        else:
            output.append(character)
        index += 1
    return "".join(output)


def parse_jsonc(text):
    return json.loads(strip_trailing_commas(strip_comments(text)) or "{}")


def opens_home_host(name, action):
    text = json.dumps(action)
    return "mosh-tmux" in text or bool(re.match(r"m1(-|$)", name))


def home_host_actions(config):
    actions = config.get("actions")
    if not isinstance(actions, dict):
        return set()
    return {name for name, action in actions.items() if opens_home_host(name, action)}


def home_host_commands(config):
    commands = config.get("commands")
    if not isinstance(commands, list):
        return set()
    return {command.get("name") for command in commands
            if isinstance(command, dict) and "mosh-tmux" in json.dumps(command)}


def localize_cmux(config, claude_binary):
    """Point New Workspace and cmux's Claude binary at this Mac. Returns the
    changes made, in words. The home host actions themselves are kept."""
    changes = []
    actions = home_host_actions(config)
    ui = config.get("ui")
    new_workspace = ui.get("newWorkspace") if isinstance(ui, dict) else None
    if isinstance(new_workspace, dict) and new_workspace.get("action") in actions:
        changes.append(f"New Workspace (Cmd+N) opened {new_workspace['action']!r} on the home host; "
                       "it now opens a blank local terminal")
        # Explicit, so a value saved in cmux's Settings cannot bring it back.
        new_workspace["action"] = BLANK_WORKSPACE
    legacy = config.get("newWorkspaceCommand")
    if legacy and (legacy in actions or legacy in home_host_commands(config)):
        changes.append(f"Removed the legacy newWorkspaceCommand {legacy!r} (home host)")
        del config["newWorkspaceCommand"]
    took_new_workspace_key = False
    for name in sorted(actions):
        action = config["actions"][name]
        shortcut = action.get("shortcut") if isinstance(action, dict) else None
        if isinstance(shortcut, str) and shortcut.replace(" ", "").lower() in NEW_WORKSPACE_KEYS:
            changes.append(f"Action {name!r} no longer takes {shortcut}; it stays in the plus-button menu")
            del action["shortcut"]
            took_new_workspace_key = True
    bindings = config.get("shortcuts", {}).get("bindings") if isinstance(config.get("shortcuts"), dict) else None
    if took_new_workspace_key and isinstance(bindings, dict):
        for builtin in ("newTab", "newLocalWorkspace", "newWorkspace"):
            value = bindings.get(builtin, "keep")
            if value is None or (isinstance(value, str) and value.lower() in UNBOUND):
                changes.append(f"Restored cmux's default shortcut for {builtin}")
                del bindings[builtin]
    automation = config.get("automation")
    path = automation.get("claudeBinaryPath") if isinstance(automation, dict) else None
    if isinstance(path, str) and "claude-cmux-home" in path:
        automation["claudeBinaryPath"] = claude_binary
        if claude_binary:
            changes.append(f"cmux starts and restores Claude locally through the pool ({claude_binary})")
        else:
            changes.append("cmux starts and restores Claude with the claude in PATH (direct sign-in)")
    return changes


def home_host_references(config):
    """Settings outside the action registry that still open the home host."""
    found = []
    actions = home_host_actions(config)
    new_workspace = (config.get("ui") or {}).get("newWorkspace") if isinstance(config.get("ui"), dict) else None
    if isinstance(new_workspace, dict) and new_workspace.get("action") in actions:
        found.append(f"ui.newWorkspace.action = {new_workspace['action']}")
    for name in sorted(actions):
        shortcut = config["actions"][name].get("shortcut") if isinstance(config["actions"][name], dict) else None
        if isinstance(shortcut, str) and shortcut.replace(" ", "").lower() in NEW_WORKSPACE_KEYS:
            found.append(f"actions.{name}.shortcut = {shortcut}")
    automation = config.get("automation") if isinstance(config.get("automation"), dict) else {}
    if "claude-cmux-home" in str(automation.get("claudeBinaryPath", "")):
        found.append(f"automation.claudeBinaryPath = {automation['claudeBinaryPath']}")
    return found


# --- shell startup --------------------------------------------------------------

def add_shell_block(text):
    if BEGIN in text:
        return text
    return text + ("" if not text or text.endswith("\n") else "\n") + "\n" + SHELL_BLOCK


def remove_shell_block(text):
    pattern = re.compile(r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    return pattern.sub("\n", text).rstrip("\n") + "\n" if BEGIN in text else text


# --- helpers ---------------------------------------------------------------------

def say(message=""):
    print(message, flush=True)


def run(command, **options):
    return subprocess.run(command, capture_output=True, text=True, **options)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fetch_from_host(relative):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", HOST,
                             f"cat {REPOSITORY}/{relative}"], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        raise SystemExit(f"Could not read {REPOSITORY}/{relative} from the home host: "
                         f"{result.stderr.decode().strip() or 'empty file'}")
    return result.stdout


def proxy_ready(url, key_file):
    """True when the proxy at url accepts the key and lists at least one model."""
    if not key_file.is_file():
        return False
    header = "Authorization: Bearer " + key_file.read_text().strip() + "\n"
    result = subprocess.run(["curl", "-fsS", "-m", "3", "-H", "@-", url + "/v1/models"],
                            input=header, capture_output=True, text=True)
    return result.returncode == 0 and '"id"' in result.stdout


def wait_ready(url, key_file, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        if proxy_ready(url, key_file):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def launch_agent_label(plist):
    result = run(["/usr/bin/plutil", "-extract", "Label", "raw", "-o", "-", str(plist)])
    return result.stdout.strip() if result.returncode == 0 else plist.stem


def retired_agents(paths):
    """The proxy, switch and tunnel LaunchAgents retired on 2026-10-09."""
    plists = sorted(paths.retired.glob("*.plist")) if paths.retired.is_dir() else []
    agents = [(plist, launch_agent_label(plist)) for plist in plists]
    return [(plist, label) for plist, label in agents if re.search(r"proxy|tunnel", label, re.I)]


def loaded(label):
    return run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"]).returncode == 0


def find_cmux():
    found = shutil.which("cmux")
    if found:
        return found
    for app in sorted(Path("/Applications").glob("cmux*.app")):
        candidate = app / "Contents/Resources/bin/cmux"
        if candidate.is_file():
            return str(candidate)
    return None


def reload_cmux():
    cmux = find_cmux()
    if cmux and run([cmux, "reload-config"]).returncode == 0:
        return "cmux reloaded its configuration."
    return "Reload cmux's configuration (cmux > Reload Configuration, or quit and reopen cmux)."


def check_guards(paths):
    if sys.platform != "darwin":
        raise SystemExit("Run this on the laptop (macOS).")
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise SystemExit("This is an SSH session. Run it in a local terminal on the laptop, such as Terminal.app.")
    if (paths.config / "home-agent-shell.sh").is_file():
        raise SystemExit("This Mac is the home host. Run it on the laptop, in a local terminal such as Terminal.app.")
    hostname = run(["ssh", "-G", HOST]).stdout
    if not re.search(rf"^hostname (?!{HOST}$)\S+$", hostname, re.M):
        raise SystemExit(f"No `{HOST}` entry in ~/.ssh/config; this does not look like the laptop.")


# --- commands --------------------------------------------------------------------

def status(paths):
    say("Laptop terminal mode")
    say(f"  Ghostty: {'home host (m1)' if 'local/bin/m1' in ghostty_command(paths) else ghostty_command(paths) or 'default'}")
    if paths.cmux.is_file():
        try:
            config = parse_jsonc(paths.cmux.read_text())
        except ValueError as error:
            say(f"  cmux.json: cannot parse ({error})")
        else:
            references = home_host_references(config)
            new_workspace = ((config.get("ui") or {}).get("newWorkspace") or {}).get("action", "(cmux default)")
            say(f"  cmux New Workspace: {new_workspace}")
            claude_binary = (config.get("automation") or {}).get("claudeBinaryPath", "")
            say(f"  cmux Claude binary: {claude_binary or '(claude in PATH)'}")
            resume = (config.get("terminal") or {}).get("autoResumeAgentSessions", True)
            say(f"  cmux auto-resume agents: {resume}")
            computer_use = (config.get("computerUse") or {}).get("enabled", True)
            say(f"  cmux computer use: {'on' if computer_use else 'off'}")
            say(f"  cmux mode: {'home host' if references else 'local'}")
            for reference in references:
                say(f"    still home host: {reference}")
    else:
        say("  cmux.json: missing")
    zshrc = paths.zshrc.read_text() if paths.zshrc.is_file() else ""
    say(f"  ~/.zshrc local agents block: {'present' if BEGIN in zshrc else 'absent'}")
    for installed, _, _ in paths.launchers:
        say(f"  {installed.relative_to(paths.home)}: {'present' if installed.is_file() else 'MISSING'}")
    for name in ("claude", "claude-home", "codex-home", "m1"):
        say(f"  ~/.local/bin/{name}: {'present' if (paths.bin / name).exists() else 'missing'}")
    for name in ("client-api-key", "m1-client-api-key"):
        key = paths.config / name
        private = key.is_file() and not key.stat().st_mode & 0o077
        say(f"  {name}: {'present, private' if private else 'present, NOT private' if key.is_file() else 'MISSING'}")
    for _, label in retired_agents(paths):
        say(f"  LaunchAgent {label}: {'loaded' if loaded(label) else 'not loaded'}")
    say(f"  Proxy switch {SWITCH_URL}: {'ready' if proxy_ready(SWITCH_URL, paths.config / 'client-api-key') else 'not ready'}")
    say(f"  M1 tunnel {TUNNEL_URL}: {'ready' if proxy_ready(TUNNEL_URL, paths.config / 'm1-client-api-key') else 'not ready'}")
    say(f"  Laptop proxy {LOCAL_URL}: {'ready' if proxy_ready(LOCAL_URL, paths.config / 'client-api-key') else 'not ready'}")


def ghostty_command(paths):
    config = paths.home / "Library/Application Support/com.mitchellh.ghostty/config.ghostty"
    if not config.is_file():
        return ""
    match = re.search(r"^\s*command\s*=\s*(.+)$", config.read_text(), re.M)
    return match.group(1).strip() if match else ""


def apply(paths):
    check_guards(paths)
    if paths.state.is_file():
        if json.loads(paths.state.read_text()).get("complete"):
            say(f"Already applied (state: {paths.state}). Run `status`, or `undo` first to apply again.")
            return
        raise SystemExit("A previous apply stopped partway. Run `undo`, then `apply` again.")
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    paths.config.mkdir(parents=True, exist_ok=True)
    # Private (0700) and unique, even for an undo and apply within one second.
    backup = Path(tempfile.mkdtemp(prefix=f"backup-{stamp}-local-cmux-", dir=paths.config))
    state = {"backup": str(backup), "launchers": [], "agents": [], "cmux": False, "zshrc": False}
    write_state(paths, state)
    say(f"Backups: {backup}")

    # 1. Launchers from the home host's checkout, replaced only when different.
    sources = {}
    for installed, source, executable in paths.launchers:
        data = sources.setdefault(source, fetch_from_host(source))
        if installed.is_file() and digest(installed.read_bytes()) == digest(data):
            continue
        if installed.is_file():
            shutil.copy2(installed, backup / installed.name)
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(data)
        installed.chmod(0o755 if executable else 0o644)
        state["launchers"].append(str(installed))
        write_state(paths, state)
        say(f"Installed {installed.relative_to(paths.home)}")
    for name in ("client-api-key", "m1-client-api-key"):
        if not (paths.config / name).is_file():
            say(f"WARNING: {paths.config / name} is missing; the pool cannot be used until it exists.")

    # 2. The tunnel, proxy switch and fallback proxy retired on 2026-10-09.
    retired = retired_agents(paths)
    if not retired:
        say(f"WARNING: no retired proxy LaunchAgents in {paths.retired}; set up the M1 tunnel by hand.")
    paths.agents.mkdir(parents=True, exist_ok=True)
    for plist, label in retired:
        target = paths.agents / plist.name
        if not target.exists():
            shutil.copy2(plist, target)
            state["agents"].append({"label": label, "plist": str(target), "restored": True})
        elif not loaded(label):
            state["agents"].append({"label": label, "plist": str(target), "restored": False})
        else:
            continue
        write_state(paths, state)
        run(["/bin/launchctl", "enable", f"gui/{os.getuid()}/{label}"])
        started = run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)])
        say(f"Started {label}" if started.returncode == 0 or loaded(label)
            else f"WARNING: could not start {label}: {started.stderr.strip()}")
    write_state(paths, state)

    # 3. Local shells: plain claude/codex run here through the pool.
    zshrc = paths.zshrc.read_text() if paths.zshrc.exists() else ""
    if BEGIN not in zshrc:
        if paths.zshrc.exists():
            shutil.copy2(paths.zshrc, backup / "zshrc")
        paths.zshrc.write_text(add_shell_block(zshrc))
        state["zshrc"] = True
        say("~/.zshrc: plain claude/codex now run on this Mac through the M1 gateway")
    write_state(paths, state)

    # 4. cmux: New Workspace opens a local terminal; cmux's Claude runs locally.
    if paths.cmux.is_file():
        original = paths.cmux.read_text()
        config = parse_jsonc(original)
        claude = paths.bin / "claude"
        claude_binary = str(paths.bin / "claude-proxy") if claude.exists() else ""
        if not claude.exists():
            say("NOTE: ~/.local/bin/claude is missing, so cmux uses the claude in PATH (direct sign-in).")
        changes = localize_cmux(config, claude_binary)
        if changes:
            shutil.copy2(paths.cmux, backup / "cmux.json")
            header = (f"// Edited by CLIProxyAPI scripts/laptop-local-cmux.py on {stamp}; "
                      f"the previous file is {backup / 'cmux.json'}.\n")
            written = header + json.dumps(config, indent=2, ensure_ascii=False) + "\n"
            parse_jsonc(written)
            paths.cmux.write_text(written)
            state["cmux"] = True
            for change in changes:
                say(f"cmux: {change}")
            say(reload_cmux())
        else:
            say("cmux: nothing pointed at the home host.")
        for reference in home_host_references(config):
            say(f"cmux: still opens the home host, please check: {reference}")
    else:
        say(f"NOTE: {paths.cmux} does not exist; open cmux once, then run undo and apply again.")
    state["complete"] = True
    write_state(paths, state)
    say()
    verify(paths)


def write_state(paths, state):
    paths.state.write_text(json.dumps(state, indent=2) + "\n")
    paths.state.chmod(0o600)


def verify(paths, ask_models=True):
    say("Checks")
    switch = wait_ready(SWITCH_URL, paths.config / "client-api-key")
    tunnel = wait_ready(TUNNEL_URL, paths.config / "m1-client-api-key")
    say(f"  proxy switch: {'ok' if switch else 'NOT READY'}")
    say(f"  M1 tunnel: {'ok' if tunnel else 'NOT READY'}")
    try:
        shell = run(["/bin/zsh", "-ic", "whence -f claude codex"], stdin=subprocess.DEVNULL, timeout=30).stdout
    except subprocess.TimeoutExpired:
        shell = ""
    defined = "claude-m1" in shell and "codex-m1" in shell
    say(f"  new zsh: claude and codex {'run locally through the pool' if defined else 'are NOT the pool functions'}")
    if not ask_models or not (switch or tunnel):
        return
    environment = {**os.environ, "CLIPROXY_HOME_DEFAULT": "0"}
    with tempfile.TemporaryDirectory() as directory:
        checks = [
            ("claude", [str(paths.bin / "claude-m1"), "-p", "Reply with exactly: ok"]),
            ("codex", [str(paths.bin / "codex-m1"), "exec", "--skip-git-repo-check", "Reply with exactly: ok"]),
        ]
        for name, command in checks:
            try:
                result = run(command, cwd=directory, env=environment, stdin=subprocess.DEVNULL, timeout=180)
                answered = result.returncode == 0 and "ok" in result.stdout.lower()
                lines = (result.stderr or result.stdout).strip().splitlines()
                detail = "" if answered else f" ({lines[-1] if lines else 'no output'})"
            except subprocess.TimeoutExpired:
                answered, detail = False, " (no answer in 3 minutes)"
            say(f"  {name} on this Mac through the pool: {'answered' if answered else 'FAILED'}{detail}")


def undo(paths):
    check_guards(paths)
    if not paths.state.is_file():
        raise SystemExit("Nothing to undo: no state file from apply.")
    state = json.loads(paths.state.read_text())
    backup = Path(state["backup"])
    if state.get("cmux") and (backup / "cmux.json").is_file():
        shutil.copy2(paths.cmux, backup / "cmux.after-apply.json")
        shutil.copy2(backup / "cmux.json", paths.cmux)
        say("cmux.json restored. " + reload_cmux())
    if state.get("zshrc") and paths.zshrc.is_file():
        paths.zshrc.write_text(remove_shell_block(paths.zshrc.read_text()))
        say("~/.zshrc: removed the local agents block")
    for agent in state.get("agents", []):
        run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{agent['label']}"])
        if agent.get("restored"):
            Path(agent["plist"]).unlink(missing_ok=True)
        say(f"Stopped {agent['label']}")
    for installed in map(Path, state.get("launchers", [])):
        previous = backup / installed.name
        if previous.is_file():
            shutil.copy2(previous, installed)
            say(f"Restored the previous {installed.relative_to(paths.home)}")
    paths.state.rename(backup / "state.json")
    say(f"Undone. Backups remain in {backup}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "apply", "undo", "verify"])
    options = parser.parse_args()
    paths = Paths(Path.home())
    {"status": status, "apply": apply, "undo": undo, "verify": verify}[options.command](paths)


if __name__ == "__main__":
    main()
