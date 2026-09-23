import re

from .config import HuicrError
from .git import Repo, View


class Review:
    def __init__(self, repo: Repo, store, scope="unstaged", base=None, target=None, origin=None):
        self.repo, self.store = repo, store
        self.scope, self.target = scope, target or "HEAD"
        self.base = base or store.get(str(repo.root), "base") or ""
        self.origin = origin
        self.commits = []
        self.commit_index = 0
        self.whole = False
        self.left = self.right = ""
        self.source = ""
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
                    base, operator, tip = match.groups()
                    self.left, self.right = repo.resolve(base), repo.resolve(tip)
                    if operator == "...":
                        self.left = repo.merge_base(self.left, self.right)
                    elif repo.git("merge-base", "--is-ancestor", self.left, self.right, check=False).returncode:
                        raise HuicrError("BASE..HEAD requires an ancestor base; use BASE...HEAD for divergent branches")
                    self.commits = repo.commits(self.left, self.right)
                self.source = self.target
            elif not self.right:
                from .github import load_pr
                self.left, self.right, self.commits, self.source = load_pr(repo, self.target)
            self.commit_index = min(self.commit_index, max(0, len(self.commits) - 1))
            self.select_view()
        else:
            raise HuicrError(f"Unknown review scope: {self.scope}")
        return self.view

    def select_view(self):
        if self.commits and not self.whole:
            self.view = self.repo.commit_view(self.commits[self.commit_index], self.scope, self.source)
        else:
            self.view = self.repo.view(self.scope, f"whole {self.scope}: {self.source}", self.left, self.right,
                                       left_commit=self.left if self.commits and self.commits[0].parents else "",
                                       right_commit=self.right, source=self.source)
        return self.view

    def anchor(self, file, lines, start=None, end=None, side=None):
        v = self.view
        anchor = {k: getattr(v, k) for k in ("scope", "label", "left", "right", "left_commit", "right_commit", "commit", "source")}
        anchor.update(view=v.key, path=file.path, old_path=file.old_path, status=file.status,
                      side="file", start=None, end=None, snippet="")
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
        self.view.files = self.repo.changes(self.view.left, self.view.right)
        return self.view
