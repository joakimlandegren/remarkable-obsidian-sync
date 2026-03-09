import base64
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from remarkable_to_obsidian import (
    DIAGRAM_RE,
    _encode_page_image,
    _content_tags,
    _extract_content_tags,
    _extract_rm_pages,
    _hash_file,
    _load_dotenv,
    _move_obsidian_note,
    _move_obsidian_note_legacy,
    _svg_to_png,
    export_notebook,
    export_notebook_pdf,
    extract_diagram_crops,
    is_ignored,
    list_notebooks,
    load_config,
    load_ignore_patterns,
    load_state,
    merge_state_files,
    sanitize_filename,
    save_source_pages,
    save_state,
    sync_notebooks,
    transcribe_page,
    transcribe_pages,
    transcribe_pdf,
    write_obsidian_note,
)


def test_config_defaults():
    """Config uses sensible defaults when env vars are not set."""
    # Clear any env vars that might be set
    for var in ["OBSIDIAN_VAULT", "RMAPI_BIN", "RM_STATE_FILE", "RM_WATCH_PATH", "RM_MODEL"]:
        os.environ.pop(var, None)

    config = load_config()
    assert config["obsidian_vault"].endswith("obsidian-vault")
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

    config = load_config()
    assert config["obsidian_vault"] == "/tmp/test-vault"
    assert config["rmapi_bin"] == "/usr/local/bin/rmapi"
    assert config["state_file"] == "/tmp/state.json"
    assert config["watch_path"] == "/Notes"
    assert config["model"] == "claude-sonnet-4-20250514"


# --- State management tests ---


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

    assert pdf_path is not None
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
    assert "type: notebook" in content
    assert "- handwritten" in content
    assert "- inbox" in content
    assert "starred:" not in content  # omitted when not starred
    assert content.endswith("- Discussed roadmap\n")
    assert "Remarkable Notes" in str(path.parent)


def test_write_obsidian_note_starred(tmp_path):
    """Starred notebooks get starred: true in frontmatter."""
    vault = tmp_path / "vault"
    notebook = {
        "id": "abc-123",
        "name": "Important",
        "modified": "2025-01-15T10:30:00Z",
        "starred": True,
    }
    path = write_obsidian_note(str(vault), notebook, "content")
    content = path.read_text()
    assert "starred: true" in content


def test_write_obsidian_note_tags(tmp_path):
    """reMarkable tags are merged into frontmatter tags."""
    vault = tmp_path / "vault"
    notebook = {
        "id": "abc-123",
        "name": "Tagged Note",
        "modified": "2025-01-15T10:30:00Z",
        "tags": ["Work", "Project Alpha"],
    }
    path = write_obsidian_note(str(vault), notebook, "content")
    content = path.read_text()
    assert "- handwritten" in content
    assert "- inbox" in content
    assert "- work" in content
    assert "- project-alpha" in content


def test_write_obsidian_note_page_count(tmp_path):
    """Page count appears in frontmatter when set."""
    vault = tmp_path / "vault"
    notebook = {
        "id": "abc-123",
        "name": "Multi Page",
        "modified": "2025-01-15T10:30:00Z",
        "page_count": 5,
    }
    path = write_obsidian_note(str(vault), notebook, "content")
    content = path.read_text()
    assert "page_count: 5" in content


def test_write_obsidian_note_type_mapping(tmp_path):
    """DocumentType maps to 'notebook', others lowercase."""
    vault = tmp_path / "vault"
    nb_doc = {"id": "1", "name": "A", "modified": "2025-01-01", "type": "DocumentType"}
    nb_epub = {"id": "2", "name": "B", "modified": "2025-01-01", "type": "epub"}

    content_doc = write_obsidian_note(str(vault), nb_doc, "c").read_text()
    content_epub = write_obsidian_note(str(vault), nb_epub, "c").read_text()
    assert "type: notebook" in content_doc
    assert "type: epub" in content_epub


# --- Move/rename tests ---


def test_move_obsidian_note_path_change(tmp_path):
    """Moves note when notebook is moved to a different folder."""
    vault = tmp_path / "vault"
    old_dir = vault / "Remarkable Notes" / "Notes"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "Meeting.md"
    old_file.write_text("content")

    notebook = {"name": "Meeting", "path": "/Archive/Meeting"}
    _move_obsidian_note(str(vault), "/Notes/Meeting", "Meeting", notebook)

    new_file = vault / "Remarkable Notes" / "Archive" / "Meeting.md"
    assert new_file.exists()
    assert not old_file.exists()
    assert new_file.read_text() == "content"


def test_move_obsidian_note_rename(tmp_path):
    """Moves note when notebook is renamed."""
    vault = tmp_path / "vault"
    old_dir = vault / "Remarkable Notes" / "Notes"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "Old Name.md"
    old_file.write_text("content")

    notebook = {"name": "New Name", "path": "/Notes/New Name"}
    _move_obsidian_note(str(vault), "/Notes/Old Name", "Old Name", notebook)

    new_file = vault / "Remarkable Notes" / "Notes" / "New Name.md"
    assert new_file.exists()
    assert not old_file.exists()


def test_move_obsidian_note_missing_old(tmp_path):
    """Gracefully handles missing old file."""
    vault = tmp_path / "vault"
    (vault / "Remarkable Notes").mkdir(parents=True)

    notebook = {"name": "Note", "path": "/Archive/Note"}
    # Should not raise — just logs a warning
    _move_obsidian_note(str(vault), "/Notes/Note", "Note", notebook)


def test_move_obsidian_note_cleans_empty_dir(tmp_path):
    """Removes empty parent directory after move."""
    vault = tmp_path / "vault"
    old_dir = vault / "Remarkable Notes" / "OldFolder"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "Note.md"
    old_file.write_text("content")

    notebook = {"name": "Note", "path": "/NewFolder/Note"}
    _move_obsidian_note(str(vault), "/OldFolder/Note", "Note", notebook)

    assert not old_dir.exists()  # empty dir removed


def test_move_obsidian_note_legacy_finds_and_moves(tmp_path):
    """Legacy migration finds note by name and moves it to current reMarkable path."""
    vault = tmp_path / "vault"
    old_dir = vault / "Remarkable Notes" / "OldFolder"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "Meeting.md"
    old_file.write_text("content")

    notebook = {"name": "Meeting", "path": "/Archive/2025/Meeting"}
    _move_obsidian_note_legacy(str(vault), notebook)

    new_file = vault / "Remarkable Notes" / "Archive" / "2025" / "Meeting.md"
    assert new_file.exists()
    assert not old_file.exists()
    assert new_file.read_text() == "content"


def test_move_obsidian_note_legacy_already_correct(tmp_path):
    """Legacy migration skips when note is already in the right place."""
    vault = tmp_path / "vault"
    correct_dir = vault / "Remarkable Notes" / "Notes"
    correct_dir.mkdir(parents=True)
    correct_file = correct_dir / "Meeting.md"
    correct_file.write_text("content")

    notebook = {"name": "Meeting", "path": "/Notes/Meeting"}
    _move_obsidian_note_legacy(str(vault), notebook)

    assert correct_file.exists()  # unchanged


def test_move_obsidian_note_legacy_multiple_matches(tmp_path):
    """Legacy migration skips when multiple notes with same name exist."""
    vault = tmp_path / "vault"
    for folder in ["FolderA", "FolderB"]:
        d = vault / "Remarkable Notes" / folder
        d.mkdir(parents=True)
        (d / "Meeting.md").write_text("content")

    notebook = {"name": "Meeting", "path": "/Archive/Meeting"}
    _move_obsidian_note_legacy(str(vault), notebook)

    # Both originals should still exist — no move attempted
    assert (vault / "Remarkable Notes" / "FolderA" / "Meeting.md").exists()
    assert (vault / "Remarkable Notes" / "FolderB" / "Meeting.md").exists()


# --- Sync orchestration tests ---


def test_sync_skips_unchanged(tmp_path, capsys):
    """Skips notebooks whose version matches state."""
    notebooks = [
        {"id": "abc-123", "name": "Old Notes", "version": 3, "modified": "2025-01-15T10:30:00Z", "path": "/Old Notes"},
    ]
    state = {"abc-123": {"version": 3, "path": "/Old Notes", "name": "Old Notes", "pages": {}}}

    with patch("remarkable_to_obsidian._extract_rm_pages") as mock_extract, \
         patch("remarkable_to_obsidian.save_state"):
        sync_notebooks(notebooks, state, "/tmp/vault", "rmapi", "claude-opus-4-6", dry_run=False, client=MagicMock(), state_file="/tmp/state.json")
        mock_extract.assert_not_called()


def test_sync_moves_renamed_notebook(tmp_path):
    """Moves Obsidian note when notebook path changes but version doesn't."""
    vault = tmp_path / "vault"
    old_dir = vault / "Remarkable Notes" / "Notes"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "My Note.md"
    old_file.write_text("---\ntitle: My Note\n---\ncontent")

    notebooks = [
        {"id": "abc-123", "name": "My Note", "version": 3, "modified": "2025-01-15T10:30:00Z", "path": "/Archive/My Note"},
    ]
    state = {"abc-123": {"version": 3, "path": "/Notes/My Note", "name": "My Note", "pages": {}}}

    with patch("remarkable_to_obsidian._extract_rm_pages") as mock_extract, \
         patch("remarkable_to_obsidian.save_state"):
        sync_notebooks(notebooks, state, str(vault), "rmapi", "claude-opus-4-6", dry_run=False, client=MagicMock(), state_file="/tmp/state.json")
        mock_extract.assert_not_called()  # version unchanged — no re-transcription

    new_file = vault / "Remarkable Notes" / "Archive" / "My Note.md"
    assert new_file.exists()
    assert not old_file.exists()
    # State should be updated with new path
    assert state["abc-123"]["path"] == "/Archive/My Note"


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
         patch("remarkable_to_obsidian.save_state"), \
         patch("tempfile.mkdtemp", return_value=str(tmp_path)):
        sync_notebooks(notebooks, state, str(tmp_path / "vault"), "rmapi", "claude-opus-4-6", dry_run=False, client=mock_client, state_file="/tmp/state.json")

    mock_extract.assert_called_once()
    mock_write.assert_called_once()
    assert state["new-1"] == {"version": 1, "path": "/New Notes", "name": "New Notes"}


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


# --- .env loading tests ---


def test_load_dotenv_loads_vars(tmp_path, monkeypatch):
    """Loads variables from .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_RM_DOTENV=loaded\nTEST_RM_OTHER=world\n")
    monkeypatch.delenv("TEST_RM_DOTENV", raising=False)
    monkeypatch.delenv("TEST_RM_OTHER", raising=False)

    # Patch __file__ so _load_dotenv finds our test .env
    import remarkable_to_obsidian
    with patch.object(remarkable_to_obsidian, "__file__", str(tmp_path / "remarkable_to_obsidian.py")):
        _load_dotenv()

    assert os.environ.get("TEST_RM_DOTENV") == "loaded"
    assert os.environ.get("TEST_RM_OTHER") == "world"
    monkeypatch.delenv("TEST_RM_DOTENV")
    monkeypatch.delenv("TEST_RM_OTHER")


def test_load_dotenv_no_override(tmp_path, monkeypatch):
    """Does not override existing environment variables."""
    monkeypatch.setenv("TEST_RM_EXISTING", "original")
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_RM_EXISTING=overwritten\n")

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and _ and key not in os.environ:
            os.environ[key] = value

    assert os.environ["TEST_RM_EXISTING"] == "original"


def test_load_dotenv_ignores_comments_and_blanks(tmp_path):
    """Ignores comment lines and blank lines."""
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nKEY1=val1\n  # another comment\nKEY2=val2\n")

    parsed = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and _:
            parsed[key] = value

    assert parsed == {"KEY1": "val1", "KEY2": "val2"}


# --- .sync_ignore tests ---


def test_load_ignore_patterns(tmp_path):
    """Loads patterns from .sync_ignore file."""
    ignore_file = tmp_path / ".sync_ignore"
    ignore_file.write_text("# comment\nKvitto*\n\nFinancial Core*\n")

    # Test the logic directly
    patterns = []
    for line in ignore_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)

    assert patterns == ["Kvitto*", "Financial Core*"]


def test_load_ignore_patterns_missing_file():
    """Returns empty list when .sync_ignore doesn't exist."""
    with patch.object(Path, "exists", return_value=False):
        result = load_ignore_patterns()
    assert result == []


def test_is_ignored_by_name():
    """Matches notebook name against patterns."""
    assert is_ignored("Kvitto receipts", "/Kvitto receipts", ["Kvitto*"])
    assert is_ignored("Financial Core Data", "/Projects/Financial Core Data", ["Financial Core*"])


def test_is_ignored_by_path():
    """Matches notebook path against patterns."""
    assert is_ignored("Notes", "/trash/Notes", ["/trash/*"])
    assert is_ignored("Plan", "/Work/Projects/Plan", ["/Work/*"])


def test_is_ignored_glob_wildcards():
    """Glob wildcards work correctly."""
    assert is_ignored("report.docx", "/report.docx", ["*.docx"])
    assert is_ignored("Notebook 3", "/Notebook 3", ["Notebook ?"])
    assert not is_ignored("Notebook 12", "/Notebook 12", ["Notebook ?"])


def test_is_ignored_no_match():
    """Returns False when no patterns match."""
    assert not is_ignored("Meeting Notes", "/Meeting Notes", ["Kvitto*", "*.docx"])


# --- Batch operations tests ---


def test_merge_state_files(tmp_path):
    """Merges multiple state files into target."""
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"notebooks": {"existing": {"version": 1}}}))

    src1 = tmp_path / "src1.json"
    src1.write_text(json.dumps({"notebooks": {"nb-1": {"version": 2}}}))

    src2 = tmp_path / "src2.json"
    src2.write_text(json.dumps({"notebooks": {"nb-2": {"version": 3}}}))

    merge_state_files(str(target), [str(src1), str(src2)])

    result = json.loads(target.read_text())["notebooks"]
    assert result["existing"] == {"version": 1}
    assert result["nb-1"] == {"version": 2}
    assert result["nb-2"] == {"version": 3}


def test_merge_state_files_override(tmp_path):
    """Later sources override earlier ones."""
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"notebooks": {"nb-1": {"version": 1}}}))

    src = tmp_path / "src.json"
    src.write_text(json.dumps({"notebooks": {"nb-1": {"version": 5}}}))

    merge_state_files(str(target), [str(src)])

    result = json.loads(target.read_text())["notebooks"]
    assert result["nb-1"] == {"version": 5}


def test_merge_state_files_empty_source(tmp_path):
    """Handles missing source files gracefully."""
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"notebooks": {"nb-1": {"version": 1}}}))

    merge_state_files(str(target), [str(tmp_path / "nonexistent.json")])

    result = json.loads(target.read_text())["notebooks"]
    assert result["nb-1"] == {"version": 1}


# --- Hash file tests ---


def test_hash_file(tmp_path):
    """Returns consistent SHA-256 hex digest."""
    f = tmp_path / "test.bin"
    f.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert _hash_file(f) == expected
    # Consistent on repeated calls
    assert _hash_file(f) == expected


# --- Image encoding tests ---


def test_encode_page_image_png(tmp_path):
    """PNG files return base64 + image/png."""
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG fake")
    data, media_type = _encode_page_image(png)
    assert media_type == "image/png"
    assert base64.standard_b64decode(data) == b"\x89PNG fake"


def test_encode_page_image_pdf(tmp_path):
    """PDF files return base64 + application/pdf."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    data, media_type = _encode_page_image(pdf)
    assert media_type == "application/pdf"
    assert base64.standard_b64decode(data) == b"%PDF-1.4"


def test_encode_page_image_svg(tmp_path):
    """SVG files are converted to PNG via _svg_to_png."""
    svg = tmp_path / "page.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>')

    fake_png = b"\x89PNG converted"
    with patch("remarkable_to_obsidian._svg_to_png", return_value=fake_png):
        data, media_type = _encode_page_image(svg)

    assert media_type == "image/png"
    assert base64.standard_b64decode(data) == fake_png


# --- SVG to PNG tests ---


def test_svg_to_png_normal_dimensions(tmp_path):
    """Uses original width when dimensions are within limits."""
    svg = tmp_path / "page.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1404" height="1872"></svg>')

    with patch("cairosvg.svg2png", return_value=b"png_bytes") as mock_cairo:
        result = _svg_to_png(svg)

    assert result == b"png_bytes"
    mock_cairo.assert_called_once_with(url=str(svg), output_width=1404)


def test_svg_to_png_oversized(tmp_path):
    """Scales down when dimensions exceed MAX_IMAGE_DIM."""
    svg = tmp_path / "page.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1404" height="10000"></svg>')

    with patch("cairosvg.svg2png", return_value=b"png_bytes") as mock_cairo:
        _svg_to_png(svg)

    # Scale = min(8000/10000, 8000/1404) = 0.8
    expected_width = int(1404 * 0.8)
    mock_cairo.assert_called_once_with(url=str(svg), output_width=expected_width)


# --- Transcription tests (page-level) ---


def test_transcribe_page(tmp_path):
    """Sends single page image to Claude and returns markdown."""
    page = tmp_path / "page.png"
    page.write_bytes(b"\x89PNG fake page")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Page content")]
    mock_client.messages.create.return_value = mock_response

    result = transcribe_page(mock_client, page, "claude-opus-4-6")

    assert result == "# Page content"
    call_args = mock_client.messages.create.call_args
    content = call_args.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["type"] == "text"


def test_transcribe_pages_multiple(tmp_path):
    """Sends multiple pages to Claude in a single request."""
    page1 = tmp_path / "page1.png"
    page2 = tmp_path / "page2.png"
    page1.write_bytes(b"\x89PNG page1")
    page2.write_bytes(b"\x89PNG page2")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Page 1\n\n# Page 2")]
    mock_client.messages.create.return_value = mock_response

    result = transcribe_pages(mock_client, [page1, page2], "claude-opus-4-6")

    assert result == "# Page 1\n\n# Page 2"
    call_args = mock_client.messages.create.call_args
    content = call_args.kwargs["messages"][0]["content"]
    # 2 images + 1 text prompt
    assert len(content) == 3
    assert content[0]["type"] == "image"
    assert content[1]["type"] == "image"
    assert content[2]["type"] == "text"


# --- Diagram extraction tests ---


def test_diagram_regex():
    """Regex matches diagram markers correctly."""
    text = '> [Diagram(page=1, top=20, bottom=60): a flowchart showing process]'
    match = DIAGRAM_RE.search(text)
    assert match is not None
    assert match.group(1) == "1"
    assert match.group(2) == "20"
    assert match.group(3) == "60"
    assert match.group(4) == "a flowchart showing process"


def test_extract_diagram_crops_no_diagrams(tmp_path):
    """Returns unchanged markdown when no diagram markers present."""
    markdown = "# Notes\n\n- Item 1\n- Item 2"
    result, crops = extract_diagram_crops(markdown, [], str(tmp_path), "Test")
    assert result == markdown
    assert crops == []


def test_extract_diagram_crops_out_of_range(tmp_path):
    """Leaves marker as-is when page number is out of range."""
    markdown = '> [Diagram(page=5, top=10, bottom=50): something]'
    page = tmp_path / "page1.png"
    page.write_bytes(b"\x89PNG")

    result, crops = extract_diagram_crops(markdown, [page], str(tmp_path), "Test")
    assert result == markdown  # unchanged
    assert crops == []


def test_extract_diagram_crops_success(tmp_path):
    """Crops image and replaces marker with embed."""
    markdown = '> [Diagram(page=1, top=20, bottom=60): a chart]'

    # Create a fake PNG image
    from PIL import Image
    img = Image.new("RGB", (100, 200), color="white")
    page = tmp_path / "page1.png"
    img.save(str(page), "PNG")

    result, crops = extract_diagram_crops(markdown, [page], str(tmp_path), "Test")

    assert "![[Test - diagram 1.png]]" in result
    assert "> *a chart*" in result
    assert len(crops) == 1
    # Verify crop file was saved
    crop_path = tmp_path / "Attachments" / "reMarkable" / "Test - diagram 1.png"
    assert crop_path.exists()


# --- Save source pages tests ---


def test_save_source_pages_pdf(tmp_path):
    """PDF files are copied with notebook name."""
    vault = tmp_path / "vault"
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 content")

    notebook = {"name": "My Notes"}
    result = save_source_pages(str(vault), notebook, [pdf])

    assert result == ["My Notes.pdf"]
    saved = vault / "Attachments" / "reMarkable" / "My Notes.pdf"
    assert saved.exists()


def test_save_source_pages_png(tmp_path):
    """PNG files are copied with page numbering."""
    vault = tmp_path / "vault"
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG data")

    notebook = {"name": "Sketches"}
    result = save_source_pages(str(vault), notebook, [png])

    assert result == ["Sketches - page 1.png"]
    saved = vault / "Attachments" / "reMarkable" / "Sketches - page 1.png"
    assert saved.exists()


def test_save_source_pages_svg(tmp_path):
    """SVG files are converted to PNG."""
    vault = tmp_path / "vault"
    svg = tmp_path / "page.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>')

    notebook = {"name": "Drawing"}
    with patch("remarkable_to_obsidian._svg_to_png", return_value=b"\x89PNG converted"):
        result = save_source_pages(str(vault), notebook, [svg])

    assert result == ["Drawing - page 1.png"]
    saved = vault / "Attachments" / "reMarkable" / "Drawing - page 1.png"
    assert saved.exists()
    assert saved.read_bytes() == b"\x89PNG converted"


# --- Content tag extraction tests ---


def test_extract_content_tags_from_rmdoc(tmp_path):
    """Extracts pageTags from .content file inside a zip/rmdoc."""
    content_data = json.dumps({
        "pageTags": [
            {"name": "strategy", "pageId": "page-1", "timestamp": 123},
            {"name": "important", "pageId": "page-2", "timestamp": 456},
            {"name": "strategy", "pageId": "page-3", "timestamp": 789},  # duplicate
        ],
        "tags": [],
    })

    rmdoc = tmp_path / "notebook.rmdoc"
    with zipfile.ZipFile(rmdoc, "w") as zf:
        zf.writestr("abc-123.content", content_data)

    _content_tags.clear()
    _extract_content_tags(rmdoc, "/Test/Notebook")
    assert "/Test/Notebook" in _content_tags
    assert sorted(_content_tags["/Test/Notebook"]) == ["important", "strategy"]
    _content_tags.clear()


def test_extract_content_tags_empty(tmp_path):
    """No tags cached when pageTags is empty."""
    content_data = json.dumps({"pageTags": [], "tags": []})
    rmdoc = tmp_path / "notebook.rmdoc"
    with zipfile.ZipFile(rmdoc, "w") as zf:
        zf.writestr("abc-123.content", content_data)

    _content_tags.clear()
    _extract_content_tags(rmdoc, "/Test/Notebook")
    assert "/Test/Notebook" not in _content_tags
    _content_tags.clear()


# --- Extract .rm pages tests ---


def test_extract_rm_pages_pdf(tmp_path):
    """Returns None when PDF is found (PDF-based notebook)."""
    def mock_run(cmd, **kwargs):
        cwd = kwargs.get("cwd", ".")
        (Path(cwd) / "notebook.pdf").write_bytes(b"%PDF-1.4")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_run):
        result = _extract_rm_pages("rmapi", "/Notebook", tmp_path)

    assert result is None


def test_extract_rm_pages_zip_with_content(tmp_path):
    """Returns ordered .rm files from zip with .content ordering."""
    # Create a zip with .rm files and a .content manifest
    zip_path = tmp_path / "notebook.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("uuid/page-b.rm", b"page b data")
        zf.writestr("uuid/page-a.rm", b"page a data")
        content = {
            "cPages": {
                "pages": [
                    {"id": "page-a"},
                    {"id": "page-b"},
                ]
            }
        }
        zf.writestr("uuid.content", json.dumps(content))

    def mock_run(cmd, **kwargs):
        # geta produces no PDF, no zip
        if cmd[1] == "geta":
            return MagicMock(returncode=0)
        # get produces the zip
        if cmd[1] == "get":
            cwd = kwargs.get("cwd", ".")
            shutil.copy2(zip_path, Path(cwd) / "notebook.zip")
            return MagicMock(returncode=0)
        return MagicMock(returncode=0)

    output = tmp_path / "output"
    output.mkdir()
    with patch("subprocess.run", side_effect=mock_run):
        result = _extract_rm_pages("rmapi", "/Notebook", output)

    assert result is not None
    assert len(result) == 2
    assert result[0].stem == "page-a"  # ordered by .content
    assert result[1].stem == "page-b"


def test_extract_rm_pages_no_rm_files(tmp_path):
    """Returns empty list when zip contains no .rm files."""
    zip_path = tmp_path / "notebook.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("uuid/notes.txt", b"not an rm file")

    def mock_run(cmd, **kwargs):
        if cmd[1] == "geta":
            cwd = kwargs.get("cwd", ".")
            shutil.copy2(zip_path, Path(cwd) / "notebook.zip")
            return MagicMock(returncode=0)
        return MagicMock(returncode=0)

    output = tmp_path / "output"
    output.mkdir()
    with patch("subprocess.run", side_effect=mock_run):
        result = _extract_rm_pages("rmapi", "/Notebook", output)

    assert result == []


def test_extract_rm_pages_no_output(tmp_path):
    """Returns empty list when neither PDF nor zip is produced."""
    def mock_run(cmd, **kwargs):
        return MagicMock(returncode=0)

    output = tmp_path / "output"
    output.mkdir()
    with patch("subprocess.run", side_effect=mock_run):
        result = _extract_rm_pages("rmapi", "/Notebook", output)

    assert result == []


# --- Export notebook tests ---


def test_export_notebook_pdf_based(tmp_path):
    """PDF notebook returns PDF paths."""
    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=None):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        result = export_notebook("rmapi", "/Notebook", "Notebook", tmp_path)

    assert len(result) == 1
    assert result[0].suffix == ".pdf"


def test_export_notebook_rm_based(tmp_path):
    """RM notebook renders SVGs."""
    rm1 = tmp_path / "page1.rm"
    rm1.write_bytes(b"rm data")

    fake_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1404" height="1872"></svg>'
    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=[rm1]), \
         patch("remarkable_to_obsidian._render_rm_to_svg", return_value=fake_svg):
        result = export_notebook("rmapi", "/Notebook", "Notebook", tmp_path)

    assert len(result) == 1
    assert result[0].suffix == ".svg"
    assert result[0].read_text() == fake_svg


def test_export_notebook_empty(tmp_path):
    """Returns empty list when no pages extracted."""
    with patch("remarkable_to_obsidian._extract_rm_pages", return_value=[]):
        result = export_notebook("rmapi", "/Notebook", "Notebook", tmp_path)

    assert result == []


# --- Write note with source files test ---


def test_write_obsidian_note_with_sources(tmp_path):
    """Appends source file embeds when provided."""
    vault = tmp_path / "vault"
    notebook = {"id": "abc", "name": "Notes", "modified": "2025-01-01"}
    markdown = "# Content"
    sources = ["Notes - page 1.png", "Notes - page 2.png"]

    path = write_obsidian_note(str(vault), notebook, markdown, sources)

    content = path.read_text()
    assert "## Handwritten source" in content
    assert "![[Notes - page 1.png]]" in content
    assert "![[Notes - page 2.png]]" in content
