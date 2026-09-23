# huicr

A personal code-review pane for [Herdr](https://herdr.dev), pronounced like **weaker**.

Review changes commit-by-commit, toggle blame, and send persistent line/file comments to your agent or publish a GitHub PR review.

## Install

Requires **Herdr 0.9.1+**, **Python 3.11+**, and **Git** on macOS or Linux. GitHub PR reviews also need the `gh` CLI and reuse its existing login or token; see [GitHub authentication](docs/reference.md#github-authentication).

```sh
herdr plugin install claytonjschneider/huicr
herdr plugin action invoke open --plugin claytonjschneider.huicr
```

For a hotkey, add this to your Herdr config and run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+o"
type = "plugin_action"
command = "claytonjschneider.huicr.toggle"
description = "huicr: toggle review"
```

Prefer a dedicated tab? Set `placement = "tab"` in the plugin's `config.toml`. Find its directory with `herdr plugin config-dir claytonjschneider.huicr`; see the [example config](config.toml.example).

## Review

| Key | Action |
| --- | --- |
| `u` / `U` / `i` | Unstaged + untracked / all uncommitted / staged |
| `b` / `t` | Branch / last agent turn |
| `o` | Open a branch, commit, range, or GitHub PR URL |
| `B` | Choose the comparator branch or revision |
| `,` / `.` / `m` | Previous commit / next commit / whole-range diff |
| `j` / `k`, `Tab` | Move / switch focus |
| `a` | Toggle blame |
| `w` | Toggle text wrapping (on by default) |
| `r` / `?` / `q` | Refresh / all shortcuts / close |

Branch, range, and PR reviews start commit-wise, using each commit's actual parent. Reviews stay frozen until you refresh, so the agent can keep editing without shifting your comment locations.

## Send feedback

1. Select a line, or use `v` to select a range.
2. Press `c` for a line comment or `C` for a file comment. The inline box grows as you type.
3. **Enter** saves and finishes, **Shift+Enter** adds a newline, **Esc** cancels. **Ctrl+S** also saves.
4. **`s` pastes** unsent comments into the agent's input; **`S` submits** them immediately.

The review stays open. Comments survive closing and retain their original commit, file, and snippet. Use `L` to browse/edit/send individual comments, or `A` to choose an agent.

Text boxes share **Option+Backspace / Ctrl+W** to delete a word and **Ctrl+U / Ctrl+K** to delete to the start/end of a line. See [text entry](docs/reference.md#text-entry) for all shortcuts.

## Post to GitHub

Open a PR with `o` and add comments as you step through commits with `,` / `.` or view the whole diff with `m`. **`P` publishes** unpublished PR drafts as a GitHub review; `L` → `P` publishes just the selected comment. Unchanged line/range locations are mapped to inline comments. File comments and historical lines that no longer map appear in the review summary with their original context.

GitHub and agent delivery have separate statuses. Posting reuses your `gh` credentials with permission to write PR reviews. See [publication details and recovery](docs/reference.md#publishing-github-reviews).

## More

- [Reference](docs/reference.md): CLI installation, configuration, full shortcuts, turn capture, state, and limitations.
- [Development](docs/reference.md#local-development): link a checkout with `herdr plugin link /path/to/huicr`.
- Tests: `python3 -m unittest discover -s tests -v`; lint: `ruff check --no-cache .`.
- Hooks: install Lefthook and Gitleaks, then run `lefthook install`. [CI](.github/workflows/ci.yml) checks Linux/macOS, Python 3.11/3.14, and secrets.

Inspired by [tuicr](https://tuicr.dev), [herdr-reviewr](https://github.com/persiyanov/herdr-reviewr), and [herdr-tuicr](https://github.com/ekropotin/herdr-tuicr).
