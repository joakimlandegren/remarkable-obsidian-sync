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
    assert config["obsidian_vault"].endswith("joakimlandegren")
    assert config["rmapi_bin"] == "rmapi"
    assert config["state_file"].endswith(".remarkable_sync_state.json")
    assert config["watch_path"] == "/"
    assert config["model"] == "claude-opus-4-6"  # default for Vertex compatibility


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


def _mock_rmapi_find_stat(notebooks_by_path: dict[str, dict]):
    """Create a mock for subprocess.run that handles rmapi find + stat.

    notebooks_by_path maps full path -> metadata dict with keys:
    ID, Name, Version, ModifiedClient.
    """
    def side_effect(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0

        if cmd[1] == "find":
            # Return "[f] /path" lines for each notebook
            lines = [f"[f] {path}" for path in notebooks_by_path]
            result.stdout = "\n".join(lines) + "\n" if lines else ""
            return result
        elif cmd[1] == "stat":
            path = cmd[2]
            meta = notebooks_by_path.get(path)
            if meta:
                result.stdout = json.dumps(meta)
            else:
                result.returncode = 1
            return result

        result.stdout = ""
        return result
    return side_effect


def test_list_notebooks_flat():
    """Lists notebooks in a flat directory."""
    notebooks = {
        "/Meeting Notes": {"ID": "abc-123", "Name": "Meeting Notes", "Version": 3, "ModifiedClient": "2025-01-15T10:30:00Z"},
        "/Sketches": {"ID": "def-456", "Name": "Sketches", "Version": 1, "ModifiedClient": "2025-01-10T08:00:00Z"},
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_find_stat(notebooks)):
        result = list_notebooks("rmapi", "/")

    assert len(result) == 2
    assert result[0]["id"] == "abc-123"
    assert result[0]["path"] == "/Meeting Notes"
    assert result[1]["path"] == "/Sketches"


def test_list_notebooks_recursive():
    """Lists notebooks in nested directories (find returns all recursively)."""
    notebooks = {
        "/Work/Project Plan": {"ID": "nb-1", "Name": "Project Plan", "Version": 2, "ModifiedClient": "2025-01-20T14:00:00Z"},
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_find_stat(notebooks)):
        result = list_notebooks("rmapi", "/")

    assert len(result) == 1
    assert result[0]["id"] == "nb-1"
    assert result[0]["path"] == "/Work/Project Plan"


def test_list_notebooks_watch_path_filter():
    """Only returns notebooks under the watch_path."""
    notebooks = {
        "/Work/Project Plan": {"ID": "nb-1", "Name": "Project Plan", "Version": 2, "ModifiedClient": "2025-01-20T14:00:00Z"},
        "/Personal/Diary": {"ID": "nb-2", "Name": "Diary", "Version": 1, "ModifiedClient": "2025-01-10T08:00:00Z"},
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_find_stat(notebooks)):
        result = list_notebooks("rmapi", "/Work")

    assert len(result) == 1
    assert result[0]["id"] == "nb-1"


def test_list_notebooks_skips_trash():
    """Skips notebooks in /trash/."""
    notebooks = {
        "/Meeting Notes": {"ID": "abc-123", "Name": "Meeting Notes", "Version": 3, "ModifiedClient": "2025-01-15T10:30:00Z"},
        "/trash/Deleted": {"ID": "del-1", "Name": "Deleted", "Version": 1, "ModifiedClient": "2025-01-01T00:00:00Z"},
    }
    with patch("subprocess.run", side_effect=_mock_rmapi_find_stat(notebooks)):
        result = list_notebooks("rmapi", "/")

    assert len(result) == 1
    assert result[0]["id"] == "abc-123"


def test_list_notebooks_auth_failure():
    """Raises SystemExit when rmapi fails (auth issue)."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="auth error", stdout="")
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


# --- Obsidian writer tests ---

from remarkable_to_obsidian import sanitize_filename, write_obsidian_note


def test_sanitize_filename():
    """Sanitizes notebook names for filesystem use."""
    assert sanitize_filename("Meeting Notes") == "Meeting Notes"
    assert sanitize_filename("Notes/2025") == "Notes_2025"
    assert sanitize_filename("hello@world!") == "hello_world_"


def test_write_obsidian_note(tmp_path):
    """Writes a markdown note with YAML frontmatter."""
    vault = tmp_path / "vault"
    notebook = {
        "id": "abc-123",
        "name": "Meeting Notes",
        "modified": "2025-01-15T10:30:00Z",
    }
    markdown = "# Meeting Notes\n\n- Discussed roadmap"

    path = write_obsidian_note(str(vault), notebook, markdown)

    assert path.exists()
    assert path.name == "Meeting Notes.md"
    content = path.read_text()
    assert content.startswith("---\n")
    assert 'title: "Meeting Notes"' in content
    assert 'remarkable_id: "abc-123"' in content
    assert "modified: 2025-01-15T10:30:00Z" in content
    assert "source: reMarkable" in content
    assert "- handwritten" in content
    assert "- inbox" in content
    assert content.endswith("- Discussed roadmap\n")
    assert "Remarkable Notes" in str(path.parent)


# --- Sync orchestration tests ---

from remarkable_to_obsidian import sync_notebooks


def test_sync_skips_unchanged(tmp_path, capsys):
    """Skips notebooks whose version matches state."""
    notebooks = [
        {"id": "abc-123", "name": "Old Notes", "version": 3, "modified": "2025-01-15T10:30:00Z", "path": "/Old Notes"},
    ]
    state = {"abc-123": {"version": 3, "pages": {}}}  # Same version — should skip

    with patch("remarkable_to_obsidian._extract_rm_pages") as mock_extract:
        sync_notebooks(notebooks, state, "/tmp/vault", "rmapi", "claude-opus-4-6", dry_run=False, client=MagicMock(), state_file="/tmp/state.json")
        mock_extract.assert_not_called()


def test_sync_skips_unchanged_legacy_state(tmp_path, capsys):
    """Handles legacy state format (plain version number) by re-processing."""
    notebooks = [
        {"id": "abc-123", "name": "Old Notes", "version": 3, "modified": "2025-01-15T10:30:00Z", "path": "/Old Notes"},
    ]
    state = {"abc-123": 3}  # Legacy format — should NOT skip (needs migration)

    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=[]) as mock_extract:
        sync_notebooks(notebooks, state, "/tmp/vault", "rmapi", "claude-opus-4-6", dry_run=False, client=MagicMock(), state_file="/tmp/state.json")
        mock_extract.assert_called_once()


def test_sync_processes_new_notebook(tmp_path):
    """Processes notebooks not in state (PDF-based notebook)."""
    notebooks = [
        {"id": "new-1", "name": "New Notes", "version": 1, "modified": "2025-01-20T09:00:00Z", "path": "/New Notes"},
    ]
    state = {}

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# New Notes\n\nContent here")]
    mock_client.messages.create.return_value = mock_response

    fake_pdf = tmp_path / "New Notes.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=None) as mock_extract, \
         patch("remarkable_to_obsidian.write_obsidian_note") as mock_write, \
         patch("remarkable_to_obsidian.save_state") as mock_save, \
         patch("tempfile.mkdtemp", return_value=str(tmp_path)):
        sync_notebooks(notebooks, state, str(tmp_path / "vault"), "rmapi", "claude-opus-4-6", dry_run=False, client=mock_client, state_file="/tmp/state.json")

    mock_extract.assert_called_once()
    mock_write.assert_called_once()
    assert state["new-1"] == {"version": 1}


def test_sync_incremental_pages(tmp_path):
    """Only re-transcribes pages whose content hash changed."""
    import hashlib

    # Create two fake .rm files
    rm_dir = tmp_path / "extracted" / "uuid"
    rm_dir.mkdir(parents=True)
    rm1 = rm_dir / "page1.rm"
    rm2 = rm_dir / "page2.rm"
    rm1.write_bytes(b"page1 content unchanged")
    rm2.write_bytes(b"page2 content NEW")

    hash1 = hashlib.sha256(b"page1 content unchanged").hexdigest()
    old_hash2 = hashlib.sha256(b"page2 content old").hexdigest()

    notebooks = [
        {"id": "nb-1", "name": "Test", "version": 2, "modified": "2025-01-20T09:00:00Z", "path": "/Test"},
    ]
    state = {
        "nb-1": {
            "version": 1,
            "pages": {
                "0": {"hash": hash1, "markdown": "# Page 1 cached"},
                "1": {"hash": old_hash2, "markdown": "# Page 2 old"},
            },
        },
    }

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Page 2 updated")]
    mock_client.messages.create.return_value = mock_response

    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=[rm1, rm2]), \
         patch("remarkable_to_obsidian._render_rm_to_svg", return_value='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1404 1872" width="1404" height="1872"><rect width="100%" height="100%" fill="white"/></svg>'), \
         patch("remarkable_to_obsidian.save_source_pages", return_value=[]), \
         patch("remarkable_to_obsidian.write_obsidian_note") as mock_write, \
         patch("remarkable_to_obsidian.save_state"), \
         patch("tempfile.mkdtemp", return_value=str(tmp_path)):
        sync_notebooks(notebooks, state, str(tmp_path / "vault"), "rmapi", "claude-opus-4-6", dry_run=False, client=mock_client, state_file="/tmp/state.json")

    # Only page 2 should have been sent to Claude
    assert mock_client.messages.create.call_count == 1
    mock_write.assert_called_once()

    # Verify the assembled markdown contains both pages
    written_md = mock_write.call_args.args[2]
    assert "# Page 1 cached" in written_md
    assert "# Page 2 updated" in written_md
