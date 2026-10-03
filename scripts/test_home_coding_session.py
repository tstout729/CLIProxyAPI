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
        responses = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "/tmp/old-project\n", "")]
        with patch.object(home, "tmux_command", return_value=(["tmux", "-L", home.SOCKET], {})), patch.object(home.subprocess, "run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
            home.host_session("claude", options, ["Do not repeat this prompt"])
        self.assertEqual(run.call_count, 2)
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
