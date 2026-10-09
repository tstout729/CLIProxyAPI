#!/usr/bin/env python3
"""Laptop: run the terminal's command (mosh to the home host) and upload dropped files.

Ghostty turns a dropped file, such as a screenshot dragged from its thumbnail
or from Finder, into a paste of its escaped local path. The home host cannot
open that path. This program sits between the terminal and the command: when
a paste holds nothing but paths to local files, it copies each file to the
home host (~/Drops/<date>/) and passes on the home host's paths instead, so
Claude Code and Codex there attach the image. Everything else passes through
unchanged, and a failed upload passes the original paste through.

usage: m1-drop-bridge [--] command [args...]

Environment: M1_DROP_HOST (ssh host, default "home"), M1_DROP_REMOTE_HOME
(home folder on that host, default this Mac's), M1_DROP_LOG (log file).
"""

import datetime
import fcntl
import os
from pathlib import Path
import queue
import select
import shlex
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
# Ghostty's Shell.escape (macos/Sources/Ghostty/Ghostty.Shell.swift) prefixes these with a backslash.
ESCAPED = "\\ ()[]{}<>\"'`!#$&;|*?\t"
MAX_PASTE = 64 * 1024
MAX_FILE = 512 * 1024 * 1024
HOST = os.environ.get("M1_DROP_HOST", "home")
REMOTE_HOME = os.environ.get("M1_DROP_REMOTE_HOME", str(Path.home()))
CONTROL_PATH = str(Path.home() / ".ssh" / "cm-m1-drop-%C")


def log(message):
    path = os.environ.get("M1_DROP_LOG", str(Path.home() / "Library/Logs/m1-drop-bridge.log"))
    try:
        with open(path, "a") as handle:
            handle.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def escape(path):
    return "".join("\\" + char if char in ESCAPED else char for char in path)


def split_paths(text):
    """Absolute paths in a Ghostty-escaped, space-separated drop, or None if it is not one."""
    paths, current, chars = [], [], iter(text)
    for char in chars:
        if char == "\\":
            current.append(next(chars, ""))
        elif char == " ":
            if current:
                paths.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        paths.append("".join(current))
    if not paths or any(not path.startswith("/") or "\n" in path or "\r" in path for path in paths):
        return None
    return paths


def droppable(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) <= MAX_FILE
    except OSError:
        return False


def ssh_upload(local_path):
    """Copy one file to the home host and return its path there."""
    now = datetime.datetime.now()
    relative = f"Drops/{now:%Y-%m-%d}/{now:%H%M%S}-{os.path.basename(local_path)}"
    folder = os.path.dirname(relative)
    command = (f"mkdir -p {shlex.quote(folder)} && cat > {shlex.quote(relative + '.part')} "
               f"&& mv {shlex.quote(relative + '.part')} {shlex.quote(relative)}")
    # A shared connection makes uploads after the first quick; a stale one is retried fresh.
    for control in (["-o", "ControlMaster=auto", "-o", f"ControlPath={CONTROL_PATH}", "-o", "ControlPersist=300"],
                    ["-o", "ControlPath=none"]):
        with open(local_path, "rb") as source:
            try:
                result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                         "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                                         *control, HOST, command],
                                        stdin=source, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                continue
        if result.returncode == 0:
            return f"{REMOTE_HOME}/{relative}"
        log(f"upload failed ({result.returncode}): {result.stderr.decode(errors='replace').strip()}")
    raise RuntimeError(f"could not upload {local_path}")


def rewrite(text, upload=ssh_upload, exists=droppable):
    """The paste with local files swapped for uploaded copies, or None to leave it alone."""
    paths = split_paths(text)
    if paths is None or not any(exists(path) for path in paths):
        return None
    out = []
    for path in paths:
        if exists(path):
            try:
                remote = upload(path)
                log(f"uploaded {path} -> {remote}")
                path = remote
            except Exception as error:
                log(f"keeping {path}: {error}")
        out.append(escape(path))
    return " ".join(out)


class InputFilter:
    """Splits terminal input into bytes to forward now and drops to rewrite first.

    feed() returns (forward, drop): `forward` goes to the command at once;
    `drop` is the text of a paste to rewrite. Input that arrives while a drop
    uploads is held; finish() returns the rewritten paste plus that input
    (and possibly the next drop).
    """

    def __init__(self, looks_droppable=lambda text: True):
        self.looks_droppable = looks_droppable
        self.tail = b""          # possible start of a paste marker, split across reads
        self.paste = None        # bytes of a paste being collected
        self.pending = None      # True/False (bracketed or not) while a drop uploads
        self.held = b""          # input that arrived during an upload

    def feed(self, data):
        if self.pending is not None:
            self.held += data
            return b"", None
        data, self.tail = self.tail + data, b""
        forward = b""
        while data:
            if self.paste is not None:
                end = data.find(PASTE_END)
                if end < 0:
                    self.paste += data
                    if len(self.paste) > MAX_PASTE or not self.paste.startswith(b"/"):
                        # An ordinary paste: pass it on; its end marker follows as plain bytes.
                        forward += PASTE_START + self.paste
                        self.paste = None
                    break
                text, data, self.paste = self.paste + data[:end], data[end + len(PASTE_END):], None
                decoded = text.decode("utf-8", "replace")
                if text.startswith(b"/") and self.looks_droppable(decoded):
                    self.pending, self.held = True, data
                    return forward, decoded
                forward += PASTE_START + text + PASTE_END
                continue
            start = data.find(PASTE_START)
            if start < 0:
                keep = self._marker_prefix(data)
                if keep:
                    data, self.tail = data[:-keep], data[-keep:]
                # A drop when the program did not ask for bracketed paste arrives as one bare chunk.
                if not forward and data.startswith(b"/") and len(data) > 1 and b"\x1b" not in data:
                    decoded = data.decode("utf-8", "replace")
                    if self.looks_droppable(decoded):
                        self.pending, self.held = False, self.tail
                        self.tail = b""
                        return b"", decoded
                forward += data
                break
            forward += data[:start]
            data = data[start + len(PASTE_START):]
            self.paste = b""
        return forward, None

    @staticmethod
    def _marker_prefix(data):
        # A lone Escape is a key press, never held; longer marker starts wait briefly.
        for size in range(min(len(PASTE_START) - 1, len(data)), 1, -1):
            if PASTE_START.startswith(data[-size:]):
                return size
        return 0

    def flush_tail(self):
        tail, self.tail = self.tail, b""
        return tail

    def finish(self, original, rewritten):
        text = (rewritten if rewritten is not None else original).encode("utf-8")
        out = PASTE_START + text + PASTE_END if self.pending else text
        held, self.pending, self.held = self.held, None, b""
        forward, drop = self.feed(held)
        return out + forward, drop


def looks_droppable(text):
    paths = split_paths(text)
    if paths is None:
        return False
    if any(droppable(path) for path in paths):
        return True
    for path in paths:
        try:
            os.stat(path)
        except OSError as error:
            # Privacy settings (Desktop, temporary screenshot files) show up here as EPERM.
            log(f"cannot read dropped {path}: {error}")
    return False


def copy_size(source_fd, target_fd):
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def write_all(fd, data):
    while data:
        try:
            data = data[os.write(fd, data):]
        except BlockingIOError:
            select.select([], [fd], [])


def spawn(command, size_from):
    """Start `command` on a new pseudo-terminal that already has the terminal's size."""
    master, slave = os.openpty()
    copy_size(size_from, slave)  # mosh fails on a zero-size terminal, so size it before exec
    pid = os.fork()
    if pid == 0:
        try:
            os.close(master)
            os.login_tty(slave)
            os.execvp(command[0], command)
        finally:
            os._exit(127)
    os.close(slave)
    return pid, master


def run(command):
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    pid, master = spawn(command, stdin)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    hangup = []
    signal.signal(signal.SIGWINCH, lambda *_: copy_size(stdin, master))
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda number, _: hangup.append(number))
    saved = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    if saved is not None:
        tty.setraw(stdin)
    flt = InputFilter(looks_droppable)
    results = queue.Queue()
    stdin_open, deadline = True, None

    def start_upload(text):
        def work():
            try:
                rewritten = rewrite(text)
            except Exception as error:
                log(f"drop failed: {error}")
                rewritten = None
            results.put((text, rewritten))
            os.write(wake_w, b"x")
        threading.Thread(target=work, daemon=True).start()

    def close_input(sig=signal.SIGHUP):
        nonlocal stdin_open, deadline
        stdin_open = False
        deadline = deadline or time.monotonic() + 5
        try:
            os.kill(pid, sig)  # mosh then ends the session cleanly
        except ProcessLookupError:
            pass

    try:
        while True:
            if hangup:
                close_input(hangup.pop())
            while not results.empty():
                original, rewritten = results.get()
                forward, drop = flt.finish(original, rewritten)
                write_all(master, forward)
                if drop is not None:
                    start_upload(drop)
            if deadline and time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
            readers = [master, wake_r] + ([stdin] if stdin_open else [])
            timeout = 0.05 if flt.tail else (1.0 if deadline else None)
            try:
                ready, _, _ = select.select(readers, [], [], timeout)
            except InterruptedError:
                continue
            if not ready and flt.tail:
                write_all(master, flt.flush_tail())
            if wake_r in ready:
                os.read(wake_r, 512)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                write_all(stdout, data)
            if stdin in ready:
                data = os.read(stdin, 65536)
                if not data:
                    close_input()
                    continue
                forward, drop = flt.feed(data)
                if forward:
                    write_all(master, forward)
                if drop is not None:
                    start_upload(drop)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin, termios.TCSAFLUSH, saved)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main(argv):
    command = argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return run(command)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
