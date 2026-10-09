import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("home_session", Path(__file__).with_name("home-coding-session.py"))
home = importlib.util.module_from_spec(spec)
spec.loader.exec_module(home)


class HomeCodingTests(unittest.TestCase):
    def test_first_number_keeps_legacy_workspace_when_tmux_session_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "projects/home-coding/home-codex-main"
            legacy.mkdir(parents=True)
            marker = legacy / "existing-work.txt"
            marker.write_text("existing work")
            options = type("Options", (), {"name": "1", "project": None, "detach": True})()
            responses = [subprocess.CompletedProcess([], code, value, "") for code, value in [(1, ""), (1, ""), (0, ""), (0, str(legacy) + "\n")]]
            with patch.object(home.Path, "home", return_value=root), patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
                home.host_session("codex", options, [])
            self.assertIn(str(legacy), run.call_args_list[2].args[0])
            self.assertEqual(marker.read_text(), "existing work")
            self.assertFalse((root / "projects/home-coding/home-codex-1").exists())

    def test_first_number_reuses_live_legacy_default(self):
        options = type("Options", (), {"name": "1", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], code, value, "") for code, value in [(1, ""), (0, ""), (0, "/tmp/project\n"), (0, "0\n")]]
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("codex", options, [])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("=home-codex-main:", commands[-1])
        self.assertTrue(all(not {"new-session", "respawn-pane", "rename-session"}.intersection(command) for command in commands))

    def test_first_number_preserves_distinct_existing_number_and_default(self):
        options = type("Options", (), {"name": "1", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/numbered-project\n", "0\n"]]
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, [])
        self.assertIn("=home-claude-1:", run.call_args.args[0])
        self.assertTrue(all("home-claude-main" not in " ".join(call.args[0]) for call in run.call_args_list))

    def test_numbered_session_restarts_exited_agent_without_killing_live_pane(self):
        options = type("Options", (), {"name": "2", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/project\n", "1\n", ""]]
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("codex", options, [])
        self.assertIn("respawn-pane", run.call_args.args[0])
        self.assertNotIn("-k", run.call_args.args[0])

    def test_project_session_restarts_exited_agent(self):
        options = type("Options", (), {"name": "simply-ops", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/simply-ops\n", "1\n", ""]]
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, [])
        self.assertIn("respawn-pane", run.call_args.args[0])
        self.assertIn("=home-claude-simply-ops:", run.call_args.args[0])

    def test_fresh_session_closes_when_agent_exits(self):
        options = type("Options", (), {"name": "app-1008-091500", "project": "/tmp", "detach": True, "fresh": True})()
        responses = [subprocess.CompletedProcess([], 0 if i else 1, value, "") for i, value in enumerate(["", "", "", "", "/private/tmp\n"])]
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, [])
        commands = [" ".join(call.args[0]) for call in run.call_args_list]
        self.assertTrue(any("new-session" in command for command in commands))
        self.assertIn("set-option -t =home-claude-app-1008-091500: remain-on-exit off", commands[2])
        self.assertIn("set-option -t =home-claude-app-1008-091500: status off", commands[3])

    def test_numbered_list_orders_numbers_and_preserves_legacy_names(self):
        output = "\n".join([
            "home-codex-10\t0\t0\t/tmp/ten",
            "home-codex-main\t0\t0\t/tmp/main",
            "home-codex-2\t1\t0\t/tmp/two",
            "home-claude-1\t0\t1\t/tmp/claude-one",
            "home-claude-main\t0\t0\t/tmp/claude-main",
        ])
        rows = home.format_sessions(output).splitlines()
        codex = [line.split() for line in rows if line.startswith("Codex")]
        self.assertEqual([row[1] for row in codex], ["1", "2", "10"])
        self.assertEqual(codex[1][2], "exited")
        claude = [line.split() for line in rows if line.startswith("Claude")]
        self.assertEqual([row[1] for row in claude], ["1", "main"])

    def test_short_commands_default_to_first_number_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launchers = root / ".local/bin"
            launchers.mkdir(parents=True)
            for agent in ["codex", "claude"]:
                launcher = launchers / (agent + "-home")
                launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
                launcher.chmod(0o755)
                script = Path(__file__).with_name(agent + "-home-short.sh")
                for arguments in [[], ["2", "/tmp/space and 'quote'", "--", "$(not-a-command)"]]:
                    with self.subTest(agent=agent, arguments=arguments):
                        result = subprocess.run(["/bin/sh", str(script), *arguments], env={**os.environ, "HOME": directory}, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.splitlines(), arguments or ["1"])

    def test_bare_shortcuts_open_default_remote_session(self):
        for agent in ["codex", "claude"]:
            with self.subTest(agent=agent), patch.object(home.sys, "argv", ["home-coding-session", "client", agent]), patch.object(home, "client_session") as launch:
                home.main()
            selected_agent, options, arguments = launch.call_args.args
            self.assertEqual((selected_agent, options.name, options.project), (agent, "main", None))
            self.assertEqual(arguments, [])

    def test_default_session_restarts_exited_agent(self):
        options = type("Options", (), {"name": "main", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/project\n", "1\n", ""]]
        with patch.object(home, "tmux_command", return_value=(["tmux", "-L", home.SOCKET], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("codex", options, [])
        last = run.call_args.args[0]
        self.assertIn("respawn-pane", last)
        self.assertNotIn("-k", last)
        self.assertIn("/tmp/project", last)

    def test_default_session_never_restarts_live_agent(self):
        options = type("Options", (), {"name": "main", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/project\n", "0\n"]]
        with patch.object(home, "tmux_command", return_value=(["tmux", "-L", home.SOCKET], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, [])
        self.assertTrue(all("respawn-pane" not in call.args[0] for call in run.call_args_list))

    def test_detached_launcher_accepts_normal_project_arguments(self):
        argv = ["home-coding-session", "host", "codex", "--detach", "task", "/tmp/project", "--", "exec", "a quoted prompt"]
        with patch.object(home.sys, "argv", argv), patch.object(home, "host_session") as launch:
            home.main()
        agent, options, arguments = launch.call_args.args
        self.assertEqual((agent, options.name, options.project, options.detach), ("codex", "task", "/tmp/project", True))
        self.assertEqual(arguments, ["exec", "a quoted prompt"])

    def test_ssh_preserves_literal_project_and_prompt(self):
        options = type("Options", (), {"name": "task", "project": "/tmp/O'Brien $(touch unwanted)", "detach": True})()
        prompt = "Read 'quoted' text; $(touch unwanted); do not run commands."
        with patch.dict(os.environ, {"HOME_CODING_SSH_HOST": "user@home"}, clear=True), patch.object(home.os, "execvp") as execute, contextlib.redirect_stdout(io.StringIO()):
            home.client_session("codex", options, [prompt])
        command = execute.call_args.args[1]
        remote = shlex.split(command[-1])
        self.assertEqual(remote[-1], prompt)
        self.assertIn(options.project, remote)
        self.assertNotIn("-t", command)

    def test_attach_uses_mosh_with_literal_arguments_when_installed(self):
        options = type("Options", (), {"name": "task", "project": None, "detach": False, "fresh": True})()
        prompt = "Read 'quoted' text; $(touch unwanted)"
        with patch.dict(os.environ, {"HOME_CODING_SSH_HOST": "user@home"}, clear=True), \
                patch.object(home.shutil, "which", return_value="/opt/homebrew/bin/mosh"), \
                patch.object(home.sys.stdin, "isatty", return_value=True), \
                patch.object(home.os, "execvp") as execute, patch.object(home, "attach_with_reconnect") as fallback:
            home.client_session("claude", options, [prompt])
        fallback.assert_not_called()
        command = execute.call_args.args[1]
        self.assertEqual(command[0], "/opt/homebrew/bin/mosh")
        self.assertIn("--server=env LANG=en_US.UTF-8 /opt/homebrew/bin/mosh-server", command)
        boundary = command.index("--")
        self.assertEqual(command[boundary - 1], "user@home")
        self.assertEqual(command[-1], prompt)
        self.assertNotIn("-t", shlex.split(command[1].removeprefix("--ssh=")))

    def test_attach_keeps_ssh_without_mosh_or_when_requested(self):
        options = type("Options", (), {"name": "task", "project": None, "detach": False, "fresh": True})()
        for env, which in (({}, None), ({"HOME_CODING_TRANSPORT": "ssh"}, "/opt/homebrew/bin/mosh")):
            with self.subTest(env=env), patch.dict(os.environ, {"HOME_CODING_SSH_HOST": "user@home", **env}, clear=True), \
                    patch.object(home.shutil, "which", return_value=which), \
                    patch.object(home.sys.stdin, "isatty", return_value=True), \
                    patch.object(home.os, "execvp") as execute, patch.object(home, "attach_with_reconnect") as fallback:
                home.client_session("claude", options, [])
            execute.assert_not_called()
            fallback.assert_called_once()

    def test_ssh_can_use_installed_python_without_developer_tools(self):
        options = type("Options", (), {"name": "main", "project": None, "detach": True})()
        with patch.dict(os.environ, {"HOME_CODING_SSH_HOST": "user@home", "HOME_CODING_REMOTE_PYTHON": "/opt/homebrew/bin/python3"}, clear=True), patch.object(home.os, "execvp") as execute, contextlib.redirect_stdout(io.StringIO()):
            home.client_session("codex", options, [])
        remote = shlex.split(execute.call_args.args[1][-1])
        self.assertEqual(remote[:2], ["/opt/homebrew/bin/python3", ".local/bin/home-coding-session"])

    def test_gateway_key_only_enters_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("test-only-inference-key\n")
            key.chmod(0o600)
            with patch.dict(os.environ, {"CPA_PROXY_KEY_FILE": str(key)}, clear=True), patch.object(home.shutil, "which", return_value="/usr/bin/codex"), patch.object(home.sys, "platform", "linux"), patch.object(home.os, "execvpe") as execute:
                home.run_agent("codex", ["--help"])
            program, arguments, env = execute.call_args.args
            self.assertEqual(env["CLIPROXY_API_KEY"], "test-only-inference-key")
            self.assertNotIn("test-only-inference-key", " ".join(arguments))
            self.assertEqual(env["TZ"], "America/Los_Angeles")
            key.chmod(0o644)
            with patch.dict(os.environ, {"CPA_PROXY_KEY_FILE": str(key)}, clear=True), self.assertRaisesRegex(SystemExit, "private"):
                home.run_agent("codex", [])

    def test_existing_session_does_not_resubmit_prompt(self):
        options = type("Options", (), {"name": "task", "project": None, "detach": True})()
        responses = [subprocess.CompletedProcess([], 0, value, "") for value in ["", "/tmp/old-project\n", "0\n"]]
        with patch.object(home, "tmux_command", return_value=(["tmux", "-L", home.SOCKET], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, ["Do not repeat this prompt"])
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all("send-keys" not in call.args[0] and "new-session" not in call.args[0] for call in run.call_args_list))

    def test_conflicting_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            options = type("Options", (), {"name": "task", "project": directory, "detach": True})()
            responses = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "/tmp/different-project\n", "")]
            with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", side_effect=responses), self.assertRaisesRegex(SystemExit, "already uses"):
                home.host_session("codex", options, [])

    def test_missing_project_never_starts_agent(self):
        options = type("Options", (), {"name": "task", "project": "/nonexistent-home-coding-project", "detach": True})()
        with patch.object(home, "tmux_command", return_value=(["tmux"], {})), patch.object(home.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run, self.assertRaisesRegex(SystemExit, "does not exist"):
            home.host_session("codex", options, [])
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
