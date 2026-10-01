from contextlib import chdir
import io
import os
from pathlib import Path
from unittest.mock import Mock, patch

from huicr.cli import main, parse_args, review_request
from huicr.config import Config, HuicrError
from huicr.git import Repo
from huicr.review import Review
from huicr.ui import UI
from test_review import RepositoryFixture


class HistoryTest(RepositoryFixture):
    def merged_history(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "feature first\nsecond\nthird\n")
        first = self.commit("feature first")
        self.write("other", "unrelated\n")
        hidden = self.commit("unrelated feature change")
        self.write("file.txt", "feature second\nsecond\nthird\n")
        self.write("other", "also changed\n")
        second = self.commit("feature second")
        self.git("switch", "main")
        self.write("main-only", "main change\n")
        main = self.commit("main change")
        self.git("merge", "--no-ff", "feature", "-m", "merge feature")
        return first, hidden, second, main, self.repo.head()

    def test_full_ancestry_includes_root_and_side_branches_newest_first(self):
        first, hidden, second, main, merge = self.merged_history()
        review = self.review("history")
        ids = [c.oid for c in review.commits]
        self.assertEqual(set(ids), {self.base, first, hidden, second, main, merge})
        self.assertEqual((ids[0], ids[-1]), (merge, self.base))
        for commit in review.commits:
            self.assertEqual(commit.parents, self.repo.commit_info(commit.oid).parents)
            for parent in commit.parents:
                self.assertLess(ids.index(commit.oid), ids.index(parent))
        self.assertEqual(review.view.left_commit, main)
        review.filter_history(True)
        self.assertEqual({c.oid for c in review.commits}, set(ids) - {merge})
        review.commit_index = len(review.commits) - 1
        review.select_view()
        self.assertEqual(review.view.left, self.repo.empty())
        self.assertEqual(review.view.left_commit, "")
        self.assertEqual(review.view.commit, self.base)
        # Branch review retains its distinct first-parent comparison.
        branch = self.review("range", target=f"{self.base}..HEAD")
        self.assertEqual([c.oid for c in branch.commits], [main, merge])

    def test_file_history_filters_commits_and_files_but_keeps_actual_parents(self):
        first, hidden, second, _, _ = self.merged_history()
        self.write("file.txt", "dirty worktree\n")
        index = (self.root / ".git/index").read_bytes()
        review = self.review("history", paths=["file.txt"], no_merges=True)
        self.assertEqual([c.oid for c in review.commits], [second, first, self.base])
        self.assertEqual(review.view.left_commit, hidden)
        self.assertEqual(review.commits[0].parents, [hidden])
        self.assertEqual([f.path for f in review.view.files], ["file.txt"])
        self.assertEqual(self.repo.blob(review.view.left, "file.txt"), b"feature first\nsecond\nthird\n")
        self.assertEqual(self.repo.blob(review.view.right, "file.txt"), b"feature second\nsecond\nthird\n")
        comment = self.add_comment(review)
        anchor = self.store.comment(comment)["anchor"]
        self.assertEqual((anchor["scope"], anchor["commit"], anchor["left_commit"]), ("history", second, hidden))
        review.whole = True
        review.select_view()
        self.assertEqual(review.view.left, self.repo.empty())
        self.assertEqual(review.view.left_commit, "")
        self.assertEqual([f.path for f in review.view.files], ["file.txt"])
        self.assertEqual(index, (self.root / ".git/index").read_bytes())
        self.assertEqual((self.root / "file.txt").read_text(), "dirty worktree\n")

    def test_file_history_keeps_side_work_discarded_by_a_treesame_merge(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "discarded change\n")
        feature = self.commit("discarded feature")
        self.git("switch", "main")
        self.git("merge", "--no-ff", "-s", "ours", "feature", "-m", "discard feature")
        merge = self.repo.head()
        review = self.review("history", paths=["file.txt"])
        for no_merges in (False, True, False):
            with self.subTest(no_merges=no_merges):
                review.filter_history(no_merges)
                self.assertEqual([c.oid for c in review.commits], [feature, self.base])
                self.assertEqual(review.view.commit, feature)
                self.assertEqual([f.path for f in review.view.files], ["file.txt"])
        self.assertEqual(self.review("history").commits[0].oid, merge)
        empty = self.review("history", target=f"{feature}..{merge}", paths=["file.txt"])
        self.assertEqual(empty.commits, [])
        self.assertEqual(empty.view.files, [])
        self.assertIn("No commits match", empty.view.label)

    def test_merge_path_filter_uses_first_parent_diff_for_each_requested_path(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "discarded change\n")
        self.write("src/other.txt", "retained change\n")
        feature = self.commit("feature changes")
        self.git("switch", "main")
        self.git("merge", "--no-ff", "--no-commit", "feature")
        self.git("restore", "--source=HEAD", "--staged", "--worktree", "--", "file.txt")
        merge = self.commit("retain only other path")
        for paths, expected in ((["file.txt"], [feature, self.base]),
                                (["src/"], [merge, feature]),
                                (["file.txt", "src/"], [merge, feature, self.base])):
            with self.subTest(paths=paths):
                review = self.review("history", paths=paths)
                self.assertEqual([c.oid for c in review.commits], expected)
                self.assertEqual(review.view.left_commit, self.base)
                if merge in expected:
                    self.assertEqual(review.commits[0].parents, [self.base, feature])
                    self.assertEqual([f.path for f in review.view.files], ["src/other.txt"])
                for index in range(len(review.commits)):
                    review.commit_index = index
                    self.assertTrue(review.select_view().files)

    def test_paths_include_rename_and_deletion_with_original_anchors(self):
        self.git("mv", "file.txt", "renamed π.txt")
        renamed = self.commit("rename")
        self.git("rm", "renamed π.txt")
        deleted = self.commit("delete")
        old = self.review("history", paths=["file.txt"])
        self.assertEqual([c.oid for c in old.commits], [renamed, self.base])
        file = old.view.files[0]
        self.assertEqual((file.old_path, file.path, file.status), ("file.txt", "renamed π.txt", "R100"))
        new = self.review("history", paths=["renamed π.txt"])
        self.assertEqual([c.oid for c in new.commits], [deleted, renamed])
        self.assertEqual(new.view.files[0].status, "D")
        self.assertEqual(self.repo.blame(new.view, new.view.files[0], "old")[0][0], self.base)

    def test_literal_paths_directories_and_multiple_paths(self):
        self.write("src/a[1] π.txt", "literal\n")
        self.write("src/a1 π.txt", "glob lookalike\n")
        self.write("src-other/file", "different directory\n")
        self.write("-option", "not an option\n")
        tip = self.commit("paths")
        review = self.review("history", paths=["src/a[1] π.txt", "-option"])
        self.assertEqual([c.oid for c in review.commits], [tip])
        self.assertEqual({f.path for f in review.view.files}, {"src/a[1] π.txt", "-option"})
        directory = self.review("history", paths=["src/"])
        self.assertEqual({f.path for f in directory.view.files}, {"src/a[1] π.txt", "src/a1 π.txt"})
        all_paths = self.review("history", paths=["."])
        self.assertEqual([c.oid for c in all_paths.commits], [tip, self.base])

    def test_merge_toggle_stays_frozen_and_refresh_preserves_selected_commit(self):
        _, _, _, _, merge = self.merged_history()
        review = self.review("history", no_merges=True)
        selected = review.view.commit
        self.write("file.txt", "later change\n")
        later = self.commit("later change")
        with patch.object(self.repo, "history", side_effect=AssertionError("toggle must not reload history")):
            review.filter_history(False)
            self.assertIn(merge, [c.oid for c in review.commits])
            self.assertNotIn(later, [c.oid for c in review.commits])
            self.assertEqual(review.view.commit, selected)
            review.filter_history(True)
            self.assertEqual(review.view.commit, selected)
        review.load()
        self.assertEqual(review.commits[0].oid, later)
        self.assertEqual(review.view.commit, selected)

    def test_history_ranges_and_empty_filters(self):
        self.write("file.txt", "next\n")
        tip = self.commit("next")
        review = self.review("history", target=f"{self.base}..{tip}", paths=["file.txt"])
        self.assertEqual([c.oid for c in review.commits], [tip])
        review.whole = True
        review.select_view()
        self.assertEqual(review.view.left_commit, self.base)
        self.git("switch", "-c", "divergent", self.base)
        self.write("another", "divergent\n")
        divergence = self.commit("divergent")
        with self.assertRaisesRegex(HuicrError, "ancestor"):
            self.review("history", target=f"{tip}..{divergence}")
        divergent = self.review("history", target=f"{tip}...{divergence}")
        self.assertEqual([c.oid for c in divergent.commits], [divergence])
        self.assertEqual(divergent.left, self.base)
        for options in ({"paths": ["does-not-exist"]}, {"target": "HEAD..HEAD"}):
            with self.subTest(options=options):
                empty = self.review("history", **options)
                self.assertEqual(empty.commits, [])
                self.assertEqual(empty.view.files, [])
                self.assertIn("No commits match", empty.view.label)

    def test_merge_only_filtered_range_does_not_fall_back_to_aggregate_changes(self):
        self.git("switch", "-c", "feature")
        self.write("feature", "feature\n")
        tip = self.commit("feature")
        self.git("switch", "main")
        self.git("merge", "--no-ff", "feature", "-m", "merge feature")
        review = self.review("history", target=f"{tip}..HEAD", no_merges=True)
        self.assertEqual(review.commits, [])
        self.assertEqual(review.view.files, [])
        review.filter_history(False)
        self.assertEqual(len(review.commits), 1)
        self.assertEqual(review.view.commit, self.repo.head())

    def test_unborn_history_is_empty(self):
        unborn = self.root / "unborn"
        unborn.mkdir()
        self.git("init", "-b", "main", cwd=unborn)
        review = Review(Repo(unborn), self.store, "history", no_merges=True)
        review.load()
        self.assertEqual(review.commits, [])
        self.assertEqual(review.view.files, [])


class HistoryCLITest(RepositoryFixture):
    def request(self, *argv):
        return review_request(parse_args(["review", "--repo", str(self.root), *argv]), self.repo)

    def test_history_flags_and_positional_paths(self):
        for argv in (("--history",), ("--scope", "history")):
            self.assertEqual(self.request(*argv), {"scope": "history"})
        self.assertEqual(self.request("--no-merges"), {"scope": "history", "no_merges": True})
        for argv in (("file.txt",), ("--", "file.txt"), ("--history", "file.txt")):
            self.assertEqual(self.request(*argv), {"scope": "history", "paths": ["file.txt"]})
        self.assertEqual(self.request("--no-merges", "main", "--", "file.txt"),
                         {"scope": "history", "target": "main", "paths": ["file.txt"], "no_merges": True})
        self.assertEqual(self.request("main", "file.txt"),
                         {"scope": "history", "target": "main", "paths": ["file.txt"]})
        self.assertEqual(self.request("--", "missing", "-option"),
                         {"scope": "history", "paths": ["missing", "-option"]})

    def test_existing_ref_and_pr_syntax_and_ambiguous_filenames(self):
        self.git("branch", "file.txt")
        for target, scope in (("main", "branch"), (self.base, "range"), ("main..HEAD", "range"),
                              ("42", "pr"), ("https://github.com/example/repo/pull/42", "pr"), ("file.txt", "branch")):
            self.assertEqual(self.request(target), {"scope": scope, "target": target})
        self.assertEqual(self.request("--", "file.txt"), {"scope": "history", "paths": ["file.txt"]})
        for argv in (("--scope", "branch", "--no-merges"), ("--scope", "pr", "42", "--", "file.txt")):
            with self.assertRaisesRegex(HuicrError, "history scope"):
                self.request(*argv)

    def test_deleted_path_is_recognized_without_separator(self):
        self.git("rm", "file.txt")
        self.commit("delete")
        self.assertEqual(self.request("--no-merges", "file.txt"),
                         {"scope": "history", "paths": ["file.txt"], "no_merges": True})

    def test_paths_relative_to_repo_directory_and_symlink_paths(self):
        self.write("src/file.txt", "nested\n")
        (self.root / "link").symlink_to(Path(self.temp.name))
        self.commit("nested and symlink")
        args = parse_args(["review", "--repo", str(self.root / "src"), "file.txt"])
        self.assertEqual(review_request(args, self.repo), {"scope": "history", "paths": ["src/file.txt"]})
        with chdir(self.root / "src"):
            args = parse_args(["open", "--no-merges", "../file.txt"])
            self.assertEqual(review_request(args, self.repo),
                             {"scope": "history", "paths": ["file.txt"], "no_merges": True})
        self.assertEqual(self.request(str(self.root / "src/file.txt"))["paths"], ["src/file.txt"])
        self.assertEqual(self.request("link")["paths"], ["link"])
        with self.assertRaisesRegex(HuicrError, "outside this repository"):
            self.request("--", "../outside")

    def test_open_transports_history_options(self):
        with chdir(self.root), patch("sys.argv", ["huicr", "open", "--no-merges", "file.txt"]), \
             patch.dict(os.environ, {"HUICR_STATE_DIR": str(self.store.directory)}), \
             patch("huicr.cli.Config.load", return_value=Config()), patch("huicr.cli.action", return_value={}) as action, \
             patch("sys.stdout", new_callable=io.StringIO):
            main()
        self.assertEqual(action.call_args.args[3], {"scope": "history", "paths": ["file.txt"], "no_merges": True})
        self.assertFalse(action.call_args.kwargs["focus"])


class OpenHistoryTest(RepositoryFixture):
    def make_ui(self, scope="unstaged", **kwargs):
        screen = Mock()
        screen.getmaxyx.return_value = 35, 110
        with patch("huicr.ui.curses.curs_set"), patch("huicr.ui.curses.set_escdelay"), patch("huicr.ui.Theme") as theme:
            theme.return_value.pairs = {}
            ui = UI(screen, self.review(scope, **kwargs), Config())
        ui.load_file()
        return ui

    def test_open_literal_file_and_directory_paths_relative_to_the_reviewed_repo(self):
        path = "src/with spaces π.txt"
        self.write(path, "file history\n")
        tip = self.commit("file history change")
        self.write("other", "unrelated\n")
        self.commit("unrelated change")
        ui = self.make_ui()
        for target in (path, str(self.root / path), "src/"):
            with self.subTest(target=target), chdir(self.temp.name):
                ui.open_target(target)
                self.assertEqual(ui.review.scope, "history")
                self.assertEqual(ui.review.target, "HEAD")
                self.assertEqual([c.oid for c in ui.review.commits], [tip])
                self.assertEqual([f.path for f in ui.view.files], [path])
                self.assertIn("file history", [line.text for line in ui.lines])

    def test_open_deleted_path_and_force_history_for_ambiguous_names(self):
        self.git("rm", "file.txt")
        deleted = self.commit("delete")
        ui = self.make_ui()
        ui.open_target("file.txt")
        self.assertEqual(ui.view.commit, deleted)
        self.assertEqual(ui.file.status, "D")
        self.git("branch", "file.txt")
        with patch.object(ui, "switch") as switch:
            ui.open_target("file.txt")
            switch.assert_called_once_with(scope="branch", target="file.txt")
        ui.open_target("-- file.txt")
        self.assertEqual(ui.review.paths, ["file.txt"])
        self.assertEqual(ui.view.commit, deleted)
        ui.open_target("-- missing file")
        self.assertEqual(ui.review.paths, ["missing file"])
        self.assertEqual(ui.review.commits, [])
        self.assertIn("No commits match", ui.view.label)

    def test_open_another_file_keeps_the_history_merge_filter(self):
        self.git("switch", "-c", "feature")
        self.write("file.txt", "feature\n")
        feature = self.commit("feature change")
        self.git("switch", "main")
        self.git("merge", "--no-ff", "feature", "-m", "merge feature")
        ui = self.make_ui("history", no_merges=True)
        ui.open_target("file.txt")
        self.assertTrue(ui.review.no_merges)
        self.assertEqual([c.oid for c in ui.review.commits], [feature, self.base])
        ui.save_ui()
        saved = self.store.get(str(self.root), "ui")
        self.assertEqual((saved["scope"], saved["paths"], saved["no_merges"]), ("history", ["file.txt"], True))

    def test_open_refs_and_prs_keeps_their_original_scopes(self):
        ui = self.make_ui("history", no_merges=True)
        for target, scope in (("main", "branch"), (self.base, "range"), ("main..HEAD", "range"),
                              ("42", "pr"), ("https://github.com/example/repo/pull/42", "pr")):
            with self.subTest(target=target), patch.object(ui, "switch") as switch:
                ui.open_target(target)
                switch.assert_called_once_with(scope=scope, target=target)

    def test_bad_open_requests_leave_the_current_review_intact(self):
        ui = self.make_ui("history")
        previous = ui.review
        for target in ("--", "-- ../outside", "missing-revision"):
            with self.subTest(target=target), self.assertRaises(HuicrError):
                ui.open_target(target)
            self.assertIs(ui.review, previous)
            self.assertEqual(ui.view.commit, self.base)
