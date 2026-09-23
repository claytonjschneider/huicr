# huicr

A personal code-review pane for [Herdr](https://herdr.dev), pronounced like **weaker**.

Review changes commit-by-commit, toggle blame, and send persistent line/file comments back to your agent while you keep reviewing.

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
| `r` / `?` / `q` | Refresh / all shortcuts / close |

Branch, range, and PR reviews start commit-wise, using each commit's actual parent. Reviews stay frozen until you refresh, so the agent can keep editing without shifting your comment locations.

## Send feedback

1. Select a line, or use `v` to select a range.
2. Press `c` for a line comment or `C` for a file comment. The inline box grows as you type.
3. **Ctrl+S** saves, **Enter** adds a newline, **Esc** cancels.
4. **`s` pastes** unsent comments into the agent's input; **`S` submits** them immediately.

The review stays open. Comments survive closing and retain their original commit, file, and snippet. Use `L` to browse/edit/send individual comments, or `A` to choose an agent. Feedback is local—not posted to GitHub.

## More

- [Reference](docs/reference.md): CLI installation, configuration, full shortcuts, turn capture, state, and limitations.
- [Development](docs/reference.md#local-development): link a checkout with `herdr plugin link /path/to/huicr`.
- Tests: `python3 -m unittest discover -s tests -v`; lint: `ruff check --no-cache .`.
- Hooks: install Lefthook and Gitleaks, then run `lefthook install`. [CI](.github/workflows/ci.yml) checks Linux/macOS, Python 3.11/3.14, and secrets.

Inspired by [tuicr](https://tuicr.dev), [herdr-reviewr](https://github.com/persiyanov/herdr-reviewr), and [herdr-tuicr](https://github.com/ekropotin/herdr-tuicr).
