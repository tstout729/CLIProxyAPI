import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cmux_home", Path(__file__).with_name("cmux-claude-home.py"))
cmux_home = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmux_home)

CONVERSATION = "5d27039e-f32b-4c9d-a856-62cce18e22e1"


class CmuxClaudeHomeTests(unittest.TestCase):
    def test_drops_cmux_settings_and_keeps_conversation(self):
        kept, conversation = cmux_home.split_arguments(["--settings", '{"hooks":{}}', "--resume", CONVERSATION, "--model", "opus"])
        self.assertEqual(kept, ["--resume", CONVERSATION, "--model", "opus"])
        self.assertEqual(conversation, CONVERSATION)
        kept, conversation = cmux_home.split_arguments(["--settings={}", f"--session-id={CONVERSATION}"])
        self.assertEqual(kept, [f"--session-id={CONVERSATION}"])
        self.assertEqual(conversation, CONVERSATION)

    def test_same_conversation_reuses_one_home_session(self):
        self.assertEqual(cmux_home.session_name(CONVERSATION), "cmux-" + CONVERSATION)
        self.assertRegex(cmux_home.session_name(None), r"^cmux-\d{4}-\d{6}$")

    def test_resume_syncs_transcript_then_attaches_on_host(self):
        with patch.object(cmux_home.sys, "argv", ["claude-cmux-home", "--settings", "{}", "--resume", CONVERSATION]), \
                patch.object(cmux_home.sys.stdin, "isatty", return_value=True), \
                patch.object(cmux_home, "host_reachable", return_value=True), \
                patch.object(cmux_home.Path, "exists", return_value=True), \
                patch.object(cmux_home, "project_directory", return_value="/Users/me"), \
                patch.object(cmux_home, "sync_transcript") as sync, \
                patch.object(cmux_home.os, "execv") as execute:
            cmux_home.main()
        sync.assert_called_once_with(CONVERSATION, "home")
        command = execute.call_args.args[1]
        self.assertEqual(command[1:], ["--fresh", "cmux-" + CONVERSATION, "/Users/me", "--", "--resume", CONVERSATION])

    def test_new_session_does_not_sync(self):
        with patch.object(cmux_home.sys, "argv", ["claude-cmux-home", "--session-id", CONVERSATION]), \
                patch.object(cmux_home.sys.stdin, "isatty", return_value=True), \
                patch.object(cmux_home, "host_reachable", return_value=True), \
                patch.object(cmux_home.Path, "exists", return_value=True), \
                patch.object(cmux_home, "project_directory", return_value="/Users/me"), \
                patch.object(cmux_home, "sync_transcript") as sync, \
                patch.object(cmux_home.os, "execv"):
            cmux_home.main()
        sync.assert_not_called()

    def test_unreachable_host_or_print_mode_runs_locally(self):
        for argv, reachable in ((["--resume", CONVERSATION], False), (["-p", "hi"], True)):
            with self.subTest(argv=argv), patch.object(cmux_home.sys, "argv", ["claude-cmux-home", *argv]), \
                    patch.object(cmux_home.sys.stdin, "isatty", return_value=True), \
                    patch.object(cmux_home, "host_reachable", return_value=reachable), \
                    patch.dict(os.environ, {"CLIPROXY_CLAUDE_BIN": "/bin/claude-real"}), \
                    patch.object(cmux_home.os, "execv") as execute, \
                    patch("sys.stderr"):
                cmux_home.main()
            self.assertEqual(execute.call_args.args[1], ["/bin/claude-real", *argv])


if __name__ == "__main__":
    unittest.main()
