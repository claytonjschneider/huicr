import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from huicr.config import Config, HuicrError
from huicr.git import Repo, parse_patch
from huicr.herdr import action, capture_turn, event, identity, send
from huicr.review import Review
from huicr.state import Store, format_comments


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        isolation = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolation.start()
        self.addCleanup(isolation.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="huicr-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "repo").resolve()
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Reviewer")
        self.git("config", "user.email", "reviewer@example.test")
        self.write("file.txt", "first\nsecond\nthird\n")
        self.commit("initial")
        self.base = self.git("rev-parse", "HEAD")
        self.repo = Repo(self.root)
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.root), *args], stderr=subprocess.PIPE).decode().strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def review(self, scope="unstaged", **kwargs):
        review = Review(self.repo, self.store, scope, **kwargs)
        review.load()
        return review

    def add_comment(self, review=None, body="Please handle this case"):
        review = review or self.review("range", target="HEAD")
        file = review.view.files[0]
        lines = self.repo.diff(review.view, file)
        index = next(i for i, line in enumerate(lines) if line.new is not None)
        return self.store.add(str(self.root), review.anchor(file, lines, index), body)


class RepositoryTest(RepositoryFixture):
    def test_unstaged_staged_and_untracked_preserve_real_index(self):
        self.write("file.txt", "staged\nsecond\nthird\n")
        self.git("add", "file.txt")
        self.write("file.txt", "unstaged\nsecond\nthird\n")
        self.write("untracked name\tπ.txt", "new\n")
        self.write(".gitignore", "ignored\n")
        self.write("ignored", "do not include\n")
        index_path = self.root / ".git/index"
        index_before = index_path.read_bytes()
        review = self.review()
        self.assertEqual(index_before, index_path.read_bytes())
        self.assertEqual(self.repo.blob(review.view.left, "file.txt"), b"staged\nsecond\nthird\n")
        self.assertEqual(self.repo.blob(review.view.right, "file.txt"), b"unstaged\nsecond\nthird\n")
        names = {f.path for f in review.view.files}
        self.assertIn("untracked name\tπ.txt", names)
        self.assertNotIn("ignored", names)
        staged = self.review("staged")
        self.assertEqual([f.path for f in staged.view.files], ["file.txt"])
        self.assertEqual(index_before, index_path.read_bytes())

    def test_staged_ignored_addition_and_split_index(self):
        self.write(".gitignore", "ignored\n")
        self.write("ignored", "added deliberately\n")
        self.git("add", "-f", "ignored")
        self.git("update-index", "--split-index")
        index_before = (self.root / ".git/index").read_bytes()
        self.write("ignored", "changed unstaged\n")
        review = self.review()
        self.assertIn("ignored", [f.path for f in review.view.files])
        self.assertEqual(index_before, (self.root / ".git/index").read_bytes())

    def test_commit_parent_views_ignore_dirty_tree_and_advancing_base(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "first change\nsecond\nthird\n")
        first = self.commit("first change")
        self.write("file.txt", "second change\nsecond\nthird\n")
        second = self.commit("second change")
        self.git("switch", "main")
        self.write("main-only", "later target change\n")
        self.commit("advance target")
        self.git("switch", "feature")
        self.write("file.txt", "dirty agent edits\n")
        review = self.review("branch", base="main")
        self.assertEqual([c.oid for c in review.commits], [first, second])
        self.assertEqual(review.view.left, self.repo.resolve(self.base, "tree"))
        self.assertEqual(review.view.right, self.repo.resolve(first, "tree"))
        review.commit_index = 1
        review.select_view()
        self.assertEqual(review.view.left, self.repo.resolve(first, "tree"))
        self.assertNotIn("dirty", self.repo.blob(review.view.right, "file.txt").decode())
        review.whole = True
        review.select_view()
        self.assertEqual(review.view.left, self.repo.resolve(self.base, "tree"))
        self.assertNotIn("main-only", [f.path for f in review.view.files])
        self.assertEqual((self.root / "file.txt").read_text(), "dirty agent edits\n")

    def test_explicit_ranges_and_root_commit(self):
        self.write("file.txt", "new\n")
        tip = self.commit("next")
        review = self.review("range", target=f"{self.base}..{tip}")
        self.assertEqual(len(review.commits), 1)
        self.assertEqual(review.view.commit, tip)
        root = self.review("range", target=self.base)
        self.assertEqual(root.view.left, self.repo.empty())
        self.git("switch", "-c", "divergent", self.base)
        self.write("another", "divergent\n")
        divergence = self.commit("divergent")
        with self.assertRaisesRegex(HuicrError, "ancestor"):
            self.review("range", target=f"{tip}..{divergence}")
        review = self.review("range", target=f"{tip}...{divergence}")
        self.assertEqual(review.left, self.base)

    def test_merge_diff_uses_first_parent(self):
        self.git("switch", "-c", "feature")
        self.write("feature", "feature\n")
        first = self.commit("feature")
        self.git("switch", "main")
        self.write("target", "target\n")
        self.commit("target")
        self.git("switch", "feature")
        self.git("merge", "--no-ff", "main", "-m", "merge target")
        review = self.review("range", target="HEAD")
        self.assertEqual(review.view.left_commit, first)
        self.assertEqual([f.path for f in review.view.files], ["target"])

    def test_named_merged_branch_recovers_historic_comparator(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "feature\nsecond\nthird\n")
        head = self.commit("feature")
        self.git("switch", "main")
        self.write("main-only", "target advance\n")
        self.commit("target advance")
        self.git("merge", "--no-ff", "feature", "-m", "integrate feature")
        self.write("main-only", "later advance\n")
        self.commit("later target")
        review = self.review("branch", base="main", target="feature")
        self.assertEqual(review.left, self.base)
        self.assertEqual([c.oid for c in review.commits], [head])
        self.assertEqual([f.path for f in review.view.files], ["file.txt"])

    def test_rename_delete_binary_and_old_side_blame(self):
        self.git("mv", "file.txt", "renamed π.txt")
        renamed = self.commit("rename")
        review = self.review("range", target=renamed)
        self.assertEqual(review.view.files[0].old_path, "file.txt")
        self.assertEqual(review.view.files[0].path, "renamed π.txt")
        blame = self.repo.blame(review.view, review.view.files[0])
        self.assertEqual(blame[0][0], self.base)
        self.git("rm", "renamed π.txt")
        self.commit("delete")
        review = self.review("range", target="HEAD")
        lines = self.repo.diff(review.view, review.view.files[0])
        deleted = next(i for i, line in enumerate(lines) if line.kind == "delete")
        anchor = review.anchor(review.view.files[0], lines, deleted)
        self.assertEqual(anchor["side"], "old")
        self.assertEqual(anchor["start"], 1)
        self.assertEqual(self.repo.blame(review.view, review.view.files[0], "old")[0][0], self.base)
        (self.root / "binary").write_bytes(b"hello\x00there")
        binary = self.review()
        patch_lines = self.repo.diff(binary.view, binary.view.files[0])
        self.assertTrue(any("Binary files" in line.text for line in patch_lines))

    def test_uncommitted_blame_uses_snapshot_not_later_edits(self):
        self.write("file.txt", "first\nchanged\nthird\n")
        review = self.review()
        self.write("file.txt", "totally different\n")
        blame = self.repo.blame(review.view, review.view.files[0])
        self.assertEqual(blame[0][0], self.base)
        self.assertEqual(blame[1][0], "uncommitted")
        self.assertEqual(blame[1][2], "changed")

    def test_turn_excludes_preexisting_dirt_and_freezes_end(self):
        self.write("file.txt", "user edit\nsecond\nthird\n")
        before_index = (self.root / ".git/index").read_bytes()
        capture_turn(self.repo, self.store)
        self.write("file.txt", "user edit\nagent change\nthird\n")
        capture_turn(self.repo, self.store, phase="end")
        self.write("file.txt", "later edit\n")
        review = self.review("turn")
        self.assertEqual(self.repo.blob(review.view.left, "file.txt"), b"user edit\nsecond\nthird\n")
        self.assertEqual(self.repo.blob(review.view.right, "file.txt"), b"user edit\nagent change\nthird\n")
        self.assertEqual(before_index, (self.root / ".git/index").read_bytes())
        first = capture_turn(self.repo, self.store, key="agent", automatic=True)
        self.write("file.txt", "resume after permission\n")
        resumed = capture_turn(self.repo, self.store, key="agent", automatic=True)
        self.assertEqual(first["id"], resumed["id"])

    def test_comments_survive_reopen_and_anchor_after_edits(self):
        self.write("file.txt", "captured\nsecond\nthird\n")
        review = self.review()
        cid = self.add_comment(review)
        other = Store(self.store.directory)
        self.addCleanup(other.close)
        self.write("file.txt", "later content\n")
        comment = other.comment(cid)
        review.restore_anchor(comment["anchor"])
        self.assertEqual(self.repo.blob(review.view.right, "file.txt"), b"captured\nsecond\nthird\n")
        self.git("gc", "--prune=now")
        self.assertEqual(self.repo.blob(review.view.right, "file.txt"), b"captured\nsecond\nthird\n")
        text = format_comments([comment], str(self.root))
        self.assertIn(comment["anchor"]["right"], text)
        self.assertIn(cid, text)

    def test_delivery_versions_failure_and_crash_reconciliation(self):
        cid = self.add_comment()
        target = {"pane_id": "w1:p1"}
        delivery, payload = self.store.prepare(str(self.root), target, "paste")
        self.assertIn("Please handle", payload)
        with self.assertRaisesRegex(HuicrError, "uncertain"):
            self.store.prepare(str(self.root), target, "paste")
        self.store.edit(cid, "Updated while sending")
        self.store.finish(delivery, "sent")
        comment = self.store.comment(cid)
        self.assertEqual((comment["version"], comment["sent_version"]), (2, 1))
        second, _ = self.store.prepare(str(self.root), target, "submit")
        self.store.finish(second, "uncertain", "connection lost")
        self.assertEqual(len(self.store.comments(str(self.root), pending=True)), 1)
        self.store.finish(second, "failed", "user confirmed not delivered")
        third, _ = self.store.prepare(str(self.root), target, "submit")
        self.store.finish(third, "sent")
        self.assertEqual(self.store.comments(str(self.root), pending=True), [])
        with self.assertRaisesRegex(HuicrError, "No unsent"):
            self.store.prepare(str(self.root), target, "submit")

    def test_single_comment_send_retains_other_drafts(self):
        first = self.add_comment(body="First")
        second = self.add_comment(body="Second")
        delivery, _ = self.store.prepare(str(self.root), {}, "paste", [first])
        self.store.finish(delivery, "sent")
        self.assertEqual([c["id"] for c in self.store.comments(str(self.root), pending=True)], [second])

    def test_worktrees_have_separate_state_and_real_indices(self):
        worktree = Path(self.temp.name) / "linked"
        self.git("worktree", "add", "-b", "other", str(worktree))
        (worktree / "file.txt").write_text("other worktree\n")
        repo = Repo(worktree)
        index_path = Path(repo.text("rev-parse", "--path-format=absolute", "--git-path", "index"))
        before = index_path.read_bytes()
        repo.snapshot()
        self.assertEqual(before, index_path.read_bytes())
        self.add_comment()
        self.assertEqual(self.store.comments(str(worktree)), [])

    def test_new_repo_without_commits(self):
        unborn = Path(self.temp.name) / "unborn"
        unborn.mkdir()
        self.git("init", "-b", "main", cwd=unborn)
        (unborn / "new").write_text("new\n")
        repo = Repo(unborn)
        review = Review(repo, self.store)
        review.load()
        self.assertEqual([f.path for f in review.view.files], ["new"])
        self.assertEqual(review.view.left, repo.empty())

    def test_herdr_delivery_checks_origin_and_retains_errors(self):
        pane = {"pane_id": "w1:p1", "terminal_id": "original", "agent": "opencode",
                "agent_status": "idle", "agent_session": {"value": "session"}, "foreground_cwd": str(self.root)}
        origin = identity(pane, self.repo)
        self.add_comment()
        with patch("huicr.herdr.call", return_value={"agent": {**pane, "terminal_id": "replacement"}}):
            with self.assertRaisesRegex(HuicrError, "changed"):
                send(self.store, str(self.root), origin)
        self.assertEqual(self.store.deliveries(str(self.root)), [])
        with patch("huicr.herdr.call", side_effect=[{"agent": pane}, HuicrError("timeout")]):
            with self.assertRaisesRegex(HuicrError, "uncertain"):
                send(self.store, str(self.root), origin)
        self.assertEqual(len(self.store.comments(str(self.root), pending=True)), 1)
        self.assertEqual(self.store.deliveries(str(self.root))[0]["status"], "uncertain")

    def test_tab_action_preserves_exact_origin_and_toggle_ownership(self):
        pane = {"pane_id": "w1:p1", "terminal_id": "agent-terminal", "agent": "opencode",
                "workspace_id": "w1", "foreground_cwd": str(self.root)}
        opened = {"pane_id": "w1:p5", "terminal_id": "review-terminal", "tab_id": "w1:t2"}
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_PLUGIN_CONTEXT_JSON": "{}"}), \
             patch("huicr.herdr.call", side_effect=[{"pane": pane}, {"panes": [pane]},
                  {"plugin_pane": {"pane": opened}}, {}]) as api:
            action("open", self.store, Config(placement="tab"), focus=False)
            open_args = api.call_args_list[2].args
            self.assertIn("tab", open_args)
            self.assertIn("--no-focus", open_args)
            self.assertTrue(any("HUICR_ORIGIN=" in arg and "w1:p1" in arg for arg in open_args))
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1", "HERDR_PLUGIN_CONTEXT_JSON": "{}"}), \
             patch("huicr.herdr.call", side_effect=[{"pane": pane}, {"panes": [pane, opened]}, {}]) as api:
            action("toggle", self.store, Config(placement="tab"))
            self.assertEqual(api.call_args_list[-1].args, ("plugin", "pane", "close", "w1:p5"))

    def test_event_tracking_ignores_stale_hooks(self):
        pane = {"pane_id": "w1:p1", "terminal_id": "terminal", "agent": "opencode",
                "agent_status": "working", "foreground_cwd": str(self.root)}
        origin = identity(pane, self.repo)
        payload = {"data": {"pane_id": "w1:p1", "agent": "opencode", "agent_status": "working"}}
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(payload)}), \
             patch("huicr.herdr.call", return_value={"pane": pane}):
            event(self.store, Config())
        turn = self.store.get(str(self.root), f"turn:{origin['key']}")
        self.assertTrue(turn["active"])
        self.write("file.txt", "agent edits\n")
        payload["data"]["agent_status"] = "idle"
        with patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": json.dumps(payload)}), \
             patch("huicr.herdr.call", return_value={"pane": {**pane, "agent_status": "idle"}}):
            event(self.store, Config())
        self.assertFalse(self.store.get(str(self.root), f"turn:{origin['key']}")["active"])


class PatchTest(unittest.TestCase):
    def test_hunk_coordinates_do_not_treat_code_as_headers(self):
        lines = parse_patch("--- a/file\n+++ b/file\n@@ -10,2 +20,2 @@\n---source\n+++source\n same\n\\ No newline at end of file\n")
        self.assertEqual((lines[1].text, lines[1].old), ("--source", 10))
        self.assertEqual((lines[2].text, lines[2].new), ("++source", 20))
        self.assertEqual((lines[3].old, lines[3].new), (11, 21))


if __name__ == "__main__":
    unittest.main()
