import importlib.util
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).parent
spec = importlib.util.spec_from_file_location("m1_sidebar", SCRIPTS / "m1-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


def pane(**fields):
    base = {field: "" for field in sidebar.PANE_FIELDS}
    base.update({"window_id": "@1", "window_index": "1", "automatic-rename": "1", "pane_active": "1", "host": "m1.local", "host_short": "m1",
                 "pane_current_command": "zsh", "pane_current_path": "/Users/me"})
    base.update(fields)
    return base


class TitleTests(unittest.TestCase):
    def test_agent_status_glyph_is_dropped_from_its_title(self):
        group = [pane(pane_title="✳ Fix invoices", pane_current_command="2_1_295")]
        self.assertEqual(sidebar.tab_title(group, home="/Users/me"), "Fix invoices")
        self.assertEqual(sidebar.kind_of(group[0]), "agent")

    def test_shell_with_default_title_is_named_by_folder(self):
        group = [pane(pane_title="m1.local", pane_current_path="/Users/me/projects/ops")]
        self.assertEqual(sidebar.tab_title(group, home="/Users/me"), "ops")
        self.assertEqual(sidebar.tab_title([pane(pane_title="m1")], home="/Users/me"), "~")

    def test_hand_given_name_wins(self):
        group = [pane(pane_title="✳ Something", window_name="Billing", **{"automatic-rename": "0"})]
        self.assertEqual(sidebar.tab_title(group, home="/Users/me"), "Billing")

    def test_sidebar_pane_is_not_the_tab(self):
        group = [pane(**{"@sidebar": "1", "pane_title": "sidebar", "pane_active": "1"}),
                 pane(pane_title="✳ Real work", pane_active="0", pane_current_command="claude")]
        self.assertEqual(sidebar.tab_title(group, home="/Users/me"), "Real work")


class StatusTests(unittest.TestCase):
    def test_reads_what_the_agent_footer_says(self):
        working = "✽ Shimmying… (3m 49s)\n❯ \n  ⏵⏵ bypass permissions on · esc to interrupt · ← for agents\n"
        self.assertEqual(sidebar.observe(working), "working")
        self.assertEqual(sidebar.observe("✻ Waiting for 2 background agents to finish\n❯ \n"), "working")
        self.assertEqual(sidebar.observe("Do you want to proceed?\n❯ 1. Yes\nEsc to cancel · Tab to amend"),
                         "asking")
        self.assertEqual(sidebar.observe("Done.\n❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"), "idle")
        self.assertIsNone(sidebar.observe(None))

    def test_finishing_unseen_marks_unread_after_settling(self):
        state = ("working", 100.0, False, 0.0)
        # A brief gap in the footer does not count as finished.
        state = sidebar.advance(state, "idle", False, 200.0)
        self.assertEqual(state, ("working", 100.0, False, 200.0))
        state = sidebar.advance(state, "working", False, 201.0)
        self.assertEqual(state, ("working", 100.0, False, 0.0))
        state = sidebar.advance(state, "idle", False, 300.0)
        state = sidebar.advance(state, "idle", False, 300.0 + sidebar.SETTLE)
        self.assertEqual(state, ("idle", 300.0 + sidebar.SETTLE, True, 0.0))
        # Looking at it marks it read; starting work again clears it too.
        self.assertFalse(sidebar.advance(state, "idle", True, 400.0)[2])
        self.assertFalse(sidebar.advance(state, "working", False, 400.0)[2])

    def test_finishing_in_view_is_not_unread(self):
        state = sidebar.advance(("working", 100.0, False, 150.0), "idle", True, 160.0)
        self.assertEqual(state[:3], ("idle", 160.0, False))

    def test_describe_stores_changes_as_window_options(self):
        group = [pane(window_id="@4", window_index="2", pane_title="✳ Ship it", pane_current_command="codex",
                      **{"@m1_state": "working", "@m1_since": "100"})]
        tab, changes = sidebar.describe(group, "❯ \n", set(), 200.0, home="/Users/me")
        self.assertEqual((tab.title, tab.kind, tab.state), ("Ship it", "agent", "working"))
        self.assertEqual(changes, {"@m1_leaving": "200.0"})
        _, changes = sidebar.describe([pane(**{"@m1_state": "idle", "@m1_unread": "1"})], None, set(), 200.0)
        self.assertEqual(changes, {"@m1_state": None, "@m1_since": None, "@m1_unread": None, "@m1_leaving": None})


def tab(window_id, index, title, **fields):
    base = dict(kind="shell", state="", unread=False, bell=False, since=0.0, path="~", command="zsh")
    base.update(fields)
    return sidebar.Tab(window_id, index, title, **base)


def plain(line):
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


class RenderTests(unittest.TestCase):
    def test_fit_pads_and_cuts_by_terminal_cells(self):
        self.assertEqual(sidebar.fit("abc", 5), "abc  ")
        self.assertEqual(sidebar.fit("abcdefgh", 5), "abcd…")
        self.assertEqual(sidebar.fit("~/projects/ops", 6, keep="end"), "…s/ops")
        self.assertEqual(sidebar.fit("日本語です", 5), "日本…")

    def test_wrap_breaks_titles_at_words(self):
        self.assertEqual(sidebar.wrap("Pay later checkout dev server", 20, 2),
                         ["Pay later checkout  ", "dev server          "])
        self.assertEqual(sidebar.wrap("Simply ops inbox with iMessage and Guesty webhooks", 20, 2)[1],
                         "with iMessage and G…")
        self.assertEqual(sidebar.wrap("short", 8, 2), ["short   "])
        self.assertEqual(sidebar.wrap("abcdefghijkl", 5, 2), ["abcde", "fghi…"])

    def test_every_line_fills_the_pane_width(self):
        tabs = [tab("@1", 1, "Pay later checkout dev server", kind="agent", state="working", since=90.0),
                tab("@2", 2, "日本語のタブ", kind="agent", state="idle", unread=True, since=50.0),
                tab("@3", 3, "ops", path="~/projects/ops")]
        for width in (24, 32):
            lines, _, _ = sidebar.render(tabs, "@1", width, 30, 100.0, hover=("@3", False))
            for line in lines[1:]:
                if line:
                    self.assertEqual(sidebar.text_width(plain(line)), width, plain(line))

    def test_rows_map_clicks_to_tabs_close_buttons_and_new_tab(self):
        tabs = [tab("@1", 1, "one"), tab("@2", 2, "two", bell=True)]
        lines, rows, closers = sidebar.render(tabs, "@2", 32, 20, 0.0)
        self.assertEqual(rows, {1: "@1", 2: "@1", 4: "@2", 5: "@2", 7: "new"})
        self.assertEqual(closers, {4})  # Only the shown tab, or a hovered one, offers its close button.
        self.assertIn("Needs you", plain(lines[5]))
        self.assertTrue(plain(lines[4]).rstrip().endswith("×"))
        _, _, closers = sidebar.render(tabs, "@2", 32, 20, 0.0, hover=("@1", False))
        self.assertEqual(closers, {1, 4})

    def test_status_lines_say_what_agents_are_doing(self):
        tabs = [tab("@1", 1, "a", kind="agent", state="working", since=40.0),
                tab("@2", 2, "b", kind="agent", state="idle", unread=True, since=-200.0),
                tab("@3", 3, "c", kind="agent", state="idle", since=-3600.0, path="~/projects/ops")]
        lines, _, _ = sidebar.render(tabs, "@3", 32, 30, 100.0)
        text = [plain(line).strip(" ▌") for line in lines]
        self.assertIn("Working · 1m", text)
        self.assertIn("Done · 5m ago", text)
        self.assertIn("Idle · 1h · ops", text)

    def test_compact_when_tabs_do_not_fit(self):
        tabs = [tab(f"@{i}", i, f"tab {i}") for i in range(1, 9)]
        lines, rows, _ = sidebar.render(tabs, "@1", 32, 12, 0.0)
        self.assertEqual([rows[row] for row in range(1, 9)], [f"@{i}" for i in range(1, 9)])
        self.assertEqual(rows[9], "new")


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxServerTests(unittest.TestCase):
    """Runs `ensure` and real sidebars on a private tmux server."""

    def setUp(self):
        self.socket = f"m1-sidebar-test-{os.getpid()}"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["M1_TABS_SOCKET"] = self.socket
        command = f"{sys.executable} -I {SCRIPTS / 'm1-sidebar.py'}"
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "t-1009-000000-1", "sleep 600")
        self.tmux("new-session", "-d", "-s", "main")
        self.tmux("set", "-g", "@m1_sidebar_cmd", command, ";", "source-file", str(SCRIPTS / "m1-tabs.tmux.conf"))
        self.terminals = []

    def tearDown(self):
        # Stop the server first: a client exits with it, even with nobody reading its terminal.
        self.tmux("kill-server")
        for pid, fd in self.terminals:
            os.close(fd)
            for _ in range(30):
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True, env=self.env).stdout

    def ensure(self):
        subprocess.run([sys.executable, "-I", str(SCRIPTS / "m1-sidebar.py"), "ensure"], env=self.env, check=True)

    def wait_for(self, predicate, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def windows(self):
        fmt = "#{window_id} #{window_panes} #{P:#{?#{@sidebar},S,}}"
        return [line.split(" ") for line in self.tmux("list-windows", "-t", "=main", "-F", fmt).splitlines()]

    def test_adopts_old_tab_sessions_and_adds_one_live_sidebar_per_tab(self):
        self.ensure()
        self.assertNotIn("t-1009-000000-1", self.tmux("ls", "-F", "#{session_name}").split())
        self.assertTrue(self.wait_for(lambda: all(w[1] == "2" and w[2] == "S" for w in self.windows())))
        self.assertEqual(len(self.windows()), 2)
        live = "#{?#{==:#{pane_pid},#{@sidebar_pid}},1,}"
        self.assertTrue(self.wait_for(lambda: self.tmux("list-panes", "-a", "-F", live).split() == ["1", "1"]))
        # Starting sidebars must not have killed and respawned any of them.
        first_ids = self.tmux("list-panes", "-a", "-f", "#{@sidebar}", "-F", "#{pane_id}").split()
        self.ensure()
        time.sleep(0.5)
        self.assertEqual(self.tmux("list-panes", "-a", "-f", "#{@sidebar}", "-F", "#{pane_id}").split(), first_ids)

    def open_window(self):
        """Open a terminal window the way Ghostty does: m1-tab in its own terminal."""
        env = dict(self.env, M1_SIDEBAR=str(SCRIPTS / "m1-sidebar.py"), M1_PYTHON=sys.executable,
                   M1_TABS_CONF=str(SCRIPTS / "m1-tabs.tmux.conf"), TERM="xterm-256color")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/sh", ["/bin/sh", str(SCRIPTS / "m1-tab.sh")], env)
        self.terminals.append((pid, fd))

    def drain(self):
        for _, fd in self.terminals:
            try:
                while select.select([fd], [], [], 0)[0] and os.read(fd, 65536):
                    pass
            except OSError:
                pass

    def shown_windows(self):
        self.drain()
        return self.tmux("list-clients", "-F", "#{window_id}").split()

    def test_another_window_opens_a_new_tab_instead_of_mirroring_one(self):
        self.ensure()
        self.open_window()
        self.assertTrue(self.wait_for(lambda: len(self.shown_windows()) == 1))
        tabs = len(self.windows())
        self.open_window()
        self.assertTrue(self.wait_for(lambda: len(self.shown_windows()) == 2))
        self.assertEqual(len(set(self.shown_windows())), 2)
        self.assertEqual(len(self.windows()), tabs + 1)

    def test_snapshot_reads_agent_screens(self):
        pane_id = self.tmux("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}",
                            "printf 'Thinking\\n  esc to interrupt\\n'; sleep 600").strip()
        env = dict(self.env)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(self.wait_for(lambda: "esc to interrupt" in sidebar.snapshot([pane_id])[0].get(pane_id, "")))
            self.assertEqual(sidebar.observe(sidebar.snapshot([pane_id])[0][pane_id]), "working")

    def test_tab_closes_when_its_program_exits(self):
        self.tmux("new-window", "-t", "=main:", "sleep 1")
        self.assertTrue(self.wait_for(lambda: len(self.windows()) == 3 and all(w[2] == "S" for w in self.windows())))
        self.assertTrue(self.wait_for(lambda: len(self.windows()) == 2, seconds=6))


if __name__ == "__main__":
    unittest.main()
