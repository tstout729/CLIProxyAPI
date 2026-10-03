"""Protect local/remote routing, direct fallback, and shell argument handling."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name("daily-proxy-shell.sh")
SHELLS = [path for shell in ("zsh", "bash") if (path := shutil.which(shell))]


class DailyProxyShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.native = self.root / "native"
        self.native.mkdir()
        self.launchers = self.root / ".local/bin"
        self.launchers.mkdir(parents=True)
        for agent in ("codex", "claude"):
            # This stub captures only fake test state; no real credentials are read.
            native = self.native / agent
            native.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "print(json.dumps({'args':sys.argv[1:], 'proxy':os.environ.get('TEST_PROXY')}))\n"
                "sys.exit(int(os.environ.get('TEST_NATIVE_EXIT', '0')))\n"
            )
            native.chmod(0o755)
            proxy = self.launchers / (agent + "-m1")
            proxy.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${TEST_PROXY_EXIT:-}\" ]; then exit \"$TEST_PROXY_EXIT\"; fi\n"
                "TEST_PROXY=m1\nexport TEST_PROXY\n"
                f'exec {agent} "$@"\n'
            )
            proxy.chmod(0o755)
            home = self.launchers / (agent + "-home")
            home.write_text(
                "#!/bin/sh\n"
                "if [ -n \"${TEST_HOME_EXIT:-}\" ]; then exit \"$TEST_HOME_EXIT\"; fi\n"
                "TEST_PROXY=home\nexport TEST_PROXY\n"
                f'exec {agent} "$@"\n'
            )
            home.chmod(0o755)

    def run_shell(self, shell, command, *arguments, extra=None):
        env = os.environ.copy()
        for key in ("TEST_PROXY", "TEST_PROXY_EXIT", "TEST_HOME_EXIT", "TEST_NATIVE_EXIT"):
            env.pop(key, None)
        env.update({"HOME": str(self.root), "PATH": str(self.native) + os.pathsep + env["PATH"]})
        env.update(extra or {})
        return subprocess.run(
            [shell, "-f" if Path(shell).name == "zsh" else "--noprofile", "-c",
             '. "$1"; shift; ' + command, "test", str(SOURCE), *arguments],
            env=env, capture_output=True, text=True, timeout=10,
        )

    def test_plain_names_use_proxy_without_recursing_and_preserve_arguments(self):
        arguments = ["--model", "model-name", "spaces and 'quotes'", "$(never-run); `never-run`"]
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                with self.subTest(shell=shell, agent=agent):
                    result = self.run_shell(shell, agent + ' "$@"', *arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"args": arguments, "proxy": "m1"})

    def test_numbered_commands_select_home_and_preserve_all_arguments(self):
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                for number in ("1", "2", "10", "999999999999999999999"):
                    with self.subTest(shell=shell, agent=agent, number=number):
                        arguments = [number, "project with 'quotes'", "--", "$(never-run); `never-run`"]
                        result = self.run_shell(shell, 'set -u; ' + agent + ' "$@"', *arguments)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout), {"args": arguments, "proxy": "home"})

    def test_bare_commands_flags_subcommands_and_prompts_keep_local_proxy_route(self):
        cases = [[], ["exec", "1"], ["resume", "--last"], ["--model", "1"],
                 ["1 task to fix"], ["1.5"], ["-1"], ["0"], ["01"], ["1a"], [""]]
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                for arguments in cases:
                    with self.subTest(shell=shell, agent=agent, arguments=arguments):
                        result = self.run_shell(shell, 'set -u; ' + agent + ' "$@"', *arguments)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout), {"args": arguments, "proxy": "m1"})

    def test_numbered_home_failure_preserves_status_without_local_fallback(self):
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                with self.subTest(shell=shell, agent=agent):
                    result = self.run_shell(shell, agent + ' 1', extra={"TEST_HOME_EXIT": "29"})
                    self.assertEqual(result.returncode, 29)
                    self.assertEqual(result.stdout, "")

    def test_numeric_arguments_to_direct_backup_stay_native(self):
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                with self.subTest(shell=shell, agent=agent):
                    result = self.run_shell(shell, agent + '-m5 1')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"args": ["1"], "proxy": None})

    def test_direct_backups_bypass_proxy_and_preserve_arguments(self):
        for shell in SHELLS:
            for agent in ("codex", "claude"):
                with self.subTest(shell=shell, agent=agent):
                    result = self.run_shell(shell, agent + '-m5 "$@"', "resume", "chat with spaces")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"args": ["resume", "chat with spaces"], "proxy": None})

    def test_proxy_settings_do_not_leak_into_direct_backup(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_shell(shell, "claude; claude 1; claude-m5")
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual([call["proxy"] for call in calls], ["m1", "home", None])

    def test_proxy_failure_requires_explicit_fallback(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                failed = self.run_shell(shell, "codex", extra={"TEST_PROXY_EXIT": "23"})
                self.assertEqual(failed.returncode, 23)
                self.assertEqual(failed.stdout, "")
                fallback = self.run_shell(shell, "codex-m5", extra={"TEST_PROXY_EXIT": "23"})
                self.assertEqual(fallback.returncode, 0, fallback.stderr)
                self.assertIsNone(json.loads(fallback.stdout)["proxy"])

    def test_native_exit_status_is_preserved(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_shell(shell, "claude-m5", extra={"TEST_NATIVE_EXIT": "7"})
                self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
