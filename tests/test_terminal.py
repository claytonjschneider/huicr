import errno
import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from test_review import RepositoryFixture


class TerminalTest(RepositoryFixture):
    def test_interactive_comment_save_blame_and_reopen(self):
        self.write("file.txt", "review this line\nsecond\nthird\n")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 110, 0, 0))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "HUICR_"))}
        env.update(TERM="xterm-256color", HUICR_STATE_DIR=str(self.store.directory),
                   HERDR_PLUGIN_CONFIG_DIR=str(Path(self.temp.name) / "no-config"))
        executable = Path(__file__).resolve().parents[1] / "bin/huicr.py"
        process = subprocess.Popen([sys.executable, str(executable), "review", "--repo", str(self.root)],
                                   env=env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        def stop():
            if process.poll() is None:
                process.kill()
            process.wait()
        self.addCleanup(stop)
        output = bytearray()

        def wait_for(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as e:
                        if e.errno != errno.EIO:
                            raise
                        # Linux reports EIO at PTY EOF, sometimes just before
                        # waitpid observes the child's exit. Check the expected
                        # outcome before treating it as an early failure.
                        if predicate():
                            return
                        if process.poll() is not None:
                            break
                        continue
                if predicate():
                    return
            self.fail(f"UI did not reach expected state; exit={process.poll()} output={output[-3000:]!r}")

        wait_for(lambda: b"review this line" in output)
        os.write(master, b"C")
        wait_for(lambda: b"Enter newline" in output)
        output.clear()
        cancelled_at = time.monotonic()
        os.write(master, b"\x1b")
        # Cancelling the actual inline editor hides its insertion caret. A
        # default ncurses ESCDELAY would take ~1 s here instead of ~25 ms.
        wait_for(lambda: b"\x1b[?25l" in output, timeout=0.8)
        self.assertLess(time.monotonic() - cancelled_at, 0.8)
        self.assertEqual(self.store.comments(str(self.root)), [])
        output.clear()
        os.write(master, b"C")
        wait_for(lambda: b"Enter newline" in output)
        os.write(master, "Please handle Unicode π\nAnd this second line\x13".encode())
        wait_for(lambda: bool(self.store.comments(str(self.root))))
        comments = self.store.comments(str(self.root))
        self.assertEqual(comments[0]["body"], "Please handle Unicode π\nAnd this second line")
        os.write(master, b"a")
        wait_for(lambda: b"blame: new" in output)
        os.write(master, b"L")
        wait_for(lambda: b"Saved comments" in output)
        os.write(master, b"\r")
        wait_for(lambda: b"Original comment snapshot" in output)
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.store.comments(str(self.root))[0]["body"], comments[0]["body"])
