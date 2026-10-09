#!/usr/bin/env python3
"""Tab sidebar for the home host's terminal tabs (tmux server `-L tabs`).

Every tab is a tmux window in the session group "main". Each window carries a
narrow pane on its left that lists all tabs, like cmux's vertical tabs. Each
terminal window is its own grouped session, so two windows can show different
tabs of the same list.

  run      draw the list in this pane (started by `ensure`); click a tab to
           show it, right-click for rename/move/close, click "New tab"
  ensure   adopt sessions left by the one-session-per-tab layout, give every
           tab a sidebar at the right width, then redraw every sidebar
  refresh  redraw every sidebar now
"""

import fcntl
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import unicodedata

GROUP = "main"
WIDTH = int(os.environ.get("M1_SIDEBAR_WIDTH", "28"))
SHELLS = {"zsh", "-zsh", "bash", "-bash", "sh", "fish", "login"}
SEP = "\x1f"
PANE_FIELDS = [
    "window_id", "window_index", "window_active_clients", "window_bell_flag",
    "automatic-rename", "window_name", "window_width", "pane_id", "@sidebar",
    "pane_active", "pane_dead", "pane_width", "pane_left", "pane_top", "pane_title",
    "pane_current_path", "pane_current_command", "host", "host_short",
]

# Colors assume the dark theme the tabs server already uses (~/.tmux.conf).
RESET = "\x1b[0m"
ACTIVE_BG = "\x1b[48;5;236m"
ACCENT = "\x1b[38;2;95;135;175m"
DIM = "\x1b[38;5;244m"
BOLD = "\x1b[1m"
BELL = "\x1b[38;2;230;180;80m"


def tmux_run(*args):
    """Run a tmux command on the tabs server."""
    base = [shutil.which("tmux") or "/opt/homebrew/bin/tmux"]
    # Inside the server (panes, hooks) $TMUX names the socket; outside, use the name.
    if not os.environ.get("TMUX"):
        base += ["-L", os.environ.get("M1_TABS_SOCKET", "tabs")]
    return subprocess.run(base + list(args), capture_output=True, text=True)


def tmux(*args, check=False):
    """Run a tmux command on the tabs server and return its stdout."""
    result = tmux_run(*args)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"tmux {args[0]} failed")
    return result.stdout


def list_panes(target):
    """All panes of the session holding `target`, as dicts keyed by PANE_FIELDS."""
    fmt = SEP.join("#{%s}" % field for field in PANE_FIELDS)
    rows = []
    for line in tmux("list-panes", "-s", "-t", target, "-F", fmt, check=True).splitlines():
        values = line.split(SEP)
        if len(values) == len(PANE_FIELDS):
            rows.append(dict(zip(PANE_FIELDS, values)))
    return rows


def is_sidebar(pane):
    return pane["@sidebar"] == "1"


def truthy(value):
    return value in ("1", "on")


def windows_of(panes):
    """Group panes by window, in tab order."""
    windows = {}
    for pane in panes:
        windows.setdefault(pane["window_id"], []).append(pane)
    return sorted(windows.values(), key=lambda group: int(group[0]["window_index"]))


def main_pane(group):
    """The pane a tab is about: the active non-sidebar pane, else the first one."""
    others = [pane for pane in group if not is_sidebar(pane)]
    active = [pane for pane in others if pane["pane_active"] == "1"]
    return (active or others or [None])[0]


def short_path(path, home=None):
    home = home or os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def tab_label(group, home=None):
    """(title, detail) for one tab."""
    pane = main_pane(group)
    if pane is None:
        return "", ""
    path = short_path(pane["pane_current_path"], home)
    command = pane["pane_current_command"]
    title = pane["pane_title"].strip()
    if not truthy(pane["automatic-rename"]):
        title = pane["window_name"]  # Renamed by hand; keep that name.
    elif not title or title in (pane["host"], pane["host_short"]):
        # Shells leave the default title (the host name); name the tab by its folder.
        title = path.rsplit("/", 1)[-1] or "~" if command in SHELLS else command
    # The second line shows where the tab is; say what runs there when the title already does.
    return title, (path if path != title else command)


def cell_width(char):
    if unicodedata.combining(char) or char in "​‍︎️":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit(text, width, keep="start"):
    """Pad or cut `text` to exactly `width` terminal cells, marking a cut with an ellipsis."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    total = sum(cell_width(char) for char in text)
    if total <= width:
        return text + " " * (width - total)
    if width <= 0:
        return ""
    chars = list(text) if keep == "start" else list(reversed(text))
    out, used = [], 0
    for char in chars:
        w = cell_width(char)
        if used + w > width - 1:
            break
        out.append(char)
        used += w
    body = "".join(out) if keep == "start" else "".join(reversed(out))
    pad = " " * (width - 1 - used)
    return body + "…" + pad if keep == "start" else "…" + body + pad


def render(tabs, current, width, height):
    """Lines for the sidebar plus a map of row -> window id ("new" for the New tab row).

    `tabs` holds (window_id, index, title, detail, bell) in tab order.
    """
    lines, rows = [""], {}
    roomy = 1 + len(tabs) * 3 + 1 <= height
    for window_id, index, title, detail, bell in tabs:
        active = window_id == current
        bg = ACTIVE_BG if active else ""
        bar = f"{ACCENT}▌{RESET}{bg}" if active else " "
        number = f"{BELL}●{RESET}{bg}" if bell and not active else f"{DIM}{index}{RESET}{bg}" if index < 10 else " "
        name = fit(title, width - 4)
        name = f"{BOLD}{name}{RESET}{bg}" if active else name
        rows[len(lines)] = window_id
        lines.append(f"{bg}{bar}{number}  {name}{RESET}")
        if roomy:
            rows[len(lines)] = window_id
            lines.append(f"{bg}{bar}   {DIM}{fit(detail, width - 4, keep='end')}{RESET}")
            lines.append("")
    if len(lines) < height:
        rows[len(lines)] = "new"
        lines.append(f"{DIM}{fit('  + New tab', width - 3)}⌘N{RESET}")
    return lines[:height], rows


class Sidebar:
    """The long-running process in a sidebar pane."""

    def __init__(self):
        self.pane = os.environ["TMUX_PANE"]
        self.rows = {}
        self.panes = []
        self.last_frame = None

    def window_panes(self):
        mine = [pane for pane in self.panes if pane["pane_id"] == self.pane]
        if not mine:
            return []
        return [pane for pane in self.panes if pane["window_id"] == mine[0]["window_id"]]

    def draw(self):
        """Redraw; return seconds until the next poll, or None when this tab has closed."""
        self.panes = list_panes(self.pane)
        own = self.window_panes()
        if not any(not is_sidebar(pane) for pane in own):
            tmux("kill-pane", "-t", self.pane)  # Only the sidebar is left: close the tab.
            return None
        current = own[0]["window_id"]
        tabs = []
        for group in windows_of(self.panes):
            title, detail = tab_label(group)
            tabs.append((group[0]["window_id"], int(group[0]["window_index"]), title, detail,
                         truthy(group[0]["window_bell_flag"])))
        width, height = os.get_terminal_size(sys.stdout.fileno())
        lines, self.rows = render(tabs, current, width, height)
        frame = "".join(f"\x1b[{row + 1};1H{line}\x1b[K" for row, line in enumerate(lines)) + "\x1b[J"
        if frame != self.last_frame:
            sys.stdout.write("\x1b[H" + frame)
            sys.stdout.flush()
            self.last_frame = frame
        # Poll often only while someone is looking; hooks signal structural changes.
        return 1.0 if int(own[0]["window_active_clients"] or 0) > 0 else 5.0

    def clicking_client(self):
        """The client that clicked: the most recently active one showing this tab."""
        own = self.window_panes()
        if not own:
            return None
        fmt = SEP.join(["#{client_activity}", "#{client_name}", "#{session_id}", "#{window_id}"])
        clients = []
        for line in tmux("list-clients", "-F", fmt).splitlines():
            activity, name, session, window = (line.split(SEP) + ["", "", "", ""])[:4]
            if window == own[0]["window_id"]:
                clients.append((int(activity or 0), name, session))
        return max(clients)[1:] if clients else None

    def show(self, session, window_id):
        tmux("select-window", "-t", f"{session}:{window_id}")
        group = [pane for pane in self.panes if pane["window_id"] == window_id]
        pane = main_pane(group)
        if pane is not None and any(is_sidebar(p) and p["pane_active"] == "1" for p in group):
            tmux("select-pane", "-t", pane["pane_id"])

    def menu(self, client, window_id, x, y):
        groups = windows_of(self.panes)
        ids = [group[0]["window_id"] for group in groups]
        group = groups[ids.index(window_id)]
        title, _ = tab_label(group)
        renamed = not truthy(group[0]["automatic-rename"])
        position = ids.index(window_id)
        quoted = title.replace("\\", "\\\\").replace('"', '\\"')
        items = ["Rename…", "r", f'command-prompt -I "{quoted}" -p "Tab name:" '
                 f'{{ rename-window -t {window_id} -- "%%" }}']
        if renamed:
            items += ["Use automatic name", "a", f"set -w -t {window_id} automatic-rename on"]
        if position > 0:
            items += ["Move up", "u", f"swap-window -d -s {window_id} -t {ids[position - 1]}"]
        if position < len(ids) - 1:
            items += ["Move down", "d", f"swap-window -d -s {window_id} -t {ids[position + 1]}"]
        items += ["", "", "", "Close tab", "x", f"kill-window -t {window_id}"]
        height = sum(1 for i in range(0, len(items), 3)) + 2
        own = self.window_panes()[0]
        left = int(own["pane_left"]) + x
        top = int(own["pane_top"]) + y
        # A numeric -y is the menu's bottom edge.
        tmux("display-menu", "-c", client, "-t", self.pane, "-x", str(left), "-y", str(top + height),
             "-T", "#[align=centre]Tab", *items)

    def handle_mouse(self, data):
        for button, x, y, kind in re.findall(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", data):
            button, x, y = int(button), int(x) - 1, int(y) - 1
            if kind != b"M" or button & (32 | 64):  # Presses only; no drags or wheel.
                continue
            target = self.rows.get(y)
            client = self.clicking_client()
            if target is None or client is None:
                continue
            name, session = client
            if target == "new":
                tmux("new-window", "-t", f"{session}:")
            elif button & 3 == 0:
                self.show(session, target)
            elif button & 3 == 2:
                self.menu(name, target, x, y)

    def run(self):
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        for sig in (signal.SIGUSR1, signal.SIGWINCH):
            signal.signal(sig, lambda *_: None)
        # `refresh` signals only sidebars that have published their pid, so a
        # sidebar still starting up is never killed by an early SIGUSR1.
        tmux("set-option", "-p", "-t", self.pane, "@sidebar_pid", str(os.getpid()))
        stdin = sys.stdin.fileno()
        if os.isatty(stdin):
            attrs = termios.tcgetattr(stdin)
            attrs[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(stdin, termios.TCSANOW, attrs)
        # Hide the cursor, stop line wrap, and ask for mouse presses (SGR encoding).
        sys.stdout.write("\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[2J")
        sys.stdout.flush()
        while True:
            try:
                wait = self.draw()
            except Exception as error:  # Keep the pane alive; a crash would only respawn it.
                sys.stdout.write(f"\x1b[H\x1b[2J{DIM}sidebar error:\r\n{fit(str(error), 60)}{RESET}")
                sys.stdout.flush()
                self.last_frame, wait = None, 5.0
            if wait is None:
                return
            ready, _, _ = select.select([wake_r, stdin], [], [], wait)
            if wake_r in ready:
                os.read(wake_r, 512)
            if stdin in ready:
                data = os.read(stdin, 4096)
                if not data:
                    return
                try:
                    self.handle_mouse(data)
                except Exception:
                    pass


def sidebar_command():
    return f"exec {sys.executable} -I {Path(__file__).resolve()} run"


def ensure():
    lock_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"m1-sidebar.{os.getuid()}.lock"
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_locked()
    refresh()


def ensure_locked():
    if tmux_run("has-session", "-t", f"={GROUP}").returncode != 0:
        return
    # Tab sessions from the one-session-per-tab layout that no terminal is showing
    # (it reattached them one per tab) join the list; their programs keep running.
    fmt = SEP.join(["#{session_attached}", "#{session_name}"])
    for line in tmux("list-sessions", "-F", fmt).splitlines():
        attached, name = (line.split(SEP) + [""])[:2]
        if attached == "0" and name.startswith("t-"):
            for window_id in tmux("list-windows", "-t", f"={name}", "-F", "#{window_id}").split():
                tmux("move-window", "-s", window_id, "-t", f"={GROUP}:")
    for group in windows_of(list_panes(f"={GROUP}")):
        bars = [pane for pane in group if is_sidebar(pane)]
        window_id, window_width = group[0]["window_id"], int(group[0]["window_width"])
        width = min(WIDTH, max(12, window_width // 4))
        if not bars:
            # Mark the pane before the sidebar starts. remain-on-exit keeps a crashed
            # sidebar on screen with its error instead of looping through respawns.
            pane_id = tmux("split-window", "-fhbd", "-l", str(width), "-t", window_id,
                           "-P", "-F", "#{pane_id}", "exec cat").strip()
            if pane_id:
                tmux("set-option", "-p", "-t", pane_id, "@sidebar", "1", ";",
                     "set-option", "-p", "-t", pane_id, "remain-on-exit", "on", ";",
                     "respawn-pane", "-k", "-t", pane_id, sidebar_command())
            continue
        for extra in bars[1:]:
            tmux("kill-pane", "-t", extra["pane_id"])
        if bars[0]["pane_dead"] == "1":
            tmux("respawn-pane", "-k", "-t", bars[0]["pane_id"], sidebar_command())
        if int(bars[0]["pane_width"]) != width:
            tmux("resize-pane", "-t", bars[0]["pane_id"], "-x", str(width))


def refresh():
    live = "#{?pane_dead,,#{?#{==:#{pane_pid},#{@sidebar_pid}},#{pane_pid},}}"
    for pid in tmux("list-panes", "-a", "-F", live).split():
        try:
            os.kill(int(pid), signal.SIGUSR1)
        except (ProcessLookupError, ValueError):
            pass


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "run":
        Sidebar().run()
    elif command == "ensure":
        ensure()
    elif command == "refresh":
        refresh()
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
