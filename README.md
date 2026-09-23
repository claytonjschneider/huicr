# huicr

A personal code-review pane for [Herdr](https://herdr.dev), pronounced like **weaker**.

Review unstaged changes, an agent turn, a branch, a commit range, or a GitHub PR. Walk commits in their original context, leave durable line/file comments, and send incremental feedback to your agent **without closing the review**.

Inspired by [tuicr](https://tuicr.dev), [herdr-reviewr](https://github.com/persiyanov/herdr-reviewr), and [herdr-tuicr](https://github.com/ekropotin/herdr-tuicr). This is a new implementation, with its own review state and terminal UI.

## Requirements

- macOS or Linux; Python **3.11+** with curses; Git.
- Herdr **0.9.1+** for pane actions, turn events, and feedback delivery.
- Authenticated `gh` and Git access to the repository for GitHub PR reviews.

No third-party Python runtime dependencies. The terminal UI also runs standalone.

## Install

```sh
herdr plugin install claytonjschneider/huicr
```

### Local development

```sh
herdr plugin link /path/to/huicr
herdr plugin enable claytonjschneider.huicr
```

Linking runs the source checkout directly; no build is needed. Reopen the review pane after changing its code. Changes to the manifest require relinking.

Add a shortcut to your Herdr user configuration:

```toml
[[keys.command]]
key = "prefix+shift+o"
type = "plugin_action"
command = "claytonjschneider.huicr.toggle"
description = "huicr: toggle review"
```

Then `herdr server reload-config`. You can also invoke:

```sh
herdr plugin action invoke open --plugin claytonjschneider.huicr
herdr plugin action invoke toggle --plugin claytonjschneider.huicr
```

### Pane configuration

`herdr plugin config-dir claytonjschneider.huicr` prints the configuration directory. Put `config.toml` there; see [config.toml.example](config.toml.example).

```toml
placement = "tab"         # split | zoomed | tab
direction = "right"       # right | down, for splits
default_scope = "unstaged"
context_lines = 5
max_file_bytes = 2000000
track_turns = true
```

`tab` opens a dedicated **huicr** tab. Toggle closes it; the next toggle reopens your saved review. One managed review pane is opened per workspace. Its comments, selected scope, commit, file, and position survive closing. Settings are read when an action/pane/event starts.

### Shell command

Herdr's plugin installation registers the extension; the optional shell command is installed separately:

```sh
pipx install git+https://github.com/claytonjschneider/huicr.git
huicr -h
```

For a local checkout, use `pipx install /path/to/huicr`, run `/path/to/huicr/bin/huicr` directly, or add `/path/to/huicr/bin` to your shell's PATH. Configuration and review state live in huicr's own user directories, independently of the source checkout.

## Review scopes

| Key | Scope | Comparison |
| --- | --- | --- |
| `u` | Unstaged | Index → worktree, including non-ignored untracked files |
| `U` | All uncommitted | HEAD → worktree, including staged changes |
| `i` | Staged | HEAD → index |
| `b` | Branch | Merge-base with the selected comparator → branch tip |
| `t` | Turn | Captured worktree at turn start → captured end, or current worktree while running |
| `o` | Open | A branch, SHA, `BASE..HEAD`, `BASE...HEAD`, or GitHub PR URL/number |
| `B` | Comparator | Any local/remote branch, tag, or revision; saved per worktree |

Branch/range/PR reviews start **commit-wise**. `,` and `.` step through commits; `m` toggles the aggregate diff. `Tab` can focus the commit list, file list, or diff. Every commit is diffed against its **actual first parent**, including merge commits; root commits are diffed against the empty tree. Branch history follows the first-parent chain in oldest-first order. PR commit lists use GitHub's paginated commit API.

`BASE..HEAD` excludes BASE and requires it to be an ancestor. `BASE...HEAD` resolves a merge-base for divergent branches. `o feature-branch` uses your saved comparator, or the remote default branch / main / master. Choose a stacked branch's comparator with `B`.

For an already-merged branch, huicr finds its integration edge in the comparator's first-parent history and recovers the pre-merge comparison. For merged PRs, it loads the merge revision and the original PR commits, rather than comparing with today's target tip. Merge, squash, rebase, and fast-forward PR histories are covered by tests. A bare fast-forwarded/rebased branch can lack a recoverable boundary; use its PR URL or an explicit range in that case.

Reviews never check out another branch. PR objects are fetched under `refs/huicr/pr/…`. Index/worktree snapshots use an alternate index, leaving the real index intact, including staged/unstaged boundaries and split indexes. Snapshots remain frozen until you press `r`; this keeps line comments stable as the agent continues editing.

## Navigation and blame

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move in the focused area |
| `Tab`, `Enter` | Cycle focus; focus diff |
| `Ctrl+D` / `Ctrl+U`, PageDown / PageUp | Move a half-page |
| `g` / `G` | First / last item |
| `h` / `l` | Horizontal scroll |
| `{` / `}`, `F` / `f` | Previous / next file |
| `[` / `]` | Previous / next hunk |
| `/`, `n` / `N` | Find, next / previous match |
| `a` | Toggle full-file blame at the captured revision |
| `H` | Switch old/new side in blame |
| `R` | Mark this file/revision reviewed |
| `z` | Hide/show navigator |
| `r` | Refresh snapshot / ref / PR |
| `?` | Help |
| `q` | Close, preserving review state |

Blame on a removed line starts on the old side, using the historical filename for renames. Mutable snapshots attribute unchanged lines to history and label new lines `uncommitted`. Comments work from blame too. File content, filenames, and commit messages are rendered as text, including escaped/control characters.

## Comments and the agent loop

1. Move to a line, or press `v` and extend a range.
2. `c` comments on it; `C` comments on the file.
3. A bordered paragraph box opens **inline below the selected line**, or below the file header for file comments. It starts with **one editable line**, grows as you type/wrap, and keeps surrounding code visible. **Enter** inserts a newline, **Ctrl+S** saves, **Esc** cancels (25 ms escape-key delay).
4. `s` pastes all unsent drafts into the agent's input; **you press Enter there**. `S` submits them immediately.
5. Continue reviewing. Sending never closes the pane.

Saved comments expand into the same inline boxes when you navigate to their lines. `e` edits the comment under the cursor; `d` deletes it locally. `L` opens the comment list across scopes and commits: jump back to the exact captured diff, edit inline, resolve/reopen with `x`, or use `s`/`S` to send just the selected comment. `A` selects a recipient among agents in the same worktree.

The invoking agent is captured by pane ID, terminal ID, agent kind, and native session identity when available. A later `huicr open` from another agent explicitly retargets the open review. Feedback is not sent to an arbitrary focused pane. If the recipient exits, changes session, moves to a different worktree, or is at an approval dialog, sending retains the drafts and reports what needs attention.

Each comment stores its ID/version, scope, commit, old/new trees, filename, side, line range, and snippet. Sending marks only the delivered versions; editing a sent comment makes its new version pending. Resolving/deleting locally doesn't retract earlier feedback.

A durable outbox is written **before** transport. A failed/crashed send can have an uncertain outcome because terminal input has no transactional acknowledgement from the agent. Huicr retains the batch and prevents automatic resending. After checking the agent, press `X` and enter `sent` or `retry`. “Sent” means Herdr accepted the input, not that the agent acted on it. Paste-mode feedback is considered delivered once pasted.

All feedback is personal and local. Nothing posts review comments to GitHub.

## Agent / CLI usage

Run the checkout's launcher, or install the optional console entrypoint with `pipx install /path/to/huicr`:

```sh
# Nonblocking: open in Herdr, retain the agent's keyboard focus.
/path/to/huicr/bin/huicr open --scope unstaged
/path/to/huicr/bin/huicr open feature-branch --base origin/main
/path/to/huicr/bin/huicr open 'main..feature-branch'
/path/to/huicr/bin/huicr open https://github.com/owner/repo/pull/123

# Standalone in the current terminal.
/path/to/huicr/bin/huicr review --repo /path/to/repo

# Read structured feedback while a review remains open.
/path/to/huicr/bin/huicr comments --repo /path/to/repo --pending
/path/to/huicr/bin/huicr comments --format markdown

# Send specific drafts (omit --id for all pending drafts).
/path/to/huicr/bin/huicr send --to w1:p1 --mode submit --id COMMENT_ID
```

`open --focus` takes focus deliberately. GitHub PR links can also be opened through the manifest's Control-click link handler.

### Turn capture

The plugin subscribes to Herdr's `pane.agent_status_changed` event hooks, so tracking works while the UI is closed. A working transition captures the start, idle/done captures the end, and blocked → working resumes the same turn. Identities and state are isolated by worktree and originating agent/session.

Herdr events are asynchronous: they can arrive after an early edit, or miss a very short turn entirely. Installation cannot reconstruct a turn already in progress. For exact boundaries, an agent or hook can run these synchronously around its work:

```sh
/path/to/huicr/bin/huicr turn begin
# agent edits, tests, commits…
/path/to/huicr/bin/huicr turn end
```

Outside Herdr, these use a manual identity (`--manual` also forces it). Turn diffs include all edits made in that worktree during the captured interval, including concurrent human/agent edits.

## State and current boundaries

- State lives in `HERDR_PLUGIN_STATE_DIR/reviews.sqlite3`, normally `~/.local/state/herdr/plugins/claytonjschneider.huicr/reviews.sqlite3`. `huicr state-path` prints it; `HUICR_STATE_DIR` overrides it for tests/standalone use. Use `comments` for stable JSON/Markdown access instead of editing the database.
- SQLite transactions, cross-process locks, and immutable anchors protect comments across concurrent UI/CLI/event processes. Git trees are retained under `refs/huicr/snapshots/…` so garbage collection won't invalidate old comments. There is currently no automatic state/snapshot pruning.
- First version: bordered commit/file/diff panels, a dark blue-gray palette, lighter interactive boxes, soft blue selections, unified diffs, and horizontal scrolling. Keyboard navigation; no mouse UI or syntax highlighting yet. The navigator hides automatically below 65 columns; minimum terminal size is 38×10. Custom terminal palette entries are restored on exit.
- Binary files and submodules show Git metadata, not line-level contents. File comments still work. Files over `max_file_bytes` show a notice. Snapshots still capture tracked/non-ignored content, including large files, for correctness.
- An unmerged index must be resolved before index/worktree snapshot review. Shallow clones may need more history. Sparse/assume-unchanged paths follow Git's index behavior.
- GitHub's commit API has its own limit (currently 250); truncated lists are reported explicitly. Use a local Git range for larger PRs. A force-push during loading is detected; refresh to retry.

## Tests and CI

```sh
python3 -m unittest discover -s tests -v
ruff check --no-cache .
sh -n bin/huicr
```

[GitHub Actions](.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch: Python **3.11 and 3.14** on **Linux and macOS**, plus lint/manifest checks, a package-install smoke test, and **Gitleaks 8.30.1** over the full fetched Git history. The secret scan uses the official versioned CLI container and redacts findings.

Tests use temporary repositories, local Git remotes, and mocked GitHub/Herdr APIs; they need no credentials or running Herdr server. The terminal test uses a real pseudo-terminal to save a multiline comment, enter blame, reopen its saved anchor, and exit.

### Pre-commit secret scanning

[Lefthook](https://github.com/evilmartians/lefthook) manages the local Git hook; [Gitleaks](https://github.com/gitleaks/gitleaks) scans the staged diff. Set it up after cloning:

```sh
brew install lefthook gitleaks
lefthook install
lefthook run pre-commit
```

On Linux, install the same tools from their release packages/package manager, then run `lefthook install`. The committed [lefthook.yml](lefthook.yml) requires Lefthook 2.1+ and uses `gitleaks git --pre-commit --staged --redact --no-banner` (Gitleaks 8.19+, with 8.30.1 pinned in CI). The hook fails if Gitleaks finds a secret or cannot run. It scans staged contents even when the worktree version differs. With both tools installed, the test suite verifies the actual installed hook against a synthetic staged secret in a temporary repository.
