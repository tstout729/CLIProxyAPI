#!/usr/bin/env python3
"""Tab sidebar for the home host's terminal tabs (tmux server `-L tabs`).

Every tab is a tmux window in the session group "main". Each window carries a
narrow pane on its left that lists all tabs, like cmux's vertical tabs, with
what each agent is doing: working, needs you, or finished while you were
away. Each terminal window is its own grouped session, so two windows can
show different tabs of the same list.

  run           draw the list in this pane (started by `ensure`). Click a tab
                to show it; click its x or middle-click it to close it;
                double-click to rename; drag to reorder; right-click for more;
                click "New tab"
  ensure        adopt sessions left by the one-session-per-tab layout, give
                every tab a sidebar at the right width, then redraw them all
  refresh       redraw every sidebar now
  restart       restart every sidebar (after installing a new version)
  rename CLIENT ask CLIENT for a new name for the tab it shows
  jump CLIENT   show CLIENT the next tab that needs you or has finished
"""

from collections import namedtuple
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
import time
import unicodedata

GROUP = "main"
WIDTH = int(os.environ.get("M1_SIDEBAR_WIDTH", "32"))
SHELLS = {"zsh", "-zsh", "bash", "-bash", "sh", "fish", "login"}
# Claude Code's native binary is named after its version (tmux shows 2_1_296).
AGENTS = re.compile(r"^(claude|codex|\d+[._]\d+[._]\d+)$")
# Claude Code and Codex show these in their footer while a turn runs or waits on agents.
WORKING = re.compile(r"esc to interrupt|Waiting for \d+ background agents?", re.I)
# Footers of permission prompts and questions waiting on an answer.
ASKING = re.compile(r"esc to cancel|enter to select|enter to confirm", re.I)
SEP = "\x1f"
MARK = "\x1e"
PANE_FIELDS = [
    "window_id", "window_index", "window_active_clients", "window_bell_flag",
    "automatic-rename", "window_name", "window_width", "pane_id", "@sidebar",
    "pane_active", "pane_dead", "pane_width", "pane_left", "pane_top", "pane_title",
    "pane_current_path", "pane_current_command", "host", "host_short",
    "@sidebar_styled", "@m1_state", "@m1_since", "@m1_unread", "@m1_leaving",
]
# A working tab must look idle this long before it counts as finished: the
# footer can drop "esc to interrupt" for a moment between steps.
SETTLE = 3.0
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DOUBLE_CLICK = 0.4
FRAME = 0.12


def rgb(color, ground=38):
    red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"\x1b[{ground};2;{red};{green};{blue}m"


# Colors suit Ghostty's default dark theme (background #282c34): the list sits
# on a slightly darker panel, like an editor's sidebar.
PANEL = "#21252b"
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
ACTIVE_BG = rgb("#2f3440", 48)
HOVER_BG = rgb("#282c34", 48)
TEXT = rgb("#abb2bf")
BRIGHT = rgb("#e8eaed")
MUTED = rgb("#6b7280")
FAINT = rgb("#4b5263")
BLUE = rgb("#61afef")
GREEN = rgb("#98c379")
YELLOW = rgb("#e5c07b")
RED = rgb("#e06c75")

Tab = namedtuple("Tab", "window_id index title kind state unread bell since path command")


def tmux_base():
    base = [shutil.which("tmux") or "/opt/homebrew/bin/tmux"]
    # Inside the server (panes, hooks) $TMUX names the socket; outside, use the name.
    if not os.environ.get("TMUX"):
        base += ["-L", os.environ.get("M1_TABS_SOCKET", "tabs")]
    return base


def tmux_run(*args):
    """Run a tmux command on the tabs server."""
    return subprocess.run(tmux_base() + list(args), capture_output=True, text=True)


CHILDREN = []


def tmux_spawn(*args):
    """Start a tmux command without waiting: menus and prompts return only once dismissed."""
    CHILDREN.append(subprocess.Popen(tmux_base() + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    CHILDREN[:] = [child for child in CHILDREN if child.poll() is None]


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


def snapshot(pane_ids):
    """(screen text by pane id, ids of windows a focused terminal shows), in one tmux call."""
    args = ["list-clients", "-F", f"{MARK}client{SEP}#{{client_flags}}{SEP}#{{window_id}}"]
    for pane_id in pane_ids:
        # display-message also runs strftime over its text, so take the id from #{pane_id}.
        args += [";", "display-message", "-p", "-t", pane_id, f"{MARK}pane{SEP}#{{pane_id}}", ";",
                 "capture-pane", "-p", "-J", "-t", pane_id]
    screens, focused, current = {}, set(), None
    # Split on newlines only: str.splitlines() also breaks lines at MARK.
    for line in tmux(*args).split("\n"):
        if line.startswith(MARK):
            kind, *values = line[1:].split(SEP)
            if kind == "client" and len(values) == 2 and "focused" in values[0].split(","):
                focused.add(values[1])
            current = values[0] if kind == "pane" and values else None
            if current is not None:
                screens[current] = []
        elif current is not None:
            screens[current].append(line)
    return {pane_id: "\n".join(lines) for pane_id, lines in screens.items()}, focused


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


def kind_of(pane):
    command = pane["pane_current_command"]
    if command in SHELLS:
        return "shell"
    if AGENTS.match(command) or pane["pane_title"].startswith("✳"):
        return "agent"
    return "program"


def tab_title(group, home=None):
    """The name shown for a tab: a name given by hand, the agent's title, or the shell's folder."""
    pane = main_pane(group)
    if pane is None:
        return ""
    if not truthy(pane["automatic-rename"]):
        return pane["window_name"]
    # Agents put a status glyph in front of their title ("✳ Fix invoices"); the sidebar shows status itself.
    title = re.sub(r"^[^\w\s~/.]{1,2}\s+", "", pane["pane_title"].strip())
    if title and title not in (pane["host"], pane["host_short"]):
        return title
    if pane["pane_current_command"] in SHELLS:
        return short_path(pane["pane_current_path"], home).rsplit("/", 1)[-1] or "~"
    return pane["pane_current_command"]


def observe(screen):
    """What an agent's screen says it is doing, or None when the screen is unknown."""
    if screen is None:
        return None
    tail = "\n".join([line for line in screen.splitlines() if line.strip()][-24:])
    if ASKING.search(tail):
        return "asking"
    return "working" if WORKING.search(tail) else "idle"


def advance(prev, observed, seen, now):
    """Next (state, since, unread, leaving) of an agent tab.

    `prev` is the stored tuple, `observed` what its screen shows now, `seen`
    whether a focused terminal shows the tab. A tab that finishes working
    while nobody looks at it stays unread until someone does.
    """
    state, since, unread, leaving = prev
    unread = unread and not seen
    if observed is None or observed == state:
        return state, since or now, unread, 0.0
    if state == "working" and observed == "idle":
        leaving = leaving or now
        if now - leaving < SETTLE:
            return state, since, unread, leaving
        return observed, now, not seen, 0.0
    return observed, now, unread and observed != "working", 0.0


def describe(group, screen, focused, now, home=None):
    """(Tab, option changes) for one tab; changes map option names to new values (None unsets)."""
    pane = main_pane(group)
    first = group[0]
    kind = kind_of(pane) if pane else "shell"
    stored = (first["@m1_state"], float(first["@m1_since"] or 0), truthy(first["@m1_unread"]),
              float(first["@m1_leaving"] or 0))
    changes = {}
    if kind == "agent":
        seen = first["window_id"] in focused
        state, since, unread, leaving = advance(stored, observe(screen), seen, now)
        new = (state, since, unread, leaving)
        names = ("@m1_state", "@m1_since", "@m1_unread", "@m1_leaving")
        values = (state or None, f"{since:.0f}", "1" if unread else None, f"{leaving:.1f}" if leaving else None)
        for name, old, value, current in zip(names, stored, values, new):
            if old != current:
                changes[name] = value
    else:
        state, since, unread = "", 0.0, False
        if stored[0] or stored[2]:
            changes = {"@m1_state": None, "@m1_since": None, "@m1_unread": None, "@m1_leaving": None}
    path = short_path(pane["pane_current_path"], home) if pane else ""
    command = pane["pane_current_command"] if pane else ""
    tab = Tab(first["window_id"], int(first["window_index"]), tab_title(group, home), kind, state, unread,
              truthy(first["window_bell_flag"]), since, path, command)
    return tab, changes


def cell_width(char):
    if unicodedata.combining(char) or char in "​‍︎️":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def text_width(text):
    return sum(cell_width(char) for char in text)


def fit(text, width, keep="start"):
    """Pad or cut `text` to exactly `width` terminal cells, marking a cut with an ellipsis."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    total = text_width(text)
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


def head(text, width):
    """The longest start of `text` that fits in `width` cells."""
    used = 0
    for position, char in enumerate(text):
        used += cell_width(char)
        if used > width:
            return text[:position]
    return text


def wrap(text, width, lines):
    """Word-wrap `text` into at most `lines` lines of `width` cells; the last one may be cut."""
    out, words = [], text.split()
    while words and len(out) < lines - 1:
        line = words.pop(0)
        if text_width(line) > width:
            # A word longer than a line: break it and carry the rest over.
            words.insert(0, line[len(head(line, width)):])
            line = head(line, width)
        while words and text_width(line) + 1 + text_width(words[0]) <= width:
            line += " " + words.pop(0)
        out.append(line)
    if words:
        out.append(" ".join(words))
    return [fit(line, width) for line in out] or [" " * width]


def ago(seconds):
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def status(tab, now, frame=0):
    """(gutter glyph, its color, status text, text color) for a tab."""
    where = "" if tab.path in ("~", "") else " · " + tab.path.rsplit("/", 1)[-1]
    elapsed = ago(now - tab.since) if tab.since else ""
    if tab.bell or tab.state == "asking":
        return "●", YELLOW, "Needs you" + where, YELLOW
    if tab.state == "working":
        return SPINNER[frame % len(SPINNER)], BLUE, f"Working · {elapsed}{where}", MUTED
    if tab.unread:
        return "●", GREEN, f"Done · {elapsed} ago{where}", GREEN
    if tab.kind == "agent":
        return " ", MUTED, f"Idle · {elapsed}{where}", MUTED
    if tab.kind == "program":
        return " ", MUTED, f"{tab.command} · {tab.path}", MUTED
    return " ", MUTED, tab.path if tab.path != tab.title else "Shell", MUTED


def render(tabs, current, width, height, now, hover=None, drag=None, frame=0):
    """Lines for the sidebar, a map of row -> window id ("new" for the New tab
    row), and the rows whose last cells are a close button.

    `hover` is (target, over_close) for the row under the mouse; `drag` is
    (source, target) while a tab is dragged.
    """
    inner = width - 7  # bar, gap, glyph, gap ... gap, close or number, gap
    layouts = ((2, 1, 1), (1, 1, 1), (1, 1, 0), (1, 0, 0))  # title lines, status line, gap
    for title_lines, status_lines, gap in layouts:
        need = 2 + sum(len(wrap(tab.title, inner, title_lines)) + status_lines + gap for tab in tabs)
        if need <= height:
            break
    lines, rows, closers = [""], {}, set()
    for tab in tabs:
        active = tab.window_id == current
        hovered = hover is not None and hover[0] == tab.window_id
        source, target = drag or (None, None)
        bg = ACTIVE_BG if active else HOVER_BG if hovered or target == tab.window_id else ""
        bar = f"{BLUE}▌" if active or target == tab.window_id else " "
        glyph, glyph_color, text, text_color = status(tab, now, frame)
        name_color = (BRIGHT + BOLD if active else BRIGHT if tab.unread or hovered else TEXT)
        if tab.window_id == source and target not in (None, source):
            name_color = MUTED
        if active or hovered:
            close_color = RED if hovered and hover[1] else TEXT
            right = f"{close_color}×"
            closers.add(len(lines))
        else:
            right = f"{FAINT}{tab.index if tab.index < 10 else ' '}"
        for number, part in enumerate(wrap(tab.title, inner, title_lines)):
            rows[len(lines)] = tab.window_id
            if number == 0:
                lines.append(f"{bg}{bar}{RESET}{bg} {glyph_color}{glyph} {name_color}{part}{RESET}{bg} "
                             f"{right}{RESET}{bg} {RESET}")
            else:
                lines.append(f"{bg}{bar}{RESET}{bg}   {name_color}{part}{RESET}{bg}   {RESET}")
        if status_lines:
            rows[len(lines)] = tab.window_id
            lines.append(f"{bg}{bar}{RESET}{bg}   {text_color}{fit(text, width - 5)}{RESET}{bg} {RESET}")
        if gap:
            lines.append("")
    if len(lines) < height:
        rows[len(lines)] = "new"
        color = TEXT if hover is not None and hover[0] == "new" else MUTED
        lines.append(f"{color}{fit('  + New tab', width - 4)}⌘N  {RESET}")
    return lines[:height], rows, closers


class Sidebar:
    """The long-running process in a sidebar pane."""

    def __init__(self):
        self.pane = os.environ["TMUX_PANE"]
        self.rows, self.closers = {}, set()
        self.panes, self.tabs = [], []
        self.current = None
        self.visible = False
        self.last_frame = None
        self.hover, self.hover_until = None, 0.0
        self.press, self.drag = None, None

    def window_panes(self):
        mine = [pane for pane in self.panes if pane["pane_id"] == self.pane]
        if not mine:
            return []
        return [pane for pane in self.panes if pane["window_id"] == mine[0]["window_id"]]

    def poll(self):
        """Read every tab; return seconds until the next poll, or None when this tab has closed."""
        self.panes = list_panes(self.pane)
        own = self.window_panes()
        if not any(not is_sidebar(pane) for pane in own):
            tmux("kill-pane", "-t", self.pane)  # Only the sidebar is left: close the tab.
            return None
        self.current = own[0]["window_id"]
        groups = windows_of(self.panes)
        mains = [main_pane(group) for group in groups]
        screens, focused = snapshot([pane["pane_id"] for pane in mains if pane and kind_of(pane) == "agent"])
        now = time.time()
        self.tabs, writes = [], []
        for group, pane in zip(groups, mains):
            tab, changes = describe(group, screens.get(pane["pane_id"]) if pane else None, focused, now)
            self.tabs.append(tab)
            for name, value in changes.items():
                writes += [";", "set-option", "-w", "-t", tab.window_id, name, value] if value is not None else \
                    [";", "set-option", "-wu", "-t", tab.window_id, name]
        if writes:
            tmux(*writes[1:])
        self.visible = int(own[0]["window_active_clients"] or 0) > 0
        # Poll often only while someone is looking; hooks signal structural changes.
        return 1.0 if self.visible else 5.0

    def hover_state(self):
        if self.hover is None or time.monotonic() > self.hover_until:
            return None
        x, y = self.hover
        target = self.rows.get(y)
        if target is None:
            return None
        width = os.get_terminal_size(sys.stdout.fileno()).columns
        return target, y in self.closers and x >= width - 3

    def paint(self):
        width, height = os.get_terminal_size(sys.stdout.fileno())
        frame_number = int(time.monotonic() / FRAME)
        lines, self.rows, self.closers = render(self.tabs, self.current, width, height, time.time(),
                                                self.hover_state(), self.drag, frame_number)
        frame = "".join(f"\x1b[{row + 1};1H{line}\x1b[K" for row, line in enumerate(lines)) + "\x1b[J"
        if frame != self.last_frame:
            sys.stdout.write("\x1b[H" + frame)
            sys.stdout.flush()
            self.last_frame = frame

    def animating(self):
        return self.visible and any(tab.state == "working" for tab in self.tabs)

    def clicking_client(self):
        """The client that clicked: the focused one showing this tab, else the most recently active."""
        own = self.window_panes()
        if not own:
            return None
        fmt = SEP.join(["#{client_flags}", "#{client_activity}", "#{client_name}", "#{session_name}",
                        "#{window_id}"])
        clients = []
        for line in tmux("list-clients", "-F", fmt).splitlines():
            flags, activity, name, session, window = (line.split(SEP) + [""] * 5)[:5]
            if window == own[0]["window_id"]:
                clients.append(("focused" in flags.split(","), int(activity or 0), name, session))
        return max(clients)[2:] if clients else None

    def group_of(self, window_id):
        return [pane for pane in self.panes if pane["window_id"] == window_id]

    def show(self, session, window_id):
        tmux("select-window", "-t", f"{session}:{window_id}")
        group = self.group_of(window_id)
        pane = main_pane(group)
        if pane is not None and any(is_sidebar(p) and p["pane_active"] == "1" for p in group):
            tmux("select-pane", "-t", pane["pane_id"])

    def menu_at(self, client, x, y, title, items):
        own = self.window_panes()[0]
        height = len(items) // 3 + 2
        # A numeric -y is the menu's bottom edge.
        tmux_spawn("display-menu", "-c", client, "-t", self.pane, "-x", str(int(own["pane_left"]) + x),
             "-y", str(int(own["pane_top"]) + y + height), "-T", f"#[align=centre]{title}", *items)

    def close(self, client, window_id, x, y):
        """Close a tab; ask first unless it is a plain shell."""
        pane = main_pane(self.group_of(window_id))
        if pane is None or pane["pane_current_command"] in SHELLS:
            tmux("kill-window", "-t", window_id)
            return
        command = pane["pane_current_command"]
        program = "Codex" if command == "codex" else "Claude" if kind_of(pane) == "agent" else command
        self.menu_at(client, x, y, "Close this tab?",
                     [f"Close tab and stop {program}", "y", f"kill-window -t {window_id}",
                      "Cancel", "Escape", ""])

    def menu(self, client, window_id, x, y):
        ids = [tab.window_id for tab in self.tabs]
        tab = self.tabs[ids.index(window_id)]
        group = self.group_of(window_id)
        renamed = not truthy(group[0]["automatic-rename"])
        position = ids.index(window_id)
        quoted = tab.title.replace("\\", "\\\\").replace('"', '\\"').replace("#", "##")
        items = ["Rename…", "r",
                 f'command-prompt -I "{quoted}" -p "Tab name:" {{ rename-window -t {window_id} -- "%%" }}']
        if renamed:
            items += ["Use automatic name", "a", f"set -w -t {window_id} automatic-rename on"]
        if tab.unread:
            items += ["Mark as read", "m", f"set -wu -t {window_id} @m1_unread"]
        # swap-window has no after-hook; tell the other sidebars directly.
        redraw = 'run-shell -b "#{@m1_sidebar_cmd} refresh"'
        if position > 0:
            items += ["Move up", "u", f"swap-window -d -s {window_id} -t {ids[position - 1]} ; {redraw}"]
        if position < len(ids) - 1:
            items += ["Move down", "d", f"swap-window -d -s {window_id} -t {ids[position + 1]} ; {redraw}"]
        items += ["", "", "", "Close tab", "x", f"kill-window -t {window_id}"]
        self.menu_at(client, x, y, "Tab", items)

    def click(self, name, session, target):
        """Show a tab, or rename it on a double-click.

        The first click usually shows another tab, so the second lands in that
        tab's sidebar; the last click is kept on the server for it to see.
        """
        now = time.time()
        last, _, at = tmux("show-options", "-gqv", "@m1_click").strip().partition(" ")
        if last == target and now - float(at or 0) < DOUBLE_CLICK:
            tmux("set-option", "-gu", "@m1_click")
            prompt_rename(name, target, next(tab.title for tab in self.tabs if tab.window_id == target))
        else:
            tmux("set-option", "-g", "@m1_click", f"{target} {now:.3f}")
            self.show(session, target)

    def move(self, source, target):
        """Put the dragged tab where it was dropped and keep tab numbers in order."""
        ids = [tab.window_id for tab in self.tabs]
        if source not in ids or target not in ids or source == target:
            return
        side = "-b" if ids.index(target) < ids.index(source) else "-a"
        tmux("move-window", side, "-s", source, "-t", target, ";", "move-window", "-r", "-t", f"={GROUP}")

    def handle_mouse(self, data):
        """Act on mouse reports; return True when tabs may have changed.

        Everything happens on release, on the row that was pressed: a menu opened
        on press would be closed by the release, and a tab shown on press would
        move the rest of a drag into that tab's sidebar.
        """
        changed = False
        width, height = os.get_terminal_size(sys.stdout.fileno())
        for button, x, y, kind in re.findall(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", data):
            button, x, y = int(button), int(x) - 1, int(y) - 1
            if button & 64:  # Wheel.
                continue
            target = self.rows.get(y)
            closing = y in self.closers and x >= width - 3
            if button & 32:  # Motion, with or without a button held.
                edge = x >= width - 1 or y in (0, height - 1)
                # Leaving the pane sends nothing, so a hover ends on its own (quickly near an edge).
                self.hover, self.hover_until = (x, y), time.monotonic() + (0.3 if edge else 6.0)
                if button & 3 != 0:  # No button held: a release outside the pane went elsewhere.
                    self.press, self.drag = None, None
                elif self.press and self.press[0] == 0 and not self.press[2] and \
                        self.press[1] != "new" and target not in (None, "new"):
                    source = self.press[1]
                    self.drag = (source, target) if target != source or self.drag else None
                continue
            if kind == b"M":
                self.press, self.drag = ((button & 3, target, closing) if target is not None else None), None
                continue
            press, drag = self.press, self.drag
            self.press, self.drag = None, None
            if drag is not None:
                self.move(*drag)
                changed = True
                continue
            if press is None or press[:2] != (button & 3, target):
                continue
            client = self.clicking_client()
            if client is None:
                continue
            name, session = client
            if target == "new":
                if press[0] == 0:
                    tmux("new-window", "-t", f"{session}:", "-c", os.path.expanduser("~"))
            elif press[0] == 1 or (press[0] == 0 and press[2] and closing):
                self.close(name, target, x, y)
            elif press[0] == 0:
                self.click(name, session, target)
            elif press[0] == 2:
                self.menu(name, target, x, y)
            changed = True
        return changed

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
        # Hide the cursor, stop line wrap, and ask for every mouse event (SGR
        # encoding): presses, drags, and plain motion for hover.
        sys.stdout.write("\x1b[?25l\x1b[?7l\x1b[?1003h\x1b[?1006h\x1b[2J")
        sys.stdout.flush()
        next_poll = 0.0
        while True:
            try:
                if time.monotonic() >= next_poll:
                    wait = self.poll()
                    if wait is None:
                        return
                    next_poll = time.monotonic() + wait
                self.paint()
            except Exception as error:  # Keep the pane alive; a crash would only respawn it.
                sys.stdout.write(f"\x1b[H\x1b[2J{MUTED}sidebar error:\r\n{fit(str(error), 60)}{RESET}")
                sys.stdout.flush()
                self.last_frame, next_poll = None, time.monotonic() + 5.0
            timeout = next_poll - time.monotonic()
            if self.animating():
                timeout = min(timeout, FRAME)
            if self.hover is not None:
                if time.monotonic() > self.hover_until:
                    self.hover = None
                else:
                    timeout = min(timeout, self.hover_until - time.monotonic())
            CHILDREN[:] = [child for child in CHILDREN if child.poll() is None]
            ready, _, _ = select.select([wake_r, stdin], [], [], max(0.0, timeout))
            if wake_r in ready:
                os.read(wake_r, 512)
                next_poll = 0.0
            if stdin in ready:
                data = os.read(stdin, 4096)
                if not data:
                    return
                try:
                    if self.handle_mouse(data):
                        next_poll = 0.0
                except Exception:
                    pass


def sidebar_command():
    return f"exec {sys.executable} -I {Path(__file__).resolve()} run"


def style_sidebar(pane_id):
    tmux("set-option", "-p", "-t", pane_id, "window-style", f"bg={PANEL}", ";",
         "set-option", "-p", "-t", pane_id, "window-active-style", f"bg={PANEL}", ";",
         "set-option", "-p", "-t", pane_id, "@sidebar_styled", "1")


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
        width = min(WIDTH, max(16, window_width // 4))
        if not bars:
            # Mark the pane before the sidebar starts. remain-on-exit keeps a crashed
            # sidebar on screen with its error instead of looping through respawns.
            pane_id = tmux("split-window", "-fhbd", "-l", str(width), "-t", window_id,
                           "-P", "-F", "#{pane_id}", "exec cat").strip()
            if pane_id:
                style_sidebar(pane_id)
                tmux("set-option", "-p", "-t", pane_id, "@sidebar", "1", ";",
                     "set-option", "-p", "-t", pane_id, "remain-on-exit", "on", ";",
                     "respawn-pane", "-k", "-t", pane_id, sidebar_command())
            continue
        for extra in bars[1:]:
            tmux("kill-pane", "-t", extra["pane_id"])
        if bars[0]["@sidebar_styled"] != "1":
            style_sidebar(bars[0]["pane_id"])
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


def restart():
    """Start every sidebar again with the installed code, restyled."""
    for pane_id in tmux("list-panes", "-a", "-f", "#{@sidebar}", "-F", "#{pane_id}").split():
        style_sidebar(pane_id)
        tmux("respawn-pane", "-k", "-t", pane_id, sidebar_command())
    ensure()


def client_view(client):
    """(session name, current window id) of a client."""
    out = tmux("display-message", "-p", "-c", client, f"#{{session_name}}{SEP}#{{window_id}}").strip()
    session, _, window_id = out.partition(SEP)
    return session, window_id


def prompt_rename(client, window_id, title):
    tmux_spawn("command-prompt", "-t", client, "-I", title.replace("#", "##"), "-p", "Tab name:",
         f'rename-window -t {window_id} -- "%%"')


def rename(client):
    _, window_id = client_view(client)
    group = [pane for pane in list_panes(f"={GROUP}") if pane["window_id"] == window_id]
    if group:
        prompt_rename(client, window_id, tab_title(group))


def jump(client):
    """Show the next tab, after the current one, that needs you or finished unseen."""
    session, current = client_view(client)
    groups = windows_of(list_panes(f"={GROUP}"))
    waiting = [group[0]["window_id"] for group in groups
               if group[0]["@m1_state"] == "asking" or truthy(group[0]["@m1_unread"])
               or truthy(group[0]["window_bell_flag"])]
    ids = [group[0]["window_id"] for group in groups]
    if not waiting:
        tmux("display-message", "-c", client, "No tab needs you")
        return
    start = ids.index(current) + 1 if current in ids else 0
    target = next((w for w in ids[start:] + ids[:start] if w in waiting), waiting[0])
    tmux("select-window", "-t", f"{session}:{target}")


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "run":
        Sidebar().run()
    elif command == "ensure":
        ensure()
    elif command == "refresh":
        refresh()
    elif command == "restart":
        restart()
    elif command == "rename" and len(argv) > 2:
        rename(argv[2])
    elif command == "jump" and len(argv) > 2:
        jump(argv[2])
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
