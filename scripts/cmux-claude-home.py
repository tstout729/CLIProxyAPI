#!/usr/bin/env python3
"""cmux's Claude binary: run every Claude session cmux starts on the home host.

cmux launches Claude itself when it restores a tab, wakes a hibernated agent,
or opens an agent tab, passing `--settings <hooks>` and `--session-id ID` or
`--resume ID`. Install this as ~/.local/bin/claude-cmux-home and set
`automation.claudeBinaryPath` in ~/.config/cmux/cmux.json to it. Each
conversation gets one home session named after its ID, so a later restore or
wake reattaches to the agent that kept running on the host instead of starting
a second copy. Only keystrokes and screen updates cross the network.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HOME = Path.home()
UUID = re.compile(r"[0-9a-fA-F-]{8,64}")


def split_arguments(arguments):
    """Drop cmux's --settings (its hooks call this Mac's cmux socket) and find
    the conversation ID cmux assigned or is resuming."""
    kept, conversation = [], None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--settings" and index + 1 < len(arguments):
            index += 2
            continue
        if argument.startswith("--settings="):
            index += 1
            continue
        for flag in ("--resume", "-r", "--session-id"):
            if argument == flag and index + 1 < len(arguments) and UUID.fullmatch(arguments[index + 1]):
                conversation = arguments[index + 1]
            elif argument.startswith(flag + "=") and UUID.fullmatch(argument.split("=", 1)[1]):
                conversation = argument.split("=", 1)[1]
        kept.append(argument)
        index += 1
    return kept, conversation


def session_name(conversation):
    if conversation:
        return "cmux-" + conversation.lower()
    return "cmux-" + time.strftime("%m%d-%H%M%S")


def project_directory(cwd):
    """The repository's main checkout under $HOME, matching plain `claude`."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    directory = common[:-len("/.git")] if common.endswith("/.git") else git("rev-parse", "--show-toplevel")
    if directory and Path(directory).is_relative_to(HOME) and Path(directory) != HOME:
        return directory
    return str(HOME)


def sync_transcript(conversation, host):
    """Copy this Mac's saved conversation to the host so it can be resumed there.
    Newer copies on the host are kept."""
    projects = HOME / ".claude/projects"
    paths = []
    for transcript in projects.glob(f"*/{conversation}.jsonl"):
        paths.append(transcript.relative_to(projects))
        if (transcript.parent / conversation).is_dir():
            paths.append((transcript.parent / conversation).relative_to(projects))
    if not paths:
        return
    subprocess.run(
        ["rsync", "-a", "--update", "--relative", *[f"./{path}" for path in paths], f"{host}:.claude/projects/"],
        cwd=projects, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
    )


def host_reachable(host):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def main():
    arguments = sys.argv[1:]
    host = os.environ.get("HOME_CODING_SSH_ALIAS", "home")
    launcher = HOME / ".local/bin/claude-home"
    # Non-interactive calls (version checks, print mode) stay on this Mac.
    local_only = not sys.stdin.isatty() or any(
        argument in ("-p", "--print", "-v", "--version", "-h", "--help") for argument in arguments
    )
    if local_only or not launcher.exists() or not host_reachable(host):
        if not local_only:
            print("The home host is unreachable, so this Claude session runs on this Mac.", file=sys.stderr, flush=True)
        os.environ["CLIPROXY_ROUTE"] = "direct"
        binary = os.environ.get("CLIPROXY_CLAUDE_BIN", str(HOME / ".local/bin/claude"))
        os.execv(binary, [binary, *arguments])
        return
    kept, conversation = split_arguments(arguments)
    resuming = any(argument in ("--resume", "-r") or argument.startswith("--resume=") for argument in arguments)
    if conversation and resuming:
        try:
            sync_transcript(conversation, host)
        except (OSError, subprocess.SubprocessError):
            pass
    command = [str(launcher), "--fresh", session_name(conversation), project_directory(Path.cwd()), "--", *kept]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
