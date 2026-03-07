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
