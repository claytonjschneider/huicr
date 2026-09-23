import copy
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock, patch

from huicr.config import HuicrError
from huicr.github import GitHubError, api, publish, reconcile_publication
from huicr.review import Review
from huicr.state import Store
from huicr.ui import UI, comment_status
from test_review import RepositoryFixture


class PublicationTest(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.write("file.txt", "updated first\nupdated second\nthird\n")
        self.head = self.commit("PR changes")
        self.url = "https://github.com/example/demo/pull/7"
        self.endpoint = "repos/example/demo/pulls/7"
        self.info = {"state": "open", "merged": False, "html_url": self.url,
                     "head": {"sha": self.head}, "base": {"sha": self.base}}
        self.current = Review(self.repo, self.store, "pr", target=self.url)
        self.current.left, self.current.right = self.base, self.head
        self.current.source = self.url
        self.current.pr = {"url": self.url, "head": self.head, "base": self.base}
        self.current.commits = self.repo.commits(self.base, self.head)
        self.current.whole = True
        self.current.select_view()
        self.files = [{"filename": "file.txt", "patch": self.repo.text("diff", self.base, self.head)}]
        self.remote = []
        self.requests = []
        self.before_post = None
        self.after_post = None
        self.client = patch("huicr.github.api", side_effect=self.request)
        self.client.start()
        self.addCleanup(self.client.stop)

    def request(self, host, endpoint, *, data=None, paginate=False):
        self.assertEqual(host, "github.com")
        self.requests.append((endpoint, copy.deepcopy(data)))
        if data is not None:
            self.assertEqual(endpoint, f"{self.endpoint}/reviews")
            if self.before_post:
                self.before_post()
            review = {"id": len(self.remote) + 1, "state": "COMMENTED", "body": data["body"],
                      "commit_id": data["commit_id"], "html_url": f"{self.url}#pullrequestreview-{len(self.remote) + 1}"}
            self.remote.append(review)
            if self.after_post:
                self.after_post()
            return review
        if endpoint == self.endpoint:
            return self.info
        if endpoint == f"{self.endpoint}/files?per_page=100":
            self.assertTrue(paginate)
            return self.files
        if endpoint == f"{self.endpoint}/reviews?per_page=100":
            self.assertTrue(paginate)
            return self.remote
        self.fail(f"Unexpected API endpoint: {endpoint}")

    def comment(self, body="Please revise this", side="new", start=1, end=None):
        file = self.current.view.files[0]
        lines = self.repo.diff(self.current.view, file)
        index = next((i for i, line in enumerate(lines) if getattr(line, side) == start), None) if side != "file" else None
        last = next(i for i, line in enumerate(lines) if getattr(line, side) == end) if end else index
        anchor = self.current.anchor(file, lines, index, last, side)
        return self.store.add(str(self.root), anchor, body)

    def post(self, ids=None, **kwargs):
        return publish(self.repo, self.store, self.url, ids, **kwargs)

    def deliveries(self):
        return self.store.github_deliveries(str(self.root))

    def test_posts_inline_ranges_and_file_summary_independently_of_agent_delivery(self):
        right = self.comment("A single new line")
        left = self.comment("An old-side range", side="old", end=2)
        file = self.comment("Whole-file feedback", side="file")
        agent, _ = self.store.prepare(str(self.root), {}, "paste", [right])
        self.store.finish(agent, "sent")
        url = self.post()
        payload = self.requests[-1][1]
        self.assertEqual(payload["commit_id"], self.head)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"], [
            {"path": "file.txt", "body": "A single new line", "side": "RIGHT", "line": 1},
            {"path": "file.txt", "body": "An old-side range", "side": "LEFT", "line": 2,
             "start_line": 1, "start_side": "LEFT"},
        ])
        self.assertIn("### `file.txt`\n\nWhole-file feedback", payload["body"])
        self.assertNotIn(str(self.root), payload["body"])
        for cid in (right, left, file):
            comment = self.store.comment(cid)
            self.assertEqual(comment["github_version"], 1)
            self.assertEqual(comment["github_url"], url)
        self.assertEqual(self.store.comment(right)["sent_version"], 1)
        self.assertEqual({c["id"] for c in self.store.comments(str(self.root), pending=True)}, {left, file})
        self.assertEqual(self.deliveries()[0]["result"]["id"], 1)
        with self.assertRaisesRegex(HuicrError, "No unpublished"):
            self.post()
        self.assertEqual(len(self.remote), 1)

    def test_file_only_review_supports_files_without_a_text_patch(self):
        self.files[0].pop("patch")
        self.comment("Binary or file-wide concern", side="file")
        self.post()
        self.assertNotIn("comments", self.requests[-1][1])
        self.assertIn("Binary or file-wide concern", self.remote[0]["body"])

    def test_selected_comments_and_other_prs_or_scopes_are_isolated(self):
        selected = self.comment("Selected")
        other = self.comment("Leave this draft")
        self.current.pr = {**self.current.pr, "url": self.url.replace("/7", "/8")}
        self.current.select_view()
        foreign = self.comment("Different PR")
        self.current.pr = {**self.current.pr, "url": self.url}
        self.current.whole = False
        self.current.select_view()
        commit = self.comment("Commit-wise feedback")
        self.post([selected])
        self.assertEqual([c["body"] for c in self.requests[-1][1]["comments"]], ["Selected"])
        for cid in (other, foreign, commit):
            self.assertEqual(self.store.comment(cid)["github_version"], 0)
        before = len(self.requests)
        for cid in (foreign, commit, "missing-id"):
            with self.subTest(comment=cid), self.assertRaisesRegex(HuicrError, "whole diff"):
                self.post([cid])
        self.assertEqual(len(self.requests), before)

    def test_stale_head_or_base_is_rejected_before_any_publication(self):
        self.comment()
        for side in ("head", "base"):
            saved = self.info[side]["sha"]
            self.info[side]["sha"] = "f" * 40
            with self.subTest(side=side), self.assertRaisesRegex(HuicrError, "revisions changed"):
                self.post()
            self.info[side]["sha"] = saved
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.remote, [])

    def test_closed_pr_is_rejected(self):
        self.comment()
        self.info["state"] = "closed"
        with self.assertRaisesRegex(HuicrError, "open PR"):
            self.post()
        self.assertEqual(self.deliveries(), [])

    def test_github_context_and_hunk_boundaries_are_validated_atomically(self):
        self.comment("Valid first comment")
        self.comment("Range crosses hunks", end=3)
        self.files[0]["patch"] = "@@ -1 +1 @@\n-first\n+updated first\n@@ -3 +3 @@\n third\n"
        with self.assertRaisesRegex(HuicrError, "outside one GitHub diff hunk"):
            self.post()
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.remote, [])
        self.files[0]["patch"] = ""
        with self.assertRaisesRegex(HuicrError, "outside one GitHub diff hunk"):
            self.post()

    def test_edits_during_publication_stay_pending_and_post_as_a_new_review(self):
        cid = self.comment("Original version")
        self.after_post = lambda: self.store.edit(cid, "Edited during the network request")
        self.post()
        comment = self.store.comment(cid)
        self.assertEqual((comment["version"], comment["github_version"], comment["sent_version"]), (2, 1, 0))
        self.assertIn("GitHub edited", comment_status(comment))
        self.after_post = None
        self.post()
        self.assertEqual(self.store.comment(cid)["github_version"], 2)
        self.assertEqual(len(self.remote), 2)
        self.assertEqual(self.requests[-1][1]["comments"][0]["body"], "Edited during the network request")

    def test_rejected_review_retains_drafts_and_can_be_retried(self):
        cid = self.comment()
        def reject():
            raise GitHubError("Validation Failed (HTTP 422)", 422)
        self.before_post = reject
        with self.assertRaisesRegex(HuicrError, "rejected"):
            self.post()
        self.assertEqual(self.deliveries()[0]["status"], "failed")
        self.assertEqual(self.store.comment(cid)["github_version"], 0)
        self.before_post = None
        self.post()
        self.assertEqual(len(self.remote), 1)

    def test_lost_response_is_recovered_after_reopen_even_if_pr_and_comment_change(self):
        cid = self.comment()
        def disconnect():
            raise GitHubError("Connection lost after GitHub accepted the review")
        self.after_post = disconnect
        with self.assertRaisesRegex(HuicrError, "uncertain"):
            self.post()
        self.assertEqual(self.deliveries()[0]["status"], "uncertain")
        self.assertEqual(self.store.comment(cid)["github_version"], 0)
        self.store.edit(cid, "New draft")
        self.store.delete(cid)
        self.info["state"] = "closed"
        self.info["head"]["sha"] = "f" * 40
        reopened = Store(self.store.directory)
        self.addCleanup(reopened.close)
        url = publish(self.repo, reopened, self.url)
        self.assertEqual(url, self.remote[0]["html_url"])
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(self.deliveries()[0]["status"], "sent")
        self.assertEqual(self.store.comment(cid)["github_version"], 1)
        self.assertEqual(self.store.comment(cid)["version"], 2)
        self.assertEqual(self.requests[-1], (f"{self.endpoint}/reviews?per_page=100", None))

    def test_process_interruption_leaves_durable_sending_record_for_recovery(self):
        self.comment()
        def interrupt():
            raise KeyboardInterrupt()
        self.after_post = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.post()
        self.assertEqual(self.deliveries()[0]["status"], "sending")
        self.post()
        self.assertEqual(self.deliveries()[0]["status"], "sent")
        self.assertEqual(len(self.remote), 1)

    def test_unknown_outcome_requires_reconciliation_and_explicit_retry_when_absent(self):
        cid = self.comment()
        def disconnect():
            raise GitHubError("Connection failed")
        self.before_post = disconnect
        with self.assertRaisesRegex(HuicrError, "uncertain"):
            self.post()
        self.before_post = None
        with self.assertRaisesRegex(HuicrError, "still uncertain"):
            self.post()
        self.assertEqual(self.remote, [])
        self.assertEqual(self.deliveries()[0]["status"], "uncertain")
        # A GitHub failure does not block independent agent delivery.
        agent, _ = self.store.prepare(str(self.root), {}, "paste", [cid])
        self.store.finish(agent, "sent")
        reconcile_publication(self.repo, self.store, self.url, retry=True)
        self.assertEqual(self.deliveries()[0]["status"], "failed")
        self.post()
        self.assertEqual(len(self.remote), 1)

    def test_failed_reconciliation_does_not_authorize_a_duplicate_post(self):
        self.comment()
        def disconnect():
            raise GitHubError("Connection failed")
        self.before_post = disconnect
        with self.assertRaises(HuicrError):
            self.post()
        with patch("huicr.github.api", side_effect=GitHubError("Forbidden (HTTP 403)", 403)):
            with self.assertRaises(HuicrError):
                self.post(retry=True)
        self.assertEqual(self.deliveries()[0]["status"], "uncertain")

    def test_pr_context_survives_restoring_a_saved_whole_diff(self):
        cid = self.comment()
        other = self.review("range", target="HEAD")
        other.restore_anchor(self.store.comment(cid)["anchor"])
        self.assertEqual(other.view.pr, self.current.pr)
        self.assertEqual(other.anchor(other.view.files[0], [], None)["pr"], self.current.pr)

    def test_ui_shortcut_posts_without_an_agent_and_shows_independent_status(self):
        cid = self.comment()
        ui = UI.__new__(UI)
        ui.review, ui.repo, ui.store = self.current, self.repo, self.store
        ui.repo_key, ui.origin, ui.busy = str(self.root), None, Mock()
        ui.handle("P")
        self.assertIn(self.remote[0]["html_url"], ui.message)
        self.assertEqual(comment_status(self.store.comment(cid)), "agent draft · GitHub posted")

    def test_comment_list_posts_only_the_selected_comment(self):
        first = self.comment("Keep local")
        second = self.comment("Publish this one")
        ui = UI.__new__(UI)
        ui.review, ui.repo, ui.store = self.current, self.repo, self.store
        ui.repo_key, ui.origin, ui.busy = str(self.root), None, Mock()
        ui.modal = Mock(side_effect=[("P", 1), (None, 1)])
        ui.running = True
        ui.show_comments()
        self.assertEqual(self.store.comment(first)["github_version"], 0)
        self.assertEqual(self.store.comment(second)["github_version"], 1)

    def test_api_uses_json_stdin_and_pagination_and_classifies_http_errors(self):
        self.client.stop()
        payload = {"body": "Quotes, Unicode π, and\nnewlines", "comments": [{"line": 2}]}
        with patch("huicr.github.gh", return_value='{"id": 7}') as command:
            self.assertEqual(api("github.example.test", self.endpoint, data=payload), {"id": 7})
        self.assertEqual(json.loads(command.call_args.kwargs["input"]), payload)
        self.assertNotIn(payload["body"], command.call_args.args)
        self.assertIn("github.example.test", command.call_args.args)
        with patch("huicr.github.gh", return_value='[[{"id": 1}], [{"id": 2}]]'):
            self.assertEqual(api("github.com", self.endpoint, paginate=True), [{"id": 1}, {"id": 2}])
        with patch("huicr.github.gh", side_effect=HuicrError("gh: Forbidden (HTTP 403)")):
            with self.assertRaises(GitHubError) as failure:
                api("github.com", self.endpoint, data=payload)
        self.assertEqual(failure.exception.status, 403)
        with patch("huicr.github.gh", return_value="incomplete response"):
            with self.assertRaises(GitHubError) as failure:
                api("github.com", self.endpoint, data=payload)
        self.assertIsNone(failure.exception.status)
        with patch("huicr.github.gh", return_value='[{"id": 1}]'):
            with self.assertRaisesRegex(GitHubError, "Invalid"):
                api("github.com", self.endpoint, paginate=True)

    def test_version_one_state_migration_preserves_comments_and_agent_versions(self):
        directory = Path(self.temp.name) / "old-state"
        directory.mkdir()
        database = sqlite3.connect(directory / "reviews.sqlite3")
        database.executescript("""
            CREATE TABLE comments (
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, anchor TEXT NOT NULL,
                body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                sent_version INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            INSERT INTO comments VALUES ('old', 'repo', '{}', 'Saved feedback', 3, 2, 0, 0, 1, 1);
            PRAGMA user_version=1;
        """)
        database.close()
        migrated = Store(directory)
        self.addCleanup(migrated.close)
        comment = migrated.comment("old")
        self.assertEqual((comment["body"], comment["version"], comment["sent_version"]), ("Saved feedback", 3, 2))
        self.assertEqual((comment["github_version"], comment["github_url"]), (0, ""))
        self.assertEqual(migrated.github_deliveries("repo"), [])
