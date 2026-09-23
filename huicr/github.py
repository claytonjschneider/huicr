"""Fetch review objects into a private namespace, never the user's checkout."""

import json
import re
from urllib.parse import urlparse

from .config import HuicrError
from .process import run


def gh(*args):
    return run(["gh", *args], timeout=120).stdout.decode()


def load_pr(repo, target):
    match = re.fullmatch(r"https://([^/]+)/([\w.-]+)/([\w.-]+)/pull/(\d+)/?", target)
    if match:
        host, owner, name, number = match.groups()
    elif target.isdigit():
        info = json.loads(run(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd=repo.root).stdout)
        host = urlparse(info["url"]).hostname
        owner, name = info["nameWithOwner"].split("/")
        number = target
    else:
        raise HuicrError("Expected a GitHub PR URL or number")
    endpoint = f"repos/{owner}/{name}/pulls/{number}"
    info = json.loads(gh("api", "--hostname", host, endpoint))
    head = info["head"]["sha"]
    base = info["base"]["sha"]
    remote = f"https://{host}/{owner}/{name}.git"
    namespace = f"refs/huicr/pr/{host}/{owner}/{name}/{number}"
    repo.git("fetch", "--no-tags", "--no-write-fetch-head", remote,
             f"+refs/pull/{number}/head:{namespace}/head", timeout=120)
    if repo.resolve(f"{namespace}/head") != head:
        raise HuicrError("PR changed during fetch. Refresh to capture a consistent review.")

    def ensure(oid, name):
        if repo.git("cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode:
            repo.git("fetch", "--no-tags", "--no-write-fetch-head", remote, f"{oid}:{namespace}/{name}", timeout=120)
        repo.git("update-ref", f"{namespace}/{name}", oid)

    # A merged PR's current target contains the PR itself. Its merge commit's
    # first parent captures the target before integration (also handles squash).
    # Original PR commits always retain their actual parents for commit-wise diffs.
    if info.get("merged"):
        merge = info.get("merge_commit_sha")
        if not merge:
            raise HuicrError("GitHub did not provide a merge revision. Open an explicit BASE..HEAD range.")
        ensure(merge, "merge")
        parents = repo.commit_info(merge).parents
        if not parents:
            raise HuicrError("Merge revision has no parent; use an explicit range")
        base = parents[0]
    ensure(base, "base")
    left = repo.merge_base(base, head)
    # Paginate; gh pr view's commits field alone silently caps large PRs.
    ids = gh("api", "--hostname", host, "--paginate", f"{endpoint}/commits?per_page=100", "--jq", ".[].sha").splitlines()
    if info.get("commits", 0) > len(ids):
        raise HuicrError("GitHub's PR commit API truncated this PR; review an explicit Git range instead")
    if not ids or ids[-1] != head:
        raise HuicrError("PR commit list changed during loading; refresh")
    commits = [repo.commit_info(oid) for oid in ids]
    # Fast-forward/rebase merges can leave the original commits on the target.
    # In that case merge^1 is *inside* the PR, not the pre-PR target revision.
    if left in ids:
        left = commits[0].parents[0] if commits[0].parents else repo.empty()
    return left, head, commits, f"{info['html_url']} — {info['title']} (target {info['base']['ref']}, fork {left[:10]})"
