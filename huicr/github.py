"""GitHub PR snapshots and explicit, durable review publication."""

import json
import re
from urllib.parse import urlparse

from .config import HuicrError
from .git import parse_patch
from .process import run


def gh(*args, cwd=None, input=b""):
    return run(["gh", *args], cwd=cwd, env={"GH_PROMPT_DISABLED": "1"},
               input=input, timeout=120).stdout.decode()


def pr_target(repo, target):
    match = re.fullmatch(r"https://([^/]+)/([\w.-]+)/([\w.-]+)/pull/(\d+)/?", target)
    if match:
        host, owner, name, number = match.groups()
    elif target.isdigit():
        info = json.loads(gh("repo", "view", "--json", "nameWithOwner,url", cwd=repo.root))
        host = urlparse(info["url"]).hostname
        owner, name = info["nameWithOwner"].split("/")
        number = target
    else:
        raise HuicrError("Expected a GitHub PR URL or number")
    host, owner, name, number = host.lower(), owner.lower(), name.lower(), str(int(number))
    return host, f"repos/{owner}/{name}/pulls/{number}", f"https://{host}/{owner}/{name}/pull/{number}"


def load_pr(repo, target):
    host, endpoint, _ = pr_target(repo, target)
    _, owner, name, _, number = endpoint.split("/")
    info = json.loads(gh("api", "--hostname", host, endpoint))
    head = info["head"]["sha"]
    base = info["base"]["sha"]
    remote = f"https://{host}/{owner}/{name}.git"
    namespace = f"refs/huicr/pr/{host}/{owner}/{name}/{number}"

    def fetch(refspec):
        # Use the same credentials as the API, including gh's stored login or
        # environment token. Scope the helper to this host and this invocation.
        # Git's /dev/tty and askpass prompts bypass curses, so disable both.
        repo.git("-c", f"credential.https://{host}.helper=",
                 "-c", f"credential.https://{host}.helper=!gh auth git-credential",
                 "fetch", "--no-tags", "--no-write-fetch-head", remote, refspec,
                 env={"GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false"},
                 input=b"", timeout=120)

    fetch(f"+refs/pull/{number}/head:{namespace}/head")
    if repo.resolve(f"{namespace}/head") != head:
        raise HuicrError("PR changed during fetch. Refresh to capture a consistent review.")

    def ensure(oid, name):
        if repo.git("cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode:
            fetch(f"{oid}:{namespace}/{name}")
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
    pr = {"url": pr_target(repo, info["html_url"])[2], "head": head, "base": info["base"]["sha"]}
    return left, head, commits, f"{info['html_url']} — {info['title']} (target {info['base']['ref']}, fork {left[:10]})", pr


class GitHubError(HuicrError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api(host, endpoint, *, data=None, paginate=False):
    args = ["api", "--hostname", host, endpoint,
            "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if data is not None:
        args += ["--method", "POST", "--input", "-"]
    if paginate:
        args += ["--paginate", "--slurp"]
    try:
        response = json.loads(gh(*args, input=json.dumps(data).encode() if data is not None else b""))
        if paginate:
            if not isinstance(response, list) or any(not isinstance(page, list) for page in response):
                raise ValueError("Expected paginated arrays")
            if any(not isinstance(item, dict) for page in response for item in page):
                raise ValueError("Expected objects in each page")
            return [item for page in response for item in page]
        if not isinstance(response, dict):
            raise ValueError("Expected an object")
        return response
    except HuicrError as e:
        # gh reports API failures as '(HTTP NNN)'. Unknown failures, including
        # timeouts and malformed responses, cannot prove a POST was rejected.
        match = re.search(r"\(HTTP (\d{3})\)", str(e))
        raise GitHubError(str(e), int(match[1]) if match else None) from e
    except (ValueError, TypeError) as e:
        raise GitHubError("Invalid GitHub API response") from e


def publishable(comment, url):
    anchor = comment["anchor"]
    return (anchor["scope"] == "pr" and not anchor["commit"]
            and (anchor.get("pr") or {}).get("url") == url)


def pending_review_comments(store, repo, url, ids=None):
    comments = store.comments(repo, include_resolved=False)
    if ids is not None:
        chosen = [c for c in comments if c["id"] in ids]
        if {c["id"] for c in chosen} != set(ids) or any(not publishable(c, url) for c in chosen):
            raise HuicrError("Select unresolved comments saved from this PR's whole diff (m) to post to GitHub.")
        comments = chosen
    comments = [c for c in comments if publishable(c, url) and c["version"] > c["github_version"]]
    if not comments:
        raise HuicrError("No unpublished whole-PR comments. Press m, then add comments for GitHub.")
    return comments


def review_payload(comments, info, files):
    if info["state"] != "open" or info.get("merged"):
        raise HuicrError("GitHub publication requires an open PR.")
    files = {file["filename"]: file for file in files}
    inline = []
    summary = ["Review comments from huicr."]
    for comment in comments:
        anchor = comment["anchor"]
        pr = anchor["pr"]
        if (pr["head"] != info["head"]["sha"] or pr["base"] != info["base"]["sha"]
                or anchor["right_commit"] != pr["head"]):
            raise HuicrError("PR revisions changed. Refresh (r), then recreate stale comments in the whole-PR diff (m).")
        path = anchor["path"]
        if path not in files:
            raise HuicrError(f"{path} is absent from GitHub's PR diff; refresh and review its current location.")
        if anchor["side"] == "file":
            fence = "`" * (1 + max((len(m) for m in re.findall(r"`+", path)), default=0))
            summary.append(f"### {fence}{path}{fence}\n\n{comment['body']}")
            continue
        side = {"old": "LEFT", "new": "RIGHT"}.get(anchor["side"])
        start, end = anchor["start"], anchor["end"]
        if not side or not isinstance(start, int) or not isinstance(end, int) or not 0 < start <= end:
            raise HuicrError(f"Invalid GitHub comment range for {path}")
        # GitHub's context can differ from the configured local diff. Both ends
        # must exist on the selected side of one actual GitHub diff hunk.
        hunk = 0
        locations = {}
        for line in parse_patch(files[path].get("patch") or ""):
            if line.kind == "hunk":
                hunk += 1
            number = line.old if side == "LEFT" else line.new
            if number is not None:
                locations[number] = hunk
        if start not in locations or locations[start] != locations.get(end):
            raise HuicrError(f"{path}:{start}-{end} is outside one GitHub diff hunk. Select a changed line or add a file comment.")
        item = {"path": path, "body": comment["body"], "side": side, "line": end}
        if start != end:
            item.update(start_line=start, start_side=side)
        inline.append(item)
    payload = {"commit_id": info["head"]["sha"], "event": "COMMENT", "body": "\n\n".join(summary)}
    if inline:
        payload["comments"] = inline
    return payload


def published_result(delivery, review):
    marker = f"<!-- huicr-review:{delivery['id']} -->"
    if (review.get("state") not in ("COMMENTED", "DISMISSED")
            or review.get("commit_id") != delivery["payload"]["commit_id"]
            or marker not in (review.get("body") or "")
            or not review.get("id") or not review.get("html_url")):
        raise HuicrError("GitHub has not confirmed the submitted review.")
    return {key: review[key] for key in ("id", "html_url", "commit_id")}


def recover_review(store, delivery, host, endpoint, *, retry=False):
    # Caller holds the same publication lock as publish(). Reconcile even when
    # the PR has advanced/closed or the local drafts were edited/deleted.
    marker = f"<!-- huicr-review:{delivery['id']} -->"
    matches = [review for review in api(host, f"{endpoint}/reviews?per_page=100", paginate=True)
               if marker in (review.get("body") or "")]
    if len(matches) > 1:
        raise HuicrError("Multiple GitHub reviews match this batch; inspect the PR before continuing.")
    if matches:
        result = published_result(delivery, matches[0])
        store.finish_github(delivery["id"], "sent", result)
        return result["html_url"]
    if retry:
        store.finish_github(delivery["id"], "failed", error="User checked GitHub and authorized a retry; no matching review found.")
    return None


def reconcile_publication(repo, store, target, *, retry=False):
    host, endpoint, url = pr_target(repo, target)
    key = str(repo.root)
    with store.lock(f"github:{key}:{url}"):
        deliveries = store.github_deliveries(key, url, unresolved=True)
        if not deliveries:
            return None
        return recover_review(store, deliveries[0], host, endpoint, retry=retry)


def publish(repo, store, target, ids=None, *, retry=False):
    host, endpoint, url = pr_target(repo, target)
    key = str(repo.root)
    with store.lock(f"github:{key}:{url}"):
        deliveries = store.github_deliveries(key, url, unresolved=True)
        if deliveries:
            recovered = recover_review(store, deliveries[0], host, endpoint, retry=retry)
            if recovered:
                return recovered
            if not retry:
                raise HuicrError("GitHub outcome still uncertain; no matching review found. Check the PR, then use X to authorize a retry.")
        comments = pending_review_comments(store, key, url, ids)
        info = api(host, endpoint)
        files = api(host, f"{endpoint}/files?per_page=100", paginate=True)
        payload = review_payload(comments, info, files)
        delivery = store.prepare_github(key, url, comments, payload)
        try:
            response = api(host, f"{endpoint}/reviews", data=delivery["payload"])
            result = published_result(delivery, response)
        except (HuicrError, ValueError, TypeError, AttributeError) as e:
            status = e.status if isinstance(e, GitHubError) else None
            rejected = status is not None and 400 <= status < 500 and status != 408
            store.finish_github(delivery["id"], "failed" if rejected else "uncertain", error=str(e))
            if rejected:
                raise HuicrError(f"GitHub rejected the review; drafts retained. {e}") from e
            raise HuicrError(f"GitHub outcome uncertain; drafts retained. Press P or X to reconcile. {e}") from e
        store.finish_github(delivery["id"], "sent", result)
        return result["html_url"]
