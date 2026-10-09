import base64
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest


SCRIPTS = Path(__file__).parent


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class PbcopyTests(unittest.TestCase):
    """Runs the pbcopy shim against a private tabs server with a terminal attached."""

    def setUp(self):
        self.socket = f"home-pbcopy-test-{os.getpid()}"
        self.dir = tempfile.TemporaryDirectory()
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "sleep 600")
        # The tabs server's real settings, with the sidebar hooks doing nothing.
        self.tmux("set", "-g", "@m1_sidebar_cmd", "true", ";", "source-file", str(SCRIPTS / "m1-tabs.tmux.conf"))
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(shutil.which("tmux"), ["tmux", "-L", self.socket, "attach", "-t", "main"],
                      dict(self.env, TERM="xterm-256color"))
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.output = b""
        self.assertTrue(self.read_until(lambda: self.tmux("list-clients", "-F", "#{client_name}").strip()))

    def tearDown(self):
        self.tmux("kill-server")
        os.close(self.fd)
        os.waitpid(self.pid, 0)
        self.dir.cleanup()

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True, env=self.env).stdout

    def read_until(self, predicate, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    break
            if predicate():
                return True
        return False

    def test_copied_text_reaches_the_terminal_and_this_mac(self):
        local = Path(self.dir.name) / "local-clipboard"
        fake = Path(self.dir.name) / "pbcopy"
        fake.write_text(f"#!/bin/sh\ncat > '{local}'\n")
        fake.chmod(0o755)
        text = "copied on the home host ✓"
        env = dict(self.env, M1_TABS_SOCKET=self.socket, M1_PBCOPY=str(fake))
        subprocess.run([str(SCRIPTS / "home-shims" / "pbcopy")], input=text.encode(), env=env, check=True)
        escape = b"\x1b]52;c;" + base64.b64encode(text.encode())
        # The "c" clipboard name matters: mosh drops the escape without it.
        self.assertTrue(self.read_until(lambda: escape in self.output), self.output[-200:])
        self.assertEqual(local.read_text(), text)

    def test_other_pasteboards_stay_local(self):
        fake = Path(self.dir.name) / "pbcopy"
        fake.write_text("#!/bin/sh\ncat > /dev/null\n")
        fake.chmod(0o755)
        env = dict(self.env, M1_TABS_SOCKET=self.socket, M1_PBCOPY=str(fake))
        subprocess.run([str(SCRIPTS / "home-shims" / "pbcopy"), "-pboard", "find"], input=b"needle", env=env,
                       check=True)
        self.assertFalse(self.read_until(lambda: b"\x1b]52;" in self.output, seconds=1))


if __name__ == "__main__":
    unittest.main()
