import argparse
import json
import os
import shutil
import sys

from . import VERSION
from .config import Config, HuicrError
from .git import Repo
from .herdr import action, call, capture_turn, event, identity, send
from .review import Review
from .state import Store, format_comments


def parser():
    p = argparse.ArgumentParser(prog="huicr", description="huicr — commit-aware personal review in Herdr")
    p.add_argument("--version", action="version", version=VERSION)
    subs = p.add_subparsers(dest="command", required=True)
    for command in ("review", "open"):
        child = subs.add_parser(command, help="review in this terminal" if command == "review" else "open a Herdr review pane (nonblocking)")
        child.add_argument("target", nargs="?", help="branch, SHA, BASE..HEAD, BASE...HEAD, PR URL/number")
        child.add_argument("--scope", choices=("unstaged", "worktree", "staged", "branch", "turn", "range", "pr"))
        child.add_argument("--base", help="comparator branch/revision")
        if command == "review":
            child.add_argument("--repo", default=os.environ.get("HUICR_REPO", "."))
            child.add_argument("--whole", action="store_true", help="start with the aggregate range diff")
        else:
            child.add_argument("--focus", action="store_true", help="take keyboard focus (agent opens preserve it by default)")
    child = subs.add_parser("action", help="Herdr manifest action entrypoint")
    child.add_argument("mode", choices=("open", "toggle", "close"))
    subs.add_parser("event", help="Herdr turn-tracking event entrypoint")
    child = subs.add_parser("turn", help="exact explicit turn boundaries for agent hooks/scripts")
    child.add_argument("phase", choices=("begin", "end"))
    child.add_argument("--repo", default=".")
    child.add_argument("--manual", action="store_true", help="use standalone/manual identity")
    child = subs.add_parser("comments", help="read durable comments without opening a pane")
    child.add_argument("--repo", default=".")
    child.add_argument("--pending", action="store_true")
    child.add_argument("--format", choices=("json", "markdown"), default="json")
    child = subs.add_parser("send", help="send pending comments to a specific live agent")
    child.add_argument("--repo", default=".")
    child.add_argument("--to", required=True, help="Herdr agent pane ID")
    child.add_argument("--mode", choices=("paste", "submit"), default="paste")
    child.add_argument("--id", action="append", help="send just these comment IDs (repeatable)")
    child = subs.add_parser("publish", help="post PR drafts as a GitHub comment review")
    child.add_argument("target", help="GitHub PR URL/number")
    child.add_argument("--repo", default=".")
    child.add_argument("--id", action="append", help="publish just these comment IDs (repeatable)")
    child.add_argument("--retry", action="store_true", help="authorize a retry after checking GitHub for an uncertain prior post")
    subs.add_parser("state-path", help="print the local review database path")
    subs.add_parser("doctor", help="check required executables and configuration")
    return p


def main():
    args = parser().parse_args()
    store = None
    try:
        os.umask(0o077)
        config = Config.load()
        store = Store()
        if args.command == "state-path":
            print(store.path)
        elif args.command == "doctor":
            for executable in ("git", "python3", "herdr", "gh"):
                print(f"{executable}: {shutil.which(executable) or 'not found'}")
            print(f"Python: {sys.version.split()[0]} · config: valid · state: {store.path}")
            print("gh is needed only for PRs; herdr is needed for pane actions and sending.")
        elif args.command == "event":
            event(store, config)
        elif args.command == "action":
            print(json.dumps(action(args.mode, store, config)))
        elif args.command == "open":
            from .ui import detect_target
            request = {key: value for key, value in {"scope": args.scope or (detect_target(args.target) if args.target else None),
                                                     "target": args.target, "base": args.base}.items() if value}
            print(json.dumps(action("open", store, config, request or None, focus=args.focus)))
        elif args.command == "turn":
            repo = Repo(args.repo)
            key = "manual"
            if not args.manual and os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_PANE_ID"):
                pane = call("pane", "get", os.environ["HERDR_PANE_ID"])["pane"]
                key = identity(pane, repo)["key"]
            print(json.dumps(capture_turn(repo, store, key, args.phase)))
        elif args.command == "comments":
            repo = Repo(args.repo)
            comments = store.comments(str(repo.root), pending=args.pending)
            print(json.dumps(comments, indent=2) if args.format == "json" else format_comments(comments, str(repo.root)))
        elif args.command == "send":
            repo = Repo(args.repo)
            pane = call("agent", "get", args.to)["agent"]
            print(send(store, str(repo.root), identity(pane, repo), args.mode, args.id))
        elif args.command == "publish":
            from .github import publish
            print(publish(Repo(args.repo), store, args.target, args.id, retry=args.retry))
        elif args.command == "review":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise HuicrError("Review needs an interactive terminal. Use `huicr open` inside Herdr or `huicr comments` for JSON.")
            from .ui import detect_target, launch
            repo = Repo(args.repo)
            origin = json.loads(os.environ.get("HUICR_ORIGIN", "null"))
            if not origin and os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_PANE_ID"):
                pane = call("pane", "get", os.environ["HERDR_PANE_ID"])["pane"]
                if pane.get("agent"):
                    origin = identity(pane, repo)
            request = json.loads(os.environ.get("HUICR_REQUEST", "null")) or {}
            saved = store.get(str(repo.root), "ui", {})
            explicit = bool(args.target or args.scope or args.base or request)
            options = request if request else vars(args) if explicit else saved
            target = options.get("target")
            scope = options.get("scope") or (detect_target(target) if target else config.default_scope)
            review = Review(repo, store, scope, options.get("base"), target, origin)
            review.whole = args.whole or (not explicit and saved.get("whole", False))
            review.commit_index = saved.get("commit_index", 0) if not explicit else 0
            launch(review, config, saved if not explicit else None)
    except (HuicrError, OSError, ValueError) as e:
        print(f"huicr: {e}", file=sys.stderr)
        if args.command == "review" and sys.stdin.isatty():
            input("Press Enter to close…")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
