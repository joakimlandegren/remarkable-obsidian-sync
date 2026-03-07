import json
import os
import pytest


def test_config_defaults():
    """Config uses sensible defaults when env vars are not set."""
    # Clear any env vars that might be set
    for var in ["OBSIDIAN_VAULT", "RMAPI_BIN", "RM_STATE_FILE", "RM_WATCH_PATH", "RM_MODEL"]:
        os.environ.pop(var, None)

    from remarkable_to_obsidian import load_config

    config = load_config()
    assert config["obsidian_vault"].endswith("obsidian-vault")
    assert config["rmapi_bin"] == "rmapi"
    assert config["state_file"].endswith(".remarkable_sync_state.json")
    assert config["watch_path"] == "/"
    assert config["model"] == "claude-opus-4-6"


def test_config_from_env(monkeypatch):
    """Config reads from environment variables."""
    monkeypatch.setenv("OBSIDIAN_VAULT", "/tmp/test-vault")
    monkeypatch.setenv("RMAPI_BIN", "/usr/local/bin/rmapi")
    monkeypatch.setenv("RM_STATE_FILE", "/tmp/state.json")
    monkeypatch.setenv("RM_WATCH_PATH", "/Notes")
    monkeypatch.setenv("RM_MODEL", "claude-sonnet-4-20250514")

    from remarkable_to_obsidian import load_config

    config = load_config()
    assert config["obsidian_vault"] == "/tmp/test-vault"
    assert config["rmapi_bin"] == "/usr/local/bin/rmapi"
    assert config["state_file"] == "/tmp/state.json"
    assert config["watch_path"] == "/Notes"
    assert config["model"] == "claude-sonnet-4-20250514"


# --- State management tests ---

from remarkable_to_obsidian import load_state, save_state


def test_load_state_missing_file(tmp_path):
    """Returns empty dict when state file doesn't exist."""
    state = load_state(str(tmp_path / "nonexistent.json"))
    assert state == {}


def test_load_state_existing_file(tmp_path):
    """Loads state from existing JSON file."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"notebooks": {"abc-123": 5}}))
    state = load_state(str(state_file))
    assert state == {"abc-123": 5}


def test_save_state(tmp_path):
    """Saves state to JSON file, creating parent dirs."""
    state_file = tmp_path / "sub" / "state.json"
    save_state(str(state_file), {"abc-123": 5})
    data = json.loads(state_file.read_text())
    assert data == {"notebooks": {"abc-123": 5}}


def test_load_state_corrupt_file(tmp_path):
    """Returns empty dict when state file is corrupt."""
    state_file = tmp_path / "state.json"
    state_file.write_text("not json{{{")
    state = load_state(str(state_file))
    assert state == {}
