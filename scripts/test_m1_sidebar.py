import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest


SCRIPTS = Path(__file__).parent
spec = importlib.util.spec_from_file_location("m1_sidebar", SCRIPTS / "m1-sidebar.py")
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


def pane(**fields):
    base = {field: "" for field in sidebar.PANE_FIELDS}
    base.update({"automatic-rename": "1", "pane_active": "1", "host": "m1.local", "host_short": "m1",
                 "pane_current_command": "zsh", "pane_current_path": "/Users/me"})
    base.update(fields)
    return base


class LabelTests(unittest.TestCase):
    def test_agent_title_with_folder(self):
        title, detail = sidebar.tab_label([pane(pane_title="✳ Fix invoices", pane_current_command="2.1.295",
                                                pane_current_path="/Users/me/projects/ops")], home="/Users/me")
        self.assertEqual((title, detail), ("✳ Fix invoices", "~/projects/ops"))

    def test_shell_with_default_title_is_named_by_folder(self):
        title, detail = sidebar.tab_label([pane(pane_title="m1.local", pane_current_path="/Users/me/projects/ops")],
                                          home="/Users/me")
        self.assertEqual((title, detail), ("ops", "~/projects/ops"))
        self.assertEqual(sidebar.tab_label([pane(pane_title="m1")], home="/Users/me"), ("~", "zsh"))

    def test_hand_given_name_wins(self):
        title, _ = sidebar.tab_label([pane(pane_title="✳ Something", window_name="Billing",
                                           **{"automatic-rename": "0"})], home="/Users/me")
        self.assertEqual(title, "Billing")

    def test_sidebar_pane_is_not_the_tab(self):
        group = [pane(**{"@sidebar": "1", "pane_title": "sidebar", "pane_active": "1"}),
                 pane(pane_title="✳ Real work", pane_active="0", pane_current_command="claude")]
        self.assertEqual(sidebar.tab_label(group, home="/Users/me")[0], "✳ Real work")


class RenderTests(unittest.TestCase):
    def test_fit_pads_and_cuts_by_terminal_cells(self):
        self.assertEqual(sidebar.fit("abc", 5), "abc  ")
        self.assertEqual(sidebar.fit("abcdefgh", 5), "abcd…")
        self.assertEqual(sidebar.fit("~/projects/ops", 6, keep="end"), "…s/ops")
        self.assertEqual(sidebar.fit("日本語です", 5), "日本…")

    def test_rows_map_clicks_to_tabs_and_new_tab(self):
        tabs = [("@1", 1, "one", "~", False), ("@2", 2, "two", "~", True)]
        lines, rows = sidebar.render(tabs, "@2", 28, 20)
        self.assertEqual(rows, {1: "@1", 2: "@1", 4: "@2", 5: "@2", 7: "new"})
        self.assertEqual(len(lines), 8)

    def test_compact_when_tabs_do_not_fit(self):
        tabs = [(f"@{i}", i, f"tab {i}", "~", False) for i in range(1, 9)]
        lines, rows = sidebar.render(tabs, "@1", 28, 12)
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

    def tearDown(self):
        self.tmux("kill-server")

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

    def test_tab_closes_when_its_program_exits(self):
        self.tmux("new-window", "-t", "=main:", "sleep 1")
        self.assertTrue(self.wait_for(lambda: len(self.windows()) == 3 and all(w[2] == "S" for w in self.windows())))
        self.assertTrue(self.wait_for(lambda: len(self.windows()) == 2, seconds=6))


if __name__ == "__main__":
    unittest.main()
