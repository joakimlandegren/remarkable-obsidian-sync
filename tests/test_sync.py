import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

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


# --- rmapi listing tests ---

from remarkable_to_obsidian import list_notebooks


def _mock_rmapi_ls(responses: dict):
    """Create a mock for subprocess.run that returns rmapi ls --json responses."""
    def side_effect(cmd, **kwargs):
        # Extract path from command: ["rmapi", "ls", "--json", path]
        path = cmd[3] if len(cmd) > 3 else "/"
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(responses.get(path, []))
        return result
    return side_effect


def test_list_notebooks_flat():
    """Lists notebooks in a flat directory."""
    responses = {
        "/": [
            {"id": "abc-123", "name": "Meeting Notes", "type": "DocumentType", "version": 3, "modifiedClient": "2025-01-15T10:30:00Z"},
            {"id": "def-456", "name": "Sketches", "type": "DocumentType", "version": 1, "modifiedClient": "2025-01-10T08:00:00Z"},
        ]
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_ls(responses)):
        notebooks = list_notebooks("rmapi", "/")

    assert len(notebooks) == 2
    assert notebooks[0]["id"] == "abc-123"
    assert notebooks[0]["path"] == "/Meeting Notes"
    assert notebooks[1]["path"] == "/Sketches"


def test_list_notebooks_recursive():
    """Recursively lists notebooks in nested directories."""
    responses = {
        "/": [
            {"id": "dir-1", "name": "Work", "type": "CollectionType", "version": 1, "modifiedClient": "2025-01-01T00:00:00Z"},
        ],
        "/Work": [
            {"id": "nb-1", "name": "Project Plan", "type": "DocumentType", "version": 2, "modifiedClient": "2025-01-20T14:00:00Z"},
        ],
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_ls(responses)):
        notebooks = list_notebooks("rmapi", "/")

    assert len(notebooks) == 1
    assert notebooks[0]["id"] == "nb-1"
    assert notebooks[0]["path"] == "/Work/Project Plan"


def test_list_notebooks_auth_failure():
    """Raises SystemExit when rmapi fails (auth issue)."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="auth error")
        with pytest.raises(SystemExit):
            list_notebooks("rmapi", "/")


# --- rmapi export tests ---

from remarkable_to_obsidian import export_notebook_pdf


def test_export_notebook_pdf(tmp_path):
    """Exports a notebook PDF to a temp directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    def mock_run(cmd, **kwargs):
        # rmapi geta writes a PDF named after the notebook into the -o dir
        out = cmd[cmd.index("-o") + 1] if "-o" in cmd else "."
        (Path(out) / "Meeting Notes.pdf").write_bytes(b"%PDF-1.4 fake content")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        pdf_path = export_notebook_pdf("rmapi", "/Meeting Notes", "Meeting Notes", output_dir)

    assert pdf_path.exists()
    assert pdf_path.name == "Meeting Notes.pdf"


def test_export_notebook_pdf_failure():
    """Returns None when rmapi geta fails."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="export error")
        with tempfile.TemporaryDirectory() as tmp:
            result = export_notebook_pdf("rmapi", "/Notebook", "Notebook", Path(tmp))
    assert result is None


# --- Transcription tests ---

from remarkable_to_obsidian import transcribe_pdf


def test_transcribe_pdf(tmp_path):
    """Sends PDF to Claude and returns markdown transcription."""
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Meeting Notes\n\n- Item 1\n- Item 2")]
    mock_client.messages.create.return_value = mock_response

    result = transcribe_pdf(mock_client, fake_pdf, "claude-opus-4-6")

    assert result == "# Meeting Notes\n\n- Item 1\n- Item 2"

    # Verify the API call structure
    call_args = mock_client.messages.create.call_args
    assert call_args.kwargs["model"] == "claude-opus-4-6"
    assert call_args.kwargs["max_tokens"] == 16384
    messages = call_args.kwargs["messages"]
    assert len(messages) == 1
    content = messages[0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
