"""Protect the laptop's switch from home host cmux workspaces to local terminals."""

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).with_name("laptop-local-cmux.py")
spec = importlib.util.spec_from_file_location("laptop_local_cmux", SOURCE)
laptop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(laptop)

REMOTE_FIRST_CMUX = """{
  "$schema": "https://raw.githubusercontent.com/manaflow-ai/cmux/main/web/data/cmux.schema.json",
  "schemaVersion": 1,
  // This file uses JSON with comments (JSONC).
  "actions": {
    "m1-workspace": {
      "type": "workspace",
      "title": "M1",
      "workspace": { "name": "M1", "command": "cmux mosh-tmux home --session m1-$(date +%H%M%S)" },
    },
    "typecheck": { "type": "command", "command": "npx tsc --noEmit // not a comment" },
  },
  "ui": { "newWorkspace": { "action": "m1-workspace" } },
  /* Agents run on the home host. */
  "automation": { "claudeBinaryPath": "/Users/me/.local/bin/claude-cmux-home" },
  "terminal": { "autoResumeAgentSessions": false, "copyOnSelect": true },
}
"""


class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas_outside_strings(self):
        config = laptop.parse_jsonc(REMOTE_FIRST_CMUX)
        self.assertEqual(config["actions"]["typecheck"]["command"], "npx tsc --noEmit // not a comment")
        self.assertTrue(config["$schema"].startswith("https://"))

    def test_escaped_quotes_and_comment_markers_in_strings(self):
        text = '{"a": "say \\"hi\\" /* kept */", "b": [1, 2,],} // end'
        self.assertEqual(laptop.parse_jsonc(text), {"a": 'say "hi" /* kept */', "b": [1, 2]})


class CmuxTests(unittest.TestCase):
    def test_remote_first_workspace_and_claude_binary_become_local(self):
        config = laptop.parse_jsonc(REMOTE_FIRST_CMUX)
        changes = laptop.localize_cmux(config, "/Users/me/.local/bin/claude-proxy")
        self.assertEqual(len(changes), 2)
        self.assertEqual(config["ui"]["newWorkspace"]["action"], "cmux.newWorkspace")
        self.assertEqual(config["automation"]["claudeBinaryPath"], "/Users/me/.local/bin/claude-proxy")
        # The home host action stays available; unrelated settings are untouched.
        self.assertIn("m1-workspace", config["actions"])
        self.assertEqual(config["terminal"], {"autoResumeAgentSessions": False, "copyOnSelect": True})
        self.assertEqual(laptop.home_host_references(config), [])
        self.assertEqual(laptop.localize_cmux(config, "/Users/me/.local/bin/claude-proxy"), [])

    def test_laptop_shape_without_a_claude_binary_gets_the_pool_launcher(self):
        config = {"actions": {"m1-workspace": {"type": "workspace", "workspace": {"layout": {"pane": {"surfaces": [
                      {"type": "terminal", "command": "cmux mosh-tmux home --name M1; exit"}]}}}}},
                  "ui": {"newWorkspace": {"action": "m1-workspace"}},
                  "terminal": {"autoResumeAgentSessions": False}}
        laptop.localize_cmux(config, "/Users/me/.local/bin/claude-proxy")
        self.assertEqual(config["ui"]["newWorkspace"]["action"], "cmux.newWorkspace")
        self.assertEqual(config["automation"], {"claudeBinaryPath": "/Users/me/.local/bin/claude-proxy"})
        self.assertEqual(config["terminal"], {"autoResumeAgentSessions": False})

    def test_home_host_action_on_cmd_n_gives_the_key_back(self):
        config = {
            "actions": {
                "m1-workspace": {"type": "workspaceCommand", "commandName": "M1", "shortcut": "cmd+n"},
                "m1-quick": {"type": "command", "command": "cmux mosh-tmux home", "shortcut": "cmd+shift+m"},
            },
            "shortcuts": {"bindings": {"newTab": "none", "splitRight": "cmd+d"}},
        }
        changes = laptop.localize_cmux(config, "")
        self.assertNotIn("shortcut", config["actions"]["m1-workspace"])
        self.assertEqual(config["actions"]["m1-quick"]["shortcut"], "cmd+shift+m")
        self.assertEqual(config["shortcuts"]["bindings"], {"splitRight": "cmd+d"})
        self.assertEqual(len(changes), 2)

    def test_legacy_new_workspace_command_is_removed(self):
        config = {"newWorkspaceCommand": "M1",
                  "commands": [{"name": "M1", "command": "cmux mosh-tmux home"}, {"name": "Build"}]}
        laptop.localize_cmux(config, "")
        self.assertNotIn("newWorkspaceCommand", config)
        self.assertEqual(len(config["commands"]), 2)

    def test_local_configuration_is_left_alone(self):
        config = {"ui": {"newWorkspace": {"action": "worktree-agents"}},
                  "actions": {"worktree-agents": {"type": "workspaceCommand"}},
                  "automation": {"claudeBinaryPath": "/opt/claude"}}
        before = json.dumps(config)
        self.assertEqual(laptop.localize_cmux(config, "/x/claude-proxy"), [])
        self.assertEqual(json.dumps(config), before)


class ShellBlockTests(unittest.TestCase):
    def test_add_is_idempotent_and_remove_restores_the_file(self):
        original = "export PATH=\"$HOME/.local/bin:$PATH\"\n"
        added = laptop.add_shell_block(original)
        self.assertIn("export CLIPROXY_HOME_DEFAULT=0", added)
        self.assertEqual(laptop.add_shell_block(added), added)
        self.assertEqual(laptop.remove_shell_block(added), original)
        self.assertEqual(laptop.remove_shell_block(original), original)


class ApplyUndoTests(unittest.TestCase):
    """apply then undo on a fake home, with SSH, launchctl and the proxies stubbed."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.paths = laptop.Paths(self.home)
        self.paths.config.mkdir(parents=True)
        for name in ("client-api-key", "m1-client-api-key"):
            (self.paths.config / name).write_text("key\n")
            (self.paths.config / name).chmod(0o600)
        self.paths.cmux.parent.mkdir(parents=True)
        self.paths.cmux.write_text(REMOTE_FIRST_CMUX)
        self.paths.zshrc.write_text("# mine\n")
        self.paths.bin.mkdir(parents=True)
        (self.paths.bin / "claude").write_text("native")
        (self.paths.bin / "claude-m1").write_text("old launcher")
        self.paths.retired.mkdir(parents=True)
        for label in ("io.tstout.cliproxy-m1-tunnel", "io.tstout.cliproxy-switch", "com.example.unrelated"):
            (self.paths.retired / f"{label}.plist").write_text(label)
        self.loaded = set()

        def run(command, **options):
            if command[:2] == ["/bin/launchctl", "bootstrap"]:
                self.loaded.add(Path(command[3]).stem)
            elif command[:2] == ["/bin/launchctl", "bootout"]:
                self.loaded.discard(command[2].rsplit("/", 1)[-1])
            return mock.Mock(returncode=0, stdout="", stderr="")

        sources = {"scripts/proxy-agent.sh": b"new launcher", "scripts/daily-proxy-shell.sh": b"shell helper"}
        for target, value in [
            ("run", run),
            ("check_guards", lambda paths: None),
            ("fetch_from_host", sources.__getitem__),
            ("launch_agent_label", lambda plist: plist.stem),
            ("loaded", lambda label: label in self.loaded),
            ("reload_cmux", lambda: "reloaded"),
            ("verify", lambda paths: None),
        ]:
            patcher = mock.patch.object(laptop, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_partial_apply_asks_for_undo_first(self):
        with mock.patch.object(laptop, "fetch_from_host", side_effect=SystemExit("host unreachable")):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                laptop.apply(self.paths)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "undo"):
            laptop.apply(self.paths)
        with redirect_stdout(io.StringIO()):
            laptop.undo(self.paths)
            laptop.apply(self.paths)
        self.assertTrue(json.loads(self.paths.state.read_text())["complete"])

    def test_apply_switches_to_local_and_undo_restores_everything(self):
        with redirect_stdout(io.StringIO()):
            laptop.apply(self.paths)
        config = laptop.parse_jsonc(self.paths.cmux.read_text())
        self.assertEqual(config["ui"]["newWorkspace"]["action"], "cmux.newWorkspace")
        self.assertEqual(config["automation"]["claudeBinaryPath"], str(self.paths.bin / "claude-proxy"))
        self.assertIn(laptop.BEGIN, self.paths.zshrc.read_text())
        self.assertEqual((self.paths.bin / "claude-m1").read_text(), "new launcher")
        self.assertTrue((self.paths.bin / "codex-m1").stat().st_mode & 0o100)
        self.assertEqual(self.loaded, {"io.tstout.cliproxy-m1-tunnel", "io.tstout.cliproxy-switch"})
        self.assertFalse((self.paths.agents / "com.example.unrelated.plist").exists())

        with redirect_stdout(io.StringIO()):
            laptop.apply(self.paths)  # A second apply changes nothing.
        with redirect_stdout(io.StringIO()):
            laptop.undo(self.paths)
        self.assertEqual(self.paths.cmux.read_text(), REMOTE_FIRST_CMUX)
        self.assertEqual(self.paths.zshrc.read_text(), "# mine\n")
        self.assertEqual((self.paths.bin / "claude-m1").read_text(), "old launcher")
        self.assertEqual(self.loaded, set())
        self.assertEqual(list(self.paths.agents.glob("*.plist")), [])
        self.assertFalse(self.paths.state.exists())


if __name__ == "__main__":
    unittest.main()
