import re

from .config import HuicrError
from .git import Repo, View


def detect_target(text):
    if text.startswith("https://") or text.isdigit():
        return "pr"
    if ".." in text or (len(text) >= 7 and all(c in "0123456789abcdefABCDEF" for c in text)):
        return "range"
    return "branch"


def resolve_target(repo, target=None, *, paths=None, scope=None, no_merges=False, cwd=None, path_separator=False):
    """Share literal path/ref resolution between the CLI and the Open prompt."""
    paths = list(paths or [])
    if target and repo and not path_separator and scope in (None, "history"):
        # Preserve revision/PR targets; a bare existing or historical path is a
        # shortcut for HEAD's file history. -- always forces a path interpretation.
        is_pr = scope != "history" and not no_merges and (target.startswith("https://") or target.isdigit())
        if not is_pr and repo.git("rev-parse", "--verify", "--quiet", "--end-of-options",
                                  f"{target}^{{commit}}", check=False).returncode:
            path = repo.relative_paths([target], cwd)[0]
            if repo.has_path(path):
                paths.insert(0, target)
                target = None
    scope = scope or ("history" if paths or no_merges else detect_target(target) if target else None)
    if scope != "history" and (paths or no_merges):
        raise HuicrError("File paths and --no-merges require history scope (--history)")
    if paths:
        if repo is None:
            raise HuicrError("Run file history from inside the repository")
        paths = repo.relative_paths(paths, cwd)
    return {key: value for key, value in {"scope": scope, "target": target,
                                        "paths": paths, "no_merges": no_merges}.items() if value}


class Review:
    def __init__(self, repo: Repo, store, scope="unstaged", base=None, target=None, origin=None, *, paths=None, no_merges=False):
        self.repo, self.store = repo, store
        self.scope, self.target = scope, target or "HEAD"
        self.base = base or store.get(str(repo.root), "base") or ""
        self.origin = origin
        if scope != "history" and (paths or no_merges):
            raise HuicrError("File paths and --no-merges require history scope (--history)")
        self.paths = repo.relative_paths(paths or [])
        self.no_merges = no_merges
        self.history_commits = []
        self.history_base = ""
        self.commits = []
        self.commit_index = 0
        self.whole = False
        self.left = self.right = ""
        self.source = ""
        self.pr = None
        self.view = None

    def load(self):
        repo = self.repo
        head = repo.head()
        if self.scope in ("unstaged", "worktree", "staged"):
            left = repo.snapshot(index=True) if self.scope == "unstaged" else head or repo.empty()
            right = repo.snapshot(index=self.scope == "staged")
            self.view = repo.view(self.scope, self.scope, left, right, left_commit=head, right_commit=head)
        elif self.scope == "turn":
            key = self.origin.get("key") if self.origin else "manual"
            turn = self.store.get(str(repo.root), f"turn:{key}")
            if not turn:
                raise HuicrError("No captured turn for this agent. Wait for its next turn, or use `huicr turn begin` before edits.")
            right = repo.snapshot() if turn["active"] else turn["end"]
            self.view = repo.view("turn", "turn (in progress)" if turn["active"] else "last completed turn",
                                  turn["start"], right, left_commit=turn["head"],
                                  right_commit=turn.get("end_head") or head,
                                  source=f"turn:{turn['id']}")
        elif self.scope == "history":
            match = re.fullmatch(r"(.+?)(\.{2,3})(.+)", self.target)
            if match:
                self.history_base, self.right = self.range_bounds(match)
            else:
                self.history_base = ""
                self.right = "" if self.target == "HEAD" and not head else repo.resolve(self.target)
            self.left = self.history_base or repo.empty()
            self.history_commits = repo.history(self.right, self.history_base, self.paths) if self.right else []
            self.filter_history(self.no_merges)
        elif self.scope in ("branch", "range", "pr"):
            if self.scope == "branch":
                if not self.base:
                    self.base = repo.default_base()
                self.right = repo.resolve(self.target)
                self.left = repo.branch_base(self.base, self.right)
                self.source = f"{self.base}...{self.target} (fork {self.left[:10]})"
                if self.left == self.right and self.target != "HEAD":
                    raise HuicrError("Target is already contained in the comparator. Use a PR URL or an explicit pre-merge BASE..HEAD range.")
                self.commits = repo.commits(self.left, self.right)
            elif self.scope == "range":
                match = re.fullmatch(r"(.+?)(\.{2,3})(.+)", self.target)
                if not match:
                    # A single SHA opens that commit, including a root commit.
                    commit = repo.commit_info(self.target)
                    self.left = commit.parents[0] if commit.parents else repo.empty()
                    self.right, self.commits = commit.oid, [commit]
                else:
                    self.left, self.right = self.range_bounds(match)
                    self.commits = repo.commits(self.left, self.right)
                self.source = self.target
            elif not self.right:
                from .github import load_pr
                self.left, self.right, self.commits, self.source, self.pr = load_pr(repo, self.target)
            self.commit_index = min(self.commit_index, max(0, len(self.commits) - 1))
            self.select_view()
        else:
            raise HuicrError(f"Unknown review scope: {self.scope}")
        return self.view

    def range_bounds(self, match):
        base, operator, tip = match.groups()
        left, right = self.repo.resolve(base), self.repo.resolve(tip)
        if operator == "...":
            left = self.repo.merge_base(left, right)
        elif self.repo.git("merge-base", "--is-ancestor", left, right, check=False).returncode:
            raise HuicrError("BASE..HEAD requires an ancestor base; use BASE...HEAD for divergent branches")
        return left, right

    def filter_history(self, no_merges):
        selected = self.commits[self.commit_index].oid if self.commits else None
        self.no_merges = no_merges
        self.commits = [c for c in self.history_commits if not no_merges or len(c.parents) < 2]
        fallback = min(self.commit_index, max(0, len(self.commits) - 1))
        self.commit_index = next((i for i, c in enumerate(self.commits) if c.oid == selected), fallback)
        self.source = self.target + (" (non-merge)" if no_merges else "")
        if self.paths:
            self.source += " -- " + ", ".join(self.paths)
        return self.select_view()

    def select_view(self):
        if self.scope == "history" and not self.commits:
            tree = self.right or self.left
            self.view = self.repo.view(self.scope, f"No commits match history: {self.source}", tree, tree,
                                       right_commit=self.right, source=self.source)
        elif self.commits and not self.whole:
            self.view = self.repo.commit_view(self.commits[self.commit_index], self.scope, self.source, self.paths)
        else:
            left_commit = self.history_base if self.scope == "history" else self.left if self.commits and self.commits[0].parents else ""
            self.view = self.repo.view(self.scope, f"whole {self.scope}: {self.source}", self.left, self.right,
                                       left_commit=left_commit, right_commit=self.right, source=self.source, paths=self.paths)
        self.view.pr = self.pr
        return self.view

    def anchor(self, file, lines, start=None, end=None, side=None):
        v = self.view
        anchor = {k: getattr(v, k) for k in ("scope", "label", "left", "right", "left_commit", "right_commit", "commit", "source")}
        anchor.update(view=v.key, path=file.path, old_path=file.old_path, status=file.status,
                      side="file", start=None, end=None, snippet="", pr=v.pr)
        if start is not None:
            if not 0 <= start < len(lines):
                raise HuicrError("Select a code line, or use C for a file comment")
            chosen = lines[min(start, end if end is not None else start):max(start, end if end is not None else start) + 1]
            side = side or ("old" if lines[start].kind == "delete" else "new")
            numbers = [line.old if side == "old" else line.new for line in chosen]
            numbers = [n for n in numbers if n is not None]
            if not numbers:
                raise HuicrError("Select a code line, or use C for a file comment")
            anchor.update(side=side, start=min(numbers), end=max(numbers),
                          snippet="\n".join(({"add": "+", "delete": "-", "context": " "}.get(line.kind, "") + line.text) for line in chosen))
        return anchor

    def restore_anchor(self, anchor):
        self.view = View(**{k: anchor[k] for k in ("scope", "label", "left", "right", "left_commit", "right_commit", "commit", "source")})
        self.view.pr = anchor.get("pr")
        self.view.files = self.repo.changes(self.view.left, self.view.right)
        return self.view
