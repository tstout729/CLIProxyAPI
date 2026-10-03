"""Protect M1-first proxy selection with fallback to the local proxy."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name("proxy-agent.sh")
M1 = "http://127.0.0.1:18318"
LOCAL = "http://127.0.0.1:8318"


class ProxyAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = root / "config"
        self.config.mkdir()
        (self.config / "m1-client-api-key").write_text("m1-key")
        (self.config / "client-api-key").write_text("local-key")
        self.bin = root / "bin"
        self.bin.mkdir()
        # Fake curl: a proxy is healthy when its URL is listed in TEST_HEALTHY and the key matches.
        fake_curl = self.bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "url = sys.argv[-1]; header = sys.stdin.read()\n"
            "healthy = os.environ.get('TEST_HEALTHY', '').split()\n"
            "key = {'" + M1 + "/v1/models': 'm1-key', '" + LOCAL + "/v1/models': 'local-key'}.get(url)\n"
            "if url.rsplit('/v1/models', 1)[0] in healthy and key and key in header:\n"
            "    print('{\"data\":[{\"id\":\"m\"}]}')\n"
            "else:\n"
            "    sys.exit(22)\n"
        )
        for agent in ("claude", "codex"):
            native = self.bin / agent
            native.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "print(json.dumps({'args': sys.argv[1:], 'base': os.environ.get('ANTHROPIC_BASE_URL'),\n"
                "  'token': os.environ.get('ANTHROPIC_AUTH_TOKEN'), 'codex_key': os.environ.get('CLIPROXY_API_KEY')}))\n"
            )
            (self.bin / (agent + "-m1")).symlink_to(SOURCE)
        for path in self.bin.iterdir():
            if not path.is_symlink():
                path.chmod(0o755)

    def run_agent(self, agent, *arguments, healthy="", route=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLIPROXY_"))}
        env.update({"PATH": f"{self.bin}{os.pathsep}{env['PATH']}", "CLIPROXY_CONFIG_DIR": str(self.config),
                    "TEST_HEALTHY": healthy})
        if route:
            env["CLIPROXY_ROUTE"] = route
        return subprocess.run([str(self.bin / (agent + "-m1")), *arguments],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_uses_m1_when_healthy(self):
        result = self.run_agent("claude", "-p", "hi", healthy=f"{M1} {LOCAL}")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual((call["base"], call["token"], call["args"]), (M1, "m1-key", ["-p", "hi"]))
        self.assertEqual(result.stderr, "")

    def test_falls_back_to_local_when_m1_is_down(self):
        result = self.run_agent("claude", healthy=LOCAL)
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual((call["base"], call["token"]), (LOCAL, "local-key"))
        self.assertIn("using the local proxy", result.stderr)

    def test_codex_fallback_targets_local_responses_endpoint(self):
        result = self.run_agent("codex", "resume", "--last", healthy=LOCAL)
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertEqual(call["codex_key"], "local-key")
        self.assertIn(f'model_providers.cliproxy_m1.base_url="{LOCAL}/v1"', call["args"])
        self.assertEqual(call["args"][-2:], ["resume", "--last"])

    def test_fails_clearly_when_no_proxy_is_usable(self):
        result = self.run_agent("claude", healthy="")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("cliproxy-local-login", result.stderr)

    def test_forced_route_skips_probe(self):
        result = self.run_agent("claude", healthy="", route="local")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["base"], LOCAL)


if __name__ == "__main__":
    unittest.main()
