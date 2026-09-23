from dataclasses import dataclass
from pathlib import Path
import os
import tomllib

from . import PLUGIN_ID


class HuicrError(Exception):
    """An actionable error suitable for displaying in the review pane."""


def state_dir() -> Path:
    override = os.environ.get("HUICR_STATE_DIR") or os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "herdr/plugins" / PLUGIN_ID


def config_path() -> Path:
    override = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if override:
        return Path(override) / "config.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "herdr/plugins/config" / PLUGIN_ID / "config.toml"


@dataclass
class Config:
    placement: str = "split"
    direction: str = "right"
    default_scope: str = "unstaged"
    context_lines: int = 5
    max_file_bytes: int = 2_000_000
    track_turns: bool = True

    @classmethod
    def load(cls):
        path = config_path()
        if not path.exists():
            return cls()
        try:
            with path.open("rb") as f:
                values = tomllib.load(f)
            unknown = values.keys() - cls.__dataclass_fields__.keys()
            if unknown:
                raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
            config = cls(**values)
            for key, choices in {
                "placement": ("split", "zoomed", "tab"),
                "direction": ("right", "down"),
                "default_scope": ("unstaged", "worktree", "branch", "turn"),
            }.items():
                if getattr(config, key) not in choices:
                    raise ValueError(f"{key} must be one of {', '.join(choices)}")
            if type(config.context_lines) is not int or not 0 <= config.context_lines <= 100:
                raise ValueError("context_lines must be an integer from 0 to 100")
            if type(config.max_file_bytes) is not int or config.max_file_bytes < 1024:
                raise ValueError("max_file_bytes must be an integer >= 1024")
            if type(config.track_turns) is not bool:
                raise ValueError("track_turns must be a boolean")
            return config
        except (ValueError, TypeError, OSError) as e:
            raise HuicrError(f"{path}: {e}") from e
