"""Sync reMarkable handwritten notebooks to Obsidian via Claude vision."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def load_config() -> dict:
    """Load configuration from environment variables with defaults."""
    home = Path.home()
    return {
        "obsidian_vault": os.environ.get("OBSIDIAN_VAULT", str(home / "obsidian-vault")),
        "rmapi_bin": os.environ.get("RMAPI_BIN", "rmapi"),
        "state_file": os.environ.get("RM_STATE_FILE", str(home / ".remarkable_sync_state.json")),
        "watch_path": os.environ.get("RM_WATCH_PATH", "/"),
        "model": os.environ.get("RM_MODEL", "claude-opus-4-6"),
    }


def load_state(state_file: str) -> dict:
    """Load notebook sync state. Returns {notebook_id: version} mapping."""
    path = Path(state_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data.get("notebooks", {})
    except (json.JSONDecodeError, KeyError):
        log.warning("Corrupt state file %s, starting fresh", state_file)
        return {}


def save_state(state_file: str, notebooks: dict) -> None:
    """Save notebook sync state to JSON file."""
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notebooks": notebooks}, indent=2))
