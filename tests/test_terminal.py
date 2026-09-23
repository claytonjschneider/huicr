import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time

from test_review import RepositoryFixture


class TerminalTest(RepositoryFixture):
    def start_ui(self, *args, extra_env=None):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 110, 0, 0))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "HUICR_"))}
        env.update(TERM="xterm-256color", HUICR_STATE_DIR=str(self.store.directory),
                   HERDR_PLUGIN_CONFIG_DIR=str(Path(self.temp.name) / "no-config"))
        env.update(extra_env or {})
        executable = Path(__file__).resolve().parents[1] / "bin/huicr.py"
        process = subprocess.Popen([sys.executable, str(executable), "review", "--repo", str(self.root), *args],
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

        return master, process, output, wait_for

    def test_interactive_comment_save_blame_and_reopen(self):
        self.write("file.txt", "review this line\nsecond\nthird\n")
        master, process, output, wait_for = self.start_ui()
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
        os.write(master, "Please handle Unicode π\x1b[13;2uAnd this second line\x13".encode())
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

    def test_modified_keys_finish_comments_and_work_in_search(self):
        self.write("file.txt", "review this line\nsecond\nthird\n")
        master, process, output, wait_for = self.start_ui()
        wait_for(lambda: b"review this line" in output)
        os.write(master, b"C")
        wait_for(lambda: b"Enter finish" in output)
        os.write(master, b"Please remove wrong\x1b\x7fword\x1b[13;2uSecond line junk\x1b[127;3ukept\x1b[13u")
        wait_for(lambda: bool(self.store.comments(str(self.root))))
        self.assertEqual(self.store.comments(str(self.root))[0]["body"], "Please remove word\nSecond line kept")
        os.write(master, b"/")
        wait_for(lambda: b"Find in diff" in output)
        os.write(master, b"review junk\x1b\x7fthis\r")
        wait_for(lambda: self.store.get(str(self.root), "ui", {}).get("cursor", 0) > 0)
        output.clear()
        os.write(master, b"c")
        wait_for(lambda: b"Enter finish" in output)
        os.write(master, b"Found the intended line\r")
        wait_for(lambda: len(self.store.comments(str(self.root))) == 2)
        self.assertEqual(self.store.comments(str(self.root))[1]["anchor"]["start"], 1)
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)

    def test_multiline_paste_stays_in_the_comment_until_enter_and_modes_are_restored(self):
        self.write("file.txt", "paste test\nsecond\nthird\n")
        master, process, output, wait_for = self.start_ui()
        wait_for(lambda: b"paste test" in output)
        self.assertIn(b"\x1b[>1u", output)
        self.assertIn(b"\x1b[?2004h", output)
        os.write(master, b"C")
        wait_for(lambda: b"Enter finish" in output)
        os.write(master, "\x1b[200~First line\r\nSecond line P q\nUnicode π\x1b[201~".encode())
        wait_for(lambda: "Unicode π".encode() in output)
        self.assertEqual(self.store.comments(str(self.root)), [])
        os.write(master, b"\r")
        wait_for(lambda: bool(self.store.comments(str(self.root))))
        self.assertEqual(self.store.comments(str(self.root))[0]["body"], "First line\nSecond line P q\nUnicode π")
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)
        self.assertIn(b"\x1b[?2004l", output)
        self.assertIn(b"\x1b[<u", output)

    def test_wrapping_defaults_on_and_toggle_survives_reopening_an_explicit_scope(self):
        self.write("file.txt", "x" * 204 + "WRAPTAIL\nsecond\nthird\n")
        master, process, output, wait_for = self.start_ui()
        wait_for(lambda: b"WRAPTAIL" in output)
        self.assertIn(b"wrap:on", output)
        os.write(master, b"w")
        wait_for(lambda: b"Text wrapping off" in output)
        self.assertFalse(self.store.get(str(self.root), "ui")["wrap"])
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)

        master, process, output, wait_for = self.start_ui("--scope", "unstaged")
        wait_for(lambda: b"wrap:off" in output and b"x" * 20 in output)
        self.assertNotIn(b"WRAPTAIL", output)
        os.write(master, b"w")
        wait_for(lambda: b"WRAPTAIL" in output)
        self.assertTrue(self.store.get(str(self.root), "ui")["wrap"])
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)

    def test_publish_pr_from_terminal_and_cli_with_simulated_github(self):
        self.write("file.txt", "review this PR line\nsecond\nthird\n")
        head = self.commit("PR change")
        url = "https://github.com/example/demo/pull/7"
        info = {"state": "open", "merged": False, "html_url": url, "title": "Example PR", "commits": 1,
                "head": {"sha": head}, "base": {"sha": self.base, "ref": "main"}}
        remote = Path(self.temp.name) / "remote.git"
        self.git("clone", "--bare", str(self.root), str(remote))
        self.git("update-ref", "refs/pull/7/head", head, cwd=remote)
        self.git("config", f"url.{remote}.insteadOf", "https://github.com/example/demo.git")
        tools = Path(self.temp.name) / "tools"
        tools.mkdir()
        gh = tools / "gh"
        gh.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""
            import json
            import os
            from pathlib import Path
            import sys

            assert os.environ.get("GH_PROMPT_DISABLED") == "1"
            args = sys.argv[1:]
            info = json.loads(os.environ["HUICR_TEST_PR"])
            if "--method" in args:
                assert args[args.index("--method") + 1] == "POST"
                payload = json.load(sys.stdin)
                path = Path(os.environ["HUICR_TEST_POSTS"])
                posts = json.loads(path.read_text()) if path.exists() else []
                posts.append(payload)
                path.write_text(json.dumps(posts))
                print(json.dumps({"id": len(posts), "state": "COMMENTED", "commit_id": payload["commit_id"],
                                  "body": payload["body"], "html_url": info["html_url"] + "#pullrequestreview-1"}))
            elif "--jq" in args:
                print(info["head"]["sha"])
            elif "--slurp" in args:
                print(os.environ["HUICR_TEST_FILES"])
            else:
                print(json.dumps(info))
        """))
        gh.chmod(0o755)
        posts = Path(self.temp.name) / "posted-reviews.json"
        environment = {"PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
                       "HUICR_TEST_PR": json.dumps(info), "HUICR_TEST_POSTS": str(posts),
                       "HUICR_TEST_FILES": json.dumps([[{"filename": "file.txt", "patch": self.repo.text("diff", self.base, head)}]])}
        master, process, output, wait_for = self.start_ui(url, extra_env=environment)
        wait_for(lambda: b"review this PR line" in output)
        os.write(master, b"C")
        wait_for(lambda: b"Enter newline" in output)
        os.write(master, b"Please review this file\x13")
        wait_for(lambda: bool(self.store.comments(str(self.root))))
        os.write(master, b"P")
        wait_for(lambda: b"GitHub review:" in output)
        comment = self.store.comments(str(self.root))[0]
        self.assertEqual((comment["github_version"], comment["sent_version"]), (1, 0))
        self.assertIn("Please review this file", json.loads(posts.read_text())[0]["body"])
        os.write(master, b"q")
        wait_for(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)

        self.store.edit(comment["id"], "Updated file feedback")
        environment.update(HUICR_STATE_DIR=str(self.store.directory),
                           HERDR_PLUGIN_CONFIG_DIR=str(Path(self.temp.name) / "no-config"))
        executable = Path(__file__).resolve().parents[1] / "bin/huicr.py"
        result = subprocess.run([sys.executable, str(executable), "publish", url, "--repo", str(self.root), "--id", comment["id"]],
                                env={**os.environ, **environment}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(url, result.stdout.decode())
        self.assertEqual(self.store.comment(comment["id"])["github_version"], 2)
        self.assertEqual(len(json.loads(posts.read_text())), 2)
