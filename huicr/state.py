"""Durable comments and an explicit delivery outbox, shared by UI/CLI/event hooks."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .config import HuicrError, state_dir


def now():
    return time.time()


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else state_dir()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "reviews.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise HuicrError(f"Unsupported state schema {version}; upgrade huicr")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                repo TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY (repo, key)
            );
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, anchor TEXT NOT NULL,
                body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                sent_version INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS comments_repo ON comments(repo);
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, target TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                entries TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS deliveries_repo ON deliveries(repo);
            CREATE TABLE IF NOT EXISTS github_deliveries (
                id TEXT PRIMARY KEY, repo TEXT NOT NULL, pr TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, entries TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS github_deliveries_repo ON github_deliveries(repo, pr);
        """)
        if version < 2:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                # Recheck under the write lock: two panes can migrate together.
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(comments)")}
                if "github_version" not in columns:
                    self.db.execute("ALTER TABLE comments ADD COLUMN github_version INTEGER NOT NULL DEFAULT 0")
                    self.db.execute("ALTER TABLE comments ADD COLUMN github_url TEXT NOT NULL DEFAULT ''")
                self.db.execute("PRAGMA user_version=2")

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self, key):
        name = hashlib.sha256(key.encode()).hexdigest()[:32]
        with (self.directory / f"{name}.lock").open("a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def get(self, repo, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE repo=? AND key=?", (repo, key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, repo, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (repo, key, json.dumps(value)))

    @staticmethod
    def decode_comment(row):
        comment = dict(row)
        comment["anchor"] = json.loads(comment["anchor"])
        return comment

    def comments(self, repo, pending=False, include_resolved=True):
        sql = "SELECT * FROM comments WHERE repo=? AND deleted=0"
        if pending:
            sql += " AND version>sent_version AND resolved=0"
        elif not include_resolved:
            sql += " AND resolved=0"
        return [self.decode_comment(row) for row in self.db.execute(sql + " ORDER BY created", (repo,))]

    def comment(self, comment_id):
        row = self.db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not row:
            raise HuicrError("Comment no longer exists")
        return self.decode_comment(row)

    def add(self, repo, anchor, body):
        if not body.strip():
            raise HuicrError("Comment cannot be empty")
        comment_id = uid()
        with self.db:
            self.db.execute("""INSERT INTO comments
                (id,repo,anchor,body,created,updated) VALUES (?,?,?,?,?,?)""",
                (comment_id, repo, json.dumps(anchor), body.strip(), now(), now()))
        return comment_id

    def edit(self, comment_id, body):
        if not body.strip():
            raise HuicrError("Comment cannot be empty")
        with self.db:
            self.db.execute("""UPDATE comments SET body=?, version=version+1, updated=?
                WHERE id=? AND body!=?""", (body.strip(), now(), comment_id, body.strip()))

    def resolve(self, comment_id, resolved=True):
        with self.db:
            self.db.execute("UPDATE comments SET resolved=?, updated=? WHERE id=?", (resolved, now(), comment_id))

    def delete(self, comment_id):
        with self.db:
            self.db.execute("UPDATE comments SET deleted=1, updated=? WHERE id=?", (now(), comment_id))

    def deliveries(self, repo, unresolved=False):
        sql = "SELECT * FROM deliveries WHERE repo=?"
        if unresolved:
            sql += " AND status IN ('sending','uncertain')"
        rows = self.db.execute(sql + " ORDER BY created", (repo,))
        return [dict(row) for row in rows]

    def prepare(self, repo, target, mode, ids=None):
        # Caller holds a repo delivery lock across transport. Crash survivors are
        # never resent blindly: only an explicit reconciliation releases them.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.deliveries(repo, unresolved=True):
                raise HuicrError("A previous send has an uncertain outcome. Press X to reconcile it before sending again.")
            comments = self.comments(repo, pending=True)
            if ids is not None:
                comments = [c for c in comments if c["id"] in ids]
            if not comments:
                raise HuicrError("No unsent comments")
            payload = format_comments(comments, repo)
            delivery_id = uid()
            entries = [(c["id"], c["version"]) for c in comments]
            self.db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)", (
                delivery_id, repo, json.dumps(target), mode, "sending", payload,
                json.dumps(entries), "", now(), now()))
        return delivery_id, payload

    def finish(self, delivery_id, status, error=""):
        if status not in ("sent", "uncertain", "failed"):
            raise ValueError(status)
        with self.db:
            delivery = self.db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not delivery:
                raise HuicrError("Unknown delivery")
            if status == "sent":
                for comment_id, version in json.loads(delivery["entries"]):
                    # An edit made while sending remains pending.
                    self.db.execute("UPDATE comments SET sent_version=MAX(sent_version,?) WHERE id=?", (version, comment_id))
            self.db.execute("UPDATE deliveries SET status=?,error=?,updated=? WHERE id=?", (status, error, now(), delivery_id))

    def github_deliveries(self, repo, pr=None, unresolved=False):
        sql = "SELECT * FROM github_deliveries WHERE repo=?"
        args = [repo]
        if pr is not None:
            sql += " AND pr=?"
            args.append(pr)
        if unresolved:
            sql += " AND status IN ('sending','uncertain')"
        deliveries = []
        for row in self.db.execute(sql + " ORDER BY created", args):
            delivery = dict(row)
            for key in ("payload", "entries", "result"):
                delivery[key] = json.loads(delivery[key])
            deliveries.append(delivery)
        return deliveries

    def prepare_github(self, repo, pr, comments, payload):
        # The caller holds the PR's publication lock across the network request.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.github_deliveries(repo, pr, unresolved=True):
                raise HuicrError("A GitHub publication has an uncertain outcome. Press P or X to reconcile it.")
            delivery_id = uid()
            payload = {**payload, "body": payload["body"] + f"\n\n<!-- huicr-review:{delivery_id} -->"}
            entries = [(c["id"], c["version"]) for c in comments]
            self.db.execute("INSERT INTO github_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)", (
                delivery_id, repo, pr, "sending", json.dumps(payload), json.dumps(entries), "{}", "", now(), now()))
        return self.github_deliveries(repo, pr, unresolved=True)[0]

    def finish_github(self, delivery_id, status, result=None, error=""):
        if status not in ("sent", "uncertain", "failed"):
            raise ValueError(status)
        with self.db:
            row = self.db.execute("SELECT * FROM github_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise HuicrError("Unknown GitHub publication")
            result = result or {}
            if status == "sent":
                for comment_id, version in json.loads(row["entries"]):
                    self.db.execute("""UPDATE comments SET github_version=?,github_url=?
                        WHERE id=? AND github_version<=?""", (version, result["html_url"], comment_id, version))
            self.db.execute("UPDATE github_deliveries SET status=?,result=?,error=?,updated=? WHERE id=?",
                            (status, json.dumps(result), error, now(), delivery_id))


def format_comments(comments, repo):
    lines = [f"# huicr review — {repo}", "", "Please address these review comments. The review is still in progress.",
             "Locations refer to the captured revisions below, not necessarily the current worktree.", ""]
    for c in comments:
        a = c["anchor"]
        location = a["path"]
        if a.get("start") is not None:
            location += f":{a['start']}"
            if a.get("end") != a["start"]:
                location += f"-{a['end']}"
        lines += [f"## `{location}` ({a.get('side', 'file')} side)",
                  f"Comment `{c['id']}` · revision {c['version']}",
                  f"Scope: {a['label']}",
                  f"Commit: {a.get('commit') or 'uncommitted/range'}",
                  f"Trees: {a['left']} → {a['right']}"]
        if a.get("source"):
            lines.append(f"Source: {a['source']}")
        if a.get("snippet"):
            # Choose a fence longer than any in the reviewed code.
            fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", a["snippet"])), default=0) + 1)
            lines += ["", fence + "diff", a["snippet"], fence]
        lines += ["", c["body"], ""]
    return "\n".join(lines)
