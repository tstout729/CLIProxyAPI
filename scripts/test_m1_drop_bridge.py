import fcntl
import importlib.util
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest


SCRIPTS = Path(__file__).parent
spec = importlib.util.spec_from_file_location("m1_drop_bridge", SCRIPTS / "m1-drop-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

SHOT = "/var/folders/vs/x/T/TemporaryItems/NSIRD_screencaptureui_AbC/Screenshot 2026-10-09 at 1.23.45 PM.png"
DROPPED = bridge.escape(SHOT)


class ParseTests(unittest.TestCase):
    def test_round_trips_ghostty_escaping(self):
        self.assertEqual(DROPPED, SHOT.replace(" ", "\\ "))
        self.assertEqual(bridge.split_paths(DROPPED), [SHOT])
        odd = "/tmp/a (1) [x] it's $HOME & more!.png"
        self.assertEqual(bridge.split_paths(bridge.escape(odd) + " /tmp/b.png"), [odd, "/tmp/b.png"])

    def test_ordinary_text_is_not_a_drop(self):
        for text in ["hello", "/tmp/a.png\n", "ls /tmp", "~/a.png", ""]:
            self.assertIsNone(bridge.split_paths(text), text)

    def test_rewrite_uploads_only_files_that_exist_here(self):
        uploads = []
        def upload(path):
            uploads.append(path)
            return "/Users/me/Drops/2026-10-09/134500-" + os.path.basename(path)
        text = DROPPED + " /not/here.png"
        out = bridge.rewrite(text, upload=upload, exists=lambda p: p == SHOT)
        self.assertEqual(uploads, [SHOT])
        self.assertEqual(bridge.split_paths(out),
                         ["/Users/me/Drops/2026-10-09/134500-" + os.path.basename(SHOT), "/not/here.png"])
        self.assertIsNone(bridge.rewrite("/not/here.png", upload=upload, exists=lambda p: False))

    def test_failed_upload_keeps_the_original_path(self):
        def upload(path):
            raise RuntimeError("offline")
        self.assertEqual(bridge.rewrite(DROPPED, upload=upload, exists=lambda p: True), DROPPED)


class FilterTests(unittest.TestCase):
    def make(self):
        return bridge.InputFilter(looks_droppable=lambda text: text.startswith("/var/"))

    def test_keys_and_ordinary_pastes_pass_straight_through(self):
        flt = self.make()
        self.assertEqual(flt.feed(b"hi\x1b"), (b"hi\x1b", None))  # a lone Escape is never held
        paste = bridge.PASTE_START + b"some text" + bridge.PASTE_END
        self.assertEqual(flt.feed(b"a" + paste + b"b"), (b"a" + paste + b"b", None))
        self.assertEqual(flt.feed(bridge.PASTE_START + b"/tmp/x.png" + bridge.PASTE_END),
                         (bridge.PASTE_START + b"/tmp/x.png" + bridge.PASTE_END, None))

    def test_bracketed_drop_is_rewritten_and_later_input_waits(self):
        flt = self.make()
        forward, drop = flt.feed(b"x" + bridge.PASTE_START + DROPPED.encode() + bridge.PASTE_END + b"y")
        self.assertEqual((forward, drop), (b"x", DROPPED))
        self.assertEqual(flt.feed(b"z"), (b"", None))
        out, again = flt.finish(DROPPED, "/Users/me/Drops/a.png")
        self.assertEqual(out, bridge.PASTE_START + b"/Users/me/Drops/a.png" + bridge.PASTE_END + b"yz")
        self.assertIsNone(again)

    def test_drop_split_across_reads(self):
        flt = self.make()
        whole = bridge.PASTE_START + DROPPED.encode() + bridge.PASTE_END
        forward, drop = flt.feed(whole[:3])
        self.assertEqual((forward, drop), (b"", None))
        forward, drop = flt.feed(whole[3:20])
        self.assertEqual((forward, drop), (b"", None))
        self.assertEqual(flt.feed(whole[20:]), (b"", DROPPED))

    def test_unbracketed_drop_and_failed_upload(self):
        flt = self.make()
        self.assertEqual(flt.feed(DROPPED.encode()), (b"", DROPPED))
        out, _ = flt.finish(DROPPED, None)
        self.assertEqual(out, DROPPED.encode())

    def test_long_paste_is_not_held(self):
        flt = self.make()
        big = b"/" + b"a" * (bridge.MAX_PASTE + 10)
        forward, drop = flt.feed(bridge.PASTE_START + big)
        self.assertEqual((forward, drop), (bridge.PASTE_START + big, None))
        self.assertEqual(flt.feed(b"tail" + bridge.PASTE_END), (b"tail" + bridge.PASTE_END, None))


RECORDER = r"""
import fcntl, os, struct, sys, termios, tty
tty.setraw(0)
rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2]
data = b""
while not data.endswith(b"END"):
    data += os.read(0, 4096)
open(sys.argv[1], "wb").write(f"{rows}x{cols}\n".encode() + data)
os.write(1, b"done")
sys.exit(3)
"""


class PtyTests(unittest.TestCase):
    def test_passes_input_size_output_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "record"
            recorder = Path(directory) / "recorder.py"
            recorder.write_text(RECORDER)
            master, slave = os.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 101, 0, 0))
            env = dict(os.environ, M1_DROP_LOG=str(Path(directory) / "log"))
            proc = subprocess.Popen([sys.executable, str(SCRIPTS / "m1-drop-bridge.py"), "--",
                                     sys.executable, str(recorder), str(record)],
                                    stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            time.sleep(0.5)
            sent = (b"keys\x03\x1b[A" + bridge.PASTE_START + b"plain paste" + bridge.PASTE_END
                    + bridge.PASTE_START + b"/no/such/file.png" + bridge.PASTE_END + b"END")
            os.write(master, sent)
            output = b""
            end = time.time() + 5
            while proc.poll() is None and time.time() < end:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        break
            self.assertEqual(proc.wait(timeout=5), 3)
            self.assertIn(b"done", output)
            self.assertEqual(record.read_bytes(), b"33x101\n" + sent)


if __name__ == "__main__":
    unittest.main()
