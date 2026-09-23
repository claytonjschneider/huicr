import json
import os

from . import PLUGIN_ID
from .config import HuicrError
from .git import Repo
from .process import run
from .state import now, uid


def call(*args):
    if os.environ.get("HERDR_ENV") != "1":
        raise HuicrError("This action needs a Herdr pane. Use `huicr review` for standalone review.")
    result = run([os.environ.get("HERDR_BIN_PATH", "herdr"), *args], timeout=30)
    try:
        response = json.loads(result.stdout)
        if "error" in response:
            raise HuicrError(response["error"].get("message", str(response["error"])))
        return response["result"]
    except (KeyError, ValueError) as e:
        raise HuicrError("Invalid Herdr response") from e


def socket_key():
    return os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HERDR_SESSION", "default")


def identity(pane, repo):
    session = pane.get("agent_session")
    key = f"{socket_key()}:{pane['pane_id']}:{pane['terminal_id']}:{json.dumps(session, sort_keys=True)}"
    return {"pane_id": pane["pane_id"], "terminal_id": pane["terminal_id"], "agent": pane.get("agent"),
            "agent_session": session, "repo": str(repo.root), "key": key, "socket": socket_key()}


def repo_for_pane(pane, context=None):
    context = context or {}
    candidates = [pane.get("foreground_cwd"), (context.get("worktree") or {}).get("checkout_path"),
                  pane.get("cwd"), context.get("workspace_cwd")]
    for cwd in candidates:
        if cwd:
            try:
                return Repo(cwd)
            except HuicrError:
                continue
    raise HuicrError("The invoking pane/workspace has no Git worktree")


def validate_target(target):
    if not target or not target.get("agent"):
        raise HuicrError("No agent selected. Press A to choose a recipient.")
    if target.get("socket") != socket_key():
        raise HuicrError("Recipient belongs to another Herdr session. Press A to reselect.")
    pane = call("agent", "get", target["pane_id"])["agent"]
    if (pane.get("terminal_id") != target["terminal_id"] or pane.get("agent") != target["agent"]
            or pane.get("agent_session") != target.get("agent_session")):
        raise HuicrError("The originating agent has changed. Press A to choose the recipient again.")
    if pane.get("agent_status") == "blocked":
        raise HuicrError("Agent is at a question/approval dialog. Resolve that before sending comments.")
    if str(repo_for_pane(pane).root) != target["repo"]:
        raise HuicrError("Agent is now in another worktree. Press A to choose a recipient.")
    return pane


def send(store, repo, target, mode="paste", ids=None):
    with store.lock(f"delivery:{repo}"):
        validate_target(target)
        delivery_id, payload = store.prepare(repo, target, mode, ids)
        try:
            if mode == "submit":
                call("agent", "prompt", target["pane_id"], payload)
            else:
                call("pane", "send-text", target["pane_id"], payload)
        except HuicrError as e:
            # The transport cannot prove whether bytes reached the PTY before a
            # failure. Keep the batch in the outbox rather than risk duplicates.
            store.finish(delivery_id, "uncertain", str(e))
            raise HuicrError(f"Delivery outcome uncertain; drafts retained. Press X after checking the agent. {e}") from e
        store.finish(delivery_id, "sent")
        return delivery_id


def action(mode, store, config, request=None, focus=True):
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    pane_id = os.environ.get("HERDR_PANE_ID") or context.get("focused_pane_id")
    if not pane_id:
        raise HuicrError("No invoking pane")
    pane = call("pane", "get", pane_id)["pane"]
    workspace = pane["workspace_id"]
    clicked = os.environ.get("HERDR_PLUGIN_CLICKED_URL") or context.get("clicked_url")
    if clicked:
        request = {"scope": "pr", "target": clicked}
    key = f"pane:{socket_key()}:{workspace}"
    with store.lock(key):
        registered = store.get("", key)
        panes = call("pane", "list", "--workspace", workspace)["panes"]
        existing = next((p for p in panes if registered and p["pane_id"] == registered["pane_id"]
                         and p["terminal_id"] == registered["terminal_id"]), None)
        if existing:
            if mode in ("toggle", "close"):
                call("plugin", "pane", "close", existing["pane_id"])
                store.put("", key, None)
                return {"closed": existing["pane_id"]}
            update = dict(request or {})
            if pane.get("agent"):
                repo = repo_for_pane(pane, context)
                if str(repo.root) != registered["repo"]:
                    raise HuicrError("The open huicr pane is reviewing another worktree. Close it before opening this repo.")
                update["origin"] = identity(pane, repo)
            if update:
                store.put(registered["repo"], "request", {**update, "id": uid()})
            if focus:
                call("plugin", "pane", "focus", existing["pane_id"])
            return {"opened": existing["pane_id"]}
        if mode == "close":
            return {"closed": None}
        repo = repo_for_pane(pane, context)
        origin = identity(pane, repo) if pane.get("agent") else None
        options = ["--placement", config.placement, "--cwd", str(repo.root)]
        if config.placement == "tab":
            options += ["--workspace", workspace]
        else:
            options += ["--target-pane", pane_id]
            if config.placement == "split":
                options += ["--direction", config.direction]
        options += ["--focus" if focus else "--no-focus",
                    "--env", f"HUICR_REPO={repo.root}",
                    "--env", f"HUICR_ORIGIN={json.dumps(origin)}",
                    "--env", f"HUICR_REQUEST={json.dumps(request)}"]
        result = call("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "review", *options)
        opened = result["plugin_pane"]["pane"]
        store.put("", key, {"pane_id": opened["pane_id"], "terminal_id": opened["terminal_id"], "repo": str(repo.root)})
        if config.placement == "tab":
            call("tab", "rename", opened["tab_id"], "huicr")
        return {"opened": opened["pane_id"], "repo": str(repo.root)}


def capture_turn(repo, store, key="manual", phase="begin", automatic=False):
    with store.lock(f"turn:{repo.root}:{key}"):
        previous = store.get(str(repo.root), f"turn:{key}")
        if phase == "begin":
            if automatic and previous and previous["active"]:
                return previous  # blocked -> working resumes this same turn
            turn = {"id": uid(), "head": repo.head(), "start": repo.snapshot(), "end": None,
                    "active": True, "started": now(), "automatic": automatic}
        else:
            if not previous or not previous["active"]:
                return previous
            turn = {**previous, "end": repo.snapshot(), "end_head": repo.head(), "active": False, "ended": now()}
        store.put(str(repo.root), f"turn:{key}", turn)
        return turn


def event(store, config):
    if not config.track_turns:
        return
    payload = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
    data = payload.get("data", {})
    status = data.get("agent_status")
    if status not in ("working", "idle", "done") or not data.get("agent"):
        return
    pane = call("pane", "get", data["pane_id"])["pane"]
    # Hooks execute asynchronously. Never label a snapshot of a later state as
    # the beginning of an already-finished turn.
    if pane.get("agent_status") != status and {pane.get("agent_status"), status} != {"idle", "done"}:
        return
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    try:
        repo = repo_for_pane(pane, context)
    except HuicrError:
        return
    origin = identity(pane, repo)
    capture_turn(repo, store, origin["key"], "begin" if status == "working" else "end", automatic=True)
