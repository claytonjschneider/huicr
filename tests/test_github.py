import json
from pathlib import Path
from unittest.mock import patch

from huicr.config import HuicrError
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
            left, right, commits, source = load_pr(self.repo, info["html_url"])
        self.assertEqual(left, self.base)
        self.assertEqual(right, head)
        self.assertEqual([c.oid for c in commits], [first, head])
        self.assertIn("target main", source)
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
