import json
import os
from pathlib import Path
import sys
import textwrap
from unittest.mock import patch

from huicr.config import HuicrError
from huicr.git import Repo
from huicr.github import load_pr
from test_review import RepositoryFixture


class GitHubTest(RepositoryFixture):
    def fixture(self, mode):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "first PR change\nsecond\nthird\n")
        first = self.commit("first PR change")
        self.write("file.txt", "second PR change\nsecond\nthird\n")
        head = self.commit("second PR change")
        self.git("switch", "main")
        if mode != "fast-forward":
            self.write("target-before", "target advanced during PR\n")
            self.commit("target advance")
        if mode == "merge":
            self.git("merge", "--no-ff", "feature", "-m", "merge PR")
        elif mode == "squash":
            self.git("merge", "--squash", "feature")
            self.commit("squash PR")
        elif mode == "rebase":
            self.git("cherry-pick", first, head)
        elif mode == "fast-forward":
            self.git("merge", "--ff-only", "feature")
        merge = self.git("rev-parse", "HEAD")
        self.write("after-PR", "later target change\n")
        tip = self.commit("after PR merged")
        remote = Path(self.temp.name) / "remote.git"
        self.git("clone", "--bare", str(self.root), str(remote))
        self.git("update-ref", "refs/pull/7/head", head, cwd=remote)
        self.git("config", f"url.{remote}.insteadOf", "https://github.com/example/demo.git")
        info = {"head": {"sha": head}, "base": {"sha": tip, "ref": "main"},
                "merged": True, "merge_commit_sha": merge, "commits": 2,
                "title": "Example PR", "html_url": "https://github.com/example/demo/pull/7"}
        return info, first, head

    def check_mode(self, mode):
        info, first, head = self.fixture(mode)
        index = (self.root / ".git/index").read_bytes()
        checkout = self.repo.head()
        with patch("huicr.github.gh", side_effect=[json.dumps(info), first + "\n" + head + "\n"]):
            left, right, commits, source, pr = load_pr(self.repo, info["html_url"])
        self.assertEqual(left, self.base)
        self.assertEqual(right, head)
        self.assertEqual([c.oid for c in commits], [first, head])
        self.assertIn("target main", source)
        self.assertEqual(pr, {"url": info["html_url"], "head": head, "base": info["base"]["sha"]})
        self.assertEqual([f.path for f in self.repo.changes(left, right)], ["file.txt"])
        self.assertEqual(self.repo.head(), checkout)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)

    def test_merged_pr_target_history(self):
        self.check_mode("merge")

    def test_squash_pr_target_history(self):
        self.check_mode("squash")

    def test_rebased_pr_target_history(self):
        self.check_mode("rebase")

    def test_fast_forward_pr_target_history(self):
        self.check_mode("fast-forward")

    def test_pr_commit_api_truncation_is_explicit(self):
        info, first, head = self.fixture("merge")
        info["commits"] = 300
        with patch("huicr.github.gh", side_effect=[json.dumps(info), first + "\n" + head + "\n"]):
            with self.assertRaisesRegex(HuicrError, "truncated"):
                load_pr(self.repo, info["html_url"])

    def authenticated_fixture(self, mode, host="github.com"):
        info, first, head = self.fixture(mode)
        info["merged"] = mode == "merge"
        info["html_url"] = f"https://{host}/example/demo/pull/7"
        # Like GitHub, permit fetching a reachable merge revision by its SHA.
        self.git("config", "uploadpack.allowReachableSHA1InWant", "true",
                 cwd=Path(self.temp.name) / "remote.git")
        # Start without PR objects so both the head and merge/base need fetching.
        root = (Path(self.temp.name) / "review").resolve()
        root.mkdir()
        self.git("init", "-b", "main", cwd=root)
        repo = Repo(root)
        tools = Path(self.temp.name) / "tools"
        tools.mkdir()

        def executable(name, source):
            path = tools / name
            path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(source))
            path.chmod(0o755)
            return path

        executable("gh", """
            import os
            import sys

            assert os.environ.get("GH_PROMPT_DISABLED") == "1"
            args = sys.argv[1:]
            if args[:2] == ["auth", "git-credential"]:
                assert args[2] == "get"
                fields = dict(line.split("=", 1) for line in sys.stdin.read().splitlines() if line)
                assert fields["protocol"] == "https"
                assert fields["host"] == os.environ["HUICR_TEST_HOST"]
                if not os.environ.get("HUICR_TEST_NO_AUTH"):
                    print("username=reviewer\\npassword=test-credential")
            else:
                assert sys.stdin.read() == ""
                if args[:2] == ["repo", "view"]:
                    assert os.getcwd() == os.environ["HUICR_TEST_ROOT"]
                    print(os.environ["HUICR_TEST_REPO"])
                else:
                    assert args[:3] == ["api", "--hostname", os.environ["HUICR_TEST_HOST"]]
                    print(os.environ["HUICR_TEST_COMMITS" if "--paginate" in args else "HUICR_TEST_PR"])
        """)
        # A local Git transport exercises the real credential protocol without
        # network access, TLS certificates, or a user's gh installation/login.
        transport = executable("authenticated-remote", """
            import os
            from pathlib import Path
            import subprocess
            import sys

            request = f"protocol=https\\nhost={os.environ['HUICR_TEST_HOST']}\\n\\n"
            credentials = subprocess.run(["git", "credential", "fill"], input=request,
                                         text=True, capture_output=True, timeout=5)
            if credentials.returncode:
                sys.stderr.write(credentials.stderr)
                sys.exit(1)
            fields = dict(line.split("=", 1) for line in credentials.stdout.splitlines() if line)
            assert fields["username"] == "reviewer", "stale credential helper was used"
            assert fields["password"] == "test-credential"
            with Path(os.environ["HUICR_TEST_FETCH_LOG"]).open("a") as log:
                log.write("authenticated fetch\\n")
            os.execvp("git", ["git", "upload-pack", os.environ["HUICR_TEST_REMOTE"]])
        """)
        askpass = executable("askpass", """
            import os
            from pathlib import Path

            Path(os.environ["HUICR_TEST_PROMPT_LOG"]).touch()
            print("unexpected prompt")
        """)
        remote = f"https://{host}/example/demo.git"
        self.git("config", f"url.ext::{transport}.insteadOf", remote, cwd=root)
        self.git("config", "protocol.ext.allow", "always", cwd=root)
        self.git("config", f"credential.https://{host}.helper",
                 "!f() { printf 'username=stale\\npassword=stale\\n'; }; f", cwd=root)
        self.git("config", "core.askPass", str(askpass), cwd=root)
        fetch_log = Path(self.temp.name) / "fetches"
        prompt_log = Path(self.temp.name) / "prompts"
        environment = patch.dict(os.environ, {
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "1", "GH_PROMPT_DISABLED": "0",
            "HUICR_TEST_HOST": host, "HUICR_TEST_ROOT": str(root),
            "HUICR_TEST_REPO": json.dumps({"url": f"https://{host}/example/demo", "nameWithOwner": "example/demo"}),
            "HUICR_TEST_PR": json.dumps(info), "HUICR_TEST_COMMITS": first + "\n" + head,
            "HUICR_TEST_REMOTE": str(Path(self.temp.name) / "remote.git"),
            "HUICR_TEST_FETCH_LOG": str(fetch_log), "HUICR_TEST_PROMPT_LOG": str(prompt_log),
            "HUICR_TEST_NO_AUTH": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        return repo, info, fetch_log, prompt_log

    def check_authenticated_pr(self, mode, host, numbered=False):
        repo, info, fetch_log, prompt_log = self.authenticated_fixture(mode, host)
        config = (repo.root / ".git/config").read_bytes()
        left, right, commits, _, _ = load_pr(repo, "7" if numbered else info["html_url"])
        self.assertEqual(left, self.base)
        self.assertEqual(right, info["head"]["sha"])
        self.assertEqual(len(commits), 2)
        self.assertEqual(fetch_log.read_text().splitlines(), ["authenticated fetch"] * 2)
        self.assertFalse(prompt_log.exists())
        self.assertEqual((repo.root / ".git/config").read_bytes(), config)
        self.assertFalse((repo.root / ".git/FETCH_HEAD").exists())
        self.assertEqual(repo.head(), "")

    def test_gh_credentials_used_for_head_and_merge_fetches(self):
        self.check_authenticated_pr("merge", "github.com")

    def test_enterprise_pr_number_uses_gh_credentials_for_head_and_base(self):
        self.check_authenticated_pr("open", "github.example.test", numbered=True)

    def test_missing_fetch_credentials_fail_without_terminal_or_askpass_prompt(self):
        repo, info, fetch_log, prompt_log = self.authenticated_fixture("merge")
        with patch.dict(os.environ, {"HUICR_TEST_NO_AUTH": "1"}):
            with self.assertRaisesRegex(HuicrError, "terminal prompts disabled"):
                load_pr(repo, info["html_url"])
        self.assertFalse(fetch_log.exists())
        self.assertFalse(prompt_log.exists())
