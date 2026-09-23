"""Immutable review views built with Git plumbing; never check out reviewed refs."""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from .config import HuicrError
from .process import run


@dataclass
class Commit:
    oid: str
    parents: list[str]
    subject: str
    author: str
    date: str


@dataclass
class FileChange:
    status: str
    old_path: str
    path: str


@dataclass
class DiffLine:
    text: str
    kind: str = "meta"
    old: int | None = None
    new: int | None = None


@dataclass
class View:
    scope: str
    label: str
    left: str
    right: str
    left_commit: str = ""
    right_commit: str = ""
    commit: str = ""
    source: str = ""
    files: list[FileChange] = field(default_factory=list)
    pr: dict | None = None

    @property
    def key(self):
        return hashlib.sha256(f"{self.left}:{self.right}:{self.commit}".encode()).hexdigest()[:24]


def parse_patch(patch: str) -> list[DiffLine]:
    lines = []
    old = new = None
    for text in patch.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", text)
        if match:
            old, new = map(int, match.groups())
            lines.append(DiffLine(text, "hunk"))
        elif old is not None and text.startswith("+"):
            lines.append(DiffLine(text[1:], "add", new=new))
            new += 1
        elif old is not None and text.startswith("-"):
            lines.append(DiffLine(text[1:], "delete", old=old))
            old += 1
        elif old is not None and text.startswith(" "):
            lines.append(DiffLine(text[1:], "context", old=old, new=new))
            old += 1
            new += 1
        elif text.startswith("\\"):
            lines.append(DiffLine(text))
        elif not text.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            lines.append(DiffLine(text))
    return lines


class Repo:
    def __init__(self, cwd):
        result = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
        self.root = Path(os.fsdecode(result.stdout).strip()).resolve()
        self.id = hashlib.sha256(os.fsencode(self.root)).hexdigest()[:24]

    def git(self, *args, **kwargs):
        env = {"GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1", **kwargs.pop("env", {})}
        return run(["git", "-C", self.root, "-c", "core.quotepath=false", *args], env=env, **kwargs)

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.decode("utf-8", "replace").strip()

    def resolve(self, ref, kind="commit"):
        if not ref or ref.startswith("-") or "\x00" in ref:
            raise HuicrError("Expected a Git revision, not an option")
        return self.text("rev-parse", "--verify", "--end-of-options", f"{ref}^{{{kind}}}")

    def head(self):
        result = self.git("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else ""

    def empty(self):
        return self.text("hash-object", "-t", "tree", "-w", "--stdin", input=b"")

    def pin(self, oid):
        # Trees referenced by comments survive gc, even for never-committed changes.
        self.git("update-ref", f"refs/huicr/snapshots/{oid}", oid)
        return oid

    def snapshot(self, index=False):
        # An alternate index preserves the user's index byte-for-byte, including
        # staged/ignored additions, split-index data and unstaged boundaries.
        with tempfile.TemporaryDirectory(prefix="huicr-index-") as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            index_path = Path(self.text("rev-parse", "--path-format=absolute", "--git-path", "index"))
            if index_path.exists():
                shutil.copyfile(index_path, env["GIT_INDEX_FILE"])
            else:
                self.git("read-tree", "--empty", env=env)
            if not index:
                self.git("add", "-A", "--", ".", env=env, timeout=120)
            return self.pin(self.text("write-tree", env=env))

    def default_base(self):
        for remote in ("upstream", "origin"):
            result = self.git("symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", check=False)
            if result.returncode == 0:
                return result.stdout.decode().strip()
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if self.git("rev-parse", "--verify", f"{candidate}^{{commit}}", check=False).returncode == 0:
                return candidate
        raise HuicrError("No default comparator found. Press B to select a base revision.")

    def refs(self):
        return self.text("for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes", "refs/tags").splitlines()

    def merge_base(self, base, head):
        return self.text("merge-base", self.resolve(base), self.resolve(head))

    def branch_base(self, base, head):
        """Recover the comparator before a branch was merged into it."""
        base, head = self.resolve(base), self.resolve(head)
        fork = self.merge_base(base, head)
        if fork != head or base == head:
            return fork
        history = self.text("rev-list", "--first-parent", "--reverse", base).splitlines()
        # Ancestry is monotonic along the target's first-parent history. Binary
        # search the integration edge instead of scanning every merge in a repo.
        low, high = 0, len(history)
        while low < high:
            middle = (low + high) // 2
            contains = self.git("merge-base", "--is-ancestor", head, history[middle], check=False)
            if contains.returncode == 0:
                high = middle
            elif contains.returncode == 1:
                low = middle + 1
            else:
                raise HuicrError("Could not resolve branch integration history")
        boundary = self.commit_info(history[low])
        if len(boundary.parents) < 2:
            raise HuicrError("This branch was fast-forwarded/rebased into the comparator; Git retained no branch boundary. Open its PR URL or BASE..HEAD range.")
        return self.merge_base(boundary.parents[0], head)

    def commit_info(self, oid):
        oid = self.resolve(oid)
        parts = self.text("show", "-s", "--format=%H%x00%P%x00%s%x00%an%x00%aI", oid).split("\0")
        return Commit(parts[0], parts[1].split(), *parts[2:])

    def commits(self, base, head):
        # First-parent preserves the branch's narrative, including merge commits
        # as changes relative to their first parent, just as gitk's per-commit diff.
        ids = self.text("rev-list", "--reverse", "--first-parent", f"{base}..{head}").splitlines()
        return [self.commit_info(oid) for oid in ids]

    def changes(self, left, right):
        fields = self.git("diff", "--name-status", "-z", "--find-renames", left, right, "--").stdout.split(b"\0")
        files = []
        i = 0
        while i < len(fields) and fields[i]:
            status = fields[i].decode("ascii")
            old = os.fsdecode(fields[i + 1])
            i += 2
            path = old
            if status[0] in "RC":
                path = os.fsdecode(fields[i])
                i += 1
            files.append(FileChange(status, old, path))
        return files

    def view(self, scope, label, left, right, **kwargs):
        view = View(scope, label, self.resolve(left, "tree"), self.resolve(right, "tree"), **kwargs)
        view.files = self.changes(view.left, view.right)
        return view

    def commit_view(self, commit, scope="branch", source=""):
        parent = commit.parents[0] if commit.parents else ""
        return self.view(scope, f"{commit.oid[:10]} {commit.subject}", parent or self.empty(), commit.oid,
                         left_commit=parent, right_commit=commit.oid, commit=commit.oid, source=source)

    def blob(self, tree, path, limit=2_000_000):
        spec = f"{tree}:{path}"
        size = self.git("cat-file", "-s", spec, check=False)
        if size.returncode:
            return b""
        if int(size.stdout) > limit:
            raise HuicrError(f"File exceeds {limit:,} bytes; increase max_file_bytes to view it")
        kind = self.text("cat-file", "-t", spec)
        if kind != "blob":
            return b""  # Gitlinks/submodules are represented by the diff metadata.
        return self.git("cat-file", "blob", spec).stdout

    def diff(self, view, file, context=5, limit=2_000_000):
        self.blob(view.left, file.old_path, limit)
        self.blob(view.right, file.path, limit)
        patch = self.git("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames",
                         f"--unified={context}", view.left, view.right, "--", file.old_path, file.path)
        return parse_patch(patch.stdout.decode("utf-8", "replace"))

    def blame(self, view, file, side="new", limit=2_000_000):
        tree = view.left if side == "old" else view.right
        commit = view.left_commit if side == "old" else view.right_commit
        path = file.old_path if side == "old" else file.path
        content = self.blob(tree, path, limit)
        if b"\0" in content:
            raise HuicrError("Binary file has no line blame")
        if not commit:
            return [("uncommitted", "working tree", line) for line in content.decode("utf-8", "replace").splitlines()]
        # For mutable snapshots use --contents against the captured HEAD, not
        # today's disk. For commits use the exact historic tree and filename.
        exact = self.resolve(commit, "tree") == tree
        args = ["blame", "--line-porcelain"]
        if exact:
            args += [commit, "--", path]
            result = self.git(*args, check=False)
        else:
            # --contents cannot be combined with an explicit final commit in git
            # blame. A temporary commit gives the snapshot that parent instead.
            snapshot_commit = self.text("-c", "user.name=huicr", "-c", "user.email=huicr@localhost",
                                        "commit-tree", tree, "-p", commit, input=b"huicr snapshot\n")
            result = self.git(*args, snapshot_commit, "--", path, check=False)
        if result.returncode:
            raise HuicrError(result.stderr.decode("utf-8", "replace").strip())
        rows = []
        oid = author = ""
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if re.match(r"^[0-9a-f]{40,64} \d+ \d+", line):
                oid = line.split()[0]
                if not exact and oid == snapshot_commit:
                    oid = "uncommitted"
            elif line.startswith("author "):
                author = line[7:]
            elif line.startswith("\t"):
                rows.append((oid, author, line[1:]))
        return rows
