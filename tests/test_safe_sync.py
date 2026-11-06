"""Synthetic-only regressions for note preservation, preview and asset isolation."""
import copy
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
import remarkable_to_obsidian as app


def notebook(**changes):
    return {"id": "id-a", "name": "Same", "path": "/A/Same", "version": 1,
            "modified": "first", **changes}


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def sync(root, nb, state, dry=False, fail=False):
    page = root / "input.rm"
    if not page.exists():
        page.write_bytes(b"first")
    with patch.object(app, "_extract_rm_pages", return_value=[page]), \
         patch.object(app, "_render_rm_to_svg", return_value='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><path d="M1 1 L9 9"/></svg>'), \
         patch.object(app, "transcribe_page", side_effect=RuntimeError("synthetic failure") if fail else None,
                      return_value="- [ ] Task") as model, \
         patch.object(app, "_convert_diagram_to_vector", return_value=("fallback_png", "")):
        client = MagicMock()
        result = app.sync_notebooks([nb], state, str(root / "vault"), "forbidden", "fake", dry,
                                    client, str(root / "state.json"), blank_detect=False)
        if dry:
            model.assert_not_called()
            client.messages.create.assert_not_called()
        return result


@pytest.mark.parametrize("edit", ["\nUSER EDIT", "\ncustom: value", "completed"])
def test_edited_note_conflict_preserves_everything(tmp_path, edit):
    state = {}
    nb = notebook()
    assert sync(tmp_path, nb, state).synced
    note = tmp_path / "vault/Remarkable Notes/A/Same.md"
    content = note.read_text()
    if edit == "completed":
        note.write_text(content.replace("[ ]", "[x]"))
    elif "custom:" in edit:
        note.write_text(content.replace("source: reMarkable", "custom: value\nsource: reMarkable"))
    else:
        note.write_text(content + edit)
    (tmp_path / "input.rm").write_bytes(b"second")
    before, old_state = snapshot(tmp_path), copy.deepcopy(state)
    for _ in range(2):
        result = sync(tmp_path, notebook(version=2, modified="second", path="/B/Renamed", name="Renamed"), state)
        assert result.errored and "conflict" in result.errored[0].detail.lower()
        assert snapshot(tmp_path) == before
        assert state == old_state


@pytest.mark.parametrize("legacy", [3, {"version": 1, "path": "/A/Same", "name": "Same"}, {}])
def test_legacy_note_requires_deliberate_migration(tmp_path, legacy):
    nb = notebook()
    note = app.write_obsidian_note(str(tmp_path / "vault"), nb, "legacy custom data")
    state = {nb["id"]: legacy}
    before = snapshot(tmp_path / "vault")
    result = sync(tmp_path, notebook(version=2, modified="second"), state)
    assert result.errored and "conflict" in result.errored[0].detail.lower()
    assert snapshot(tmp_path / "vault") == before
    assert state == {nb["id"]: legacy}
    # Move the old note outside managed output, keeping all legacy assets.
    note.rename(tmp_path / "saved-legacy.md")
    assert sync(tmp_path, notebook(version=2, modified="second"), state).synced
    assert state[nb["id"]]["output_hash"] == app._hash_file(note)


@pytest.mark.parametrize("kind", ["rename", "legacy", "version", "modified", "pages", "changed"])
def test_preview_keeps_paths_bytes_and_state(tmp_path, kind):
    state = {}
    nb = notebook()
    sync(tmp_path, nb, state)
    if kind == "rename":
        nb = notebook(path="/B/New", name="New")
    elif kind == "legacy":
        state[nb["id"]] = {"version": 0, "pages": {}}
    elif kind in ("pages", "changed"):
        nb = notebook(version=2, modified="second")
        if kind == "changed":
            (tmp_path / "input.rm").write_bytes(b"changed")
    elif kind == "modified":
        nb = notebook(version=0)
    before, old_state = snapshot(tmp_path), copy.deepcopy(state)
    entry = state[nb["id"]]
    sync(tmp_path, nb, state, dry=True)
    sync(tmp_path, nb, state, dry=True)
    assert snapshot(tmp_path) == before
    assert state == old_state
    assert state[nb["id"]] is entry


@pytest.mark.parametrize("mode", [["--retag"], ["--merge-states", "source.json"], ["--list-only", "list.json"]])
def test_preview_rejects_alternate_writes(tmp_path, mode):
    config = {"state_file": str(tmp_path / "state.json"), "obsidian_vault": str(tmp_path / "vault"),
              "rmapi_bin": "forbidden", "watch_path": "/", "model": "fake"}
    before = snapshot(tmp_path)
    with patch.object(sys, "argv", ["sync", "--dry-run", *mode]), \
         patch.object(app, "load_config", return_value=config), \
         patch.object(app, "list_notebooks", return_value=[]), \
         patch.object(app, "retag_notebooks", side_effect=AssertionError("retag in preview")), \
         patch.object(app, "merge_state_files", side_effect=AssertionError("merge in preview")):
        with pytest.raises(SystemExit) as exc:
            app.main()
        assert exc.value.code == 2
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("suffix", [".png", ".pdf"])
def test_same_name_source_assets_are_isolated_and_stable(tmp_path, suffix):
    a, b = tmp_path / ("a" + suffix), tmp_path / ("b" + suffix)
    a.write_bytes(b"first"); b.write_bytes(b"second")
    vault = str(tmp_path / "vault")
    first = app.save_source_pages(vault, notebook(), [a])
    second = app.save_source_pages(vault, notebook(id="id-b"), [b])
    assert first != second
    assert app.save_source_pages(vault, notebook(name="Renamed"), [a]) == first
    for names, data in [(first, b"first"), (second, b"second")]:
        assert (Path(vault) / "Attachments/reMarkable" / names[0]).read_bytes() == data


def test_same_name_diagram_assets_are_isolated(tmp_path):
    image = tmp_path / "page.png"
    Image.new("RGB", (20, 20), "red").save(image)
    marker = "> [Diagram(page=1, top=10, bottom=90): chart]"
    with patch.object(app, "_convert_diagram_to_vector", return_value=("excalidraw", "[]")):
        one, first = app.extract_diagram_crops(marker, [image], str(tmp_path / "vault"), "Same",
                                              client=MagicMock(), model="fake", notebook_id="id-a")
        two, second = app.extract_diagram_crops(marker, [image], str(tmp_path / "vault"), "Same",
                                               client=MagicMock(), model="fake", notebook_id="id-b")
    assert set(first).isdisjoint(second)
    assert first[-1] in one and second[-1] in two


def test_partial_update_and_rename_supply_all_assets(tmp_path):
    a, b = tmp_path / "a.rm", tmp_path / "b.rm"
    a.write_bytes(b"a"); b.write_bytes(b"b")
    state = {}
    marker = "> [Diagram(page=1, top=10, bottom=90): chart]"
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><path d="M1 1 L9 9"/></svg>'
    def run(nb):
        with patch.object(app, "_extract_rm_pages", return_value=[a, b]), \
             patch.object(app, "_render_rm_to_svg", return_value=svg), \
             patch.object(app, "transcribe_page", return_value=marker) as model, \
             patch.object(app, "_convert_diagram_to_vector", return_value=("fallback_png", "")):
            result = app.sync_notebooks([nb], state, str(tmp_path / "vault"), "forbidden", "fake", False,
                                        MagicMock(), str(tmp_path / "state.json"), blank_detect=False)
        assert not result.errored
        return model.call_count
    assert run(notebook()) == 2
    legacy = tmp_path / "vault/Attachments/reMarkable/Same - page 1.png"
    legacy.write_bytes(b"keep legacy")
    b.write_bytes(b"changed")
    nb = notebook(version=2, modified="second", name="New", path="/B/New")
    assert run(nb) == 1
    note = tmp_path / "vault/Remarkable Notes/B/New.md"
    text = note.read_text()
    assert "[Diagram(" not in text
    for link in re.findall(r"!\[\[(.*?)\]\]", text):
        assert (tmp_path / "vault/Attachments/reMarkable" / link).is_file()
    assert legacy.read_bytes() == b"keep legacy"
    before = snapshot(tmp_path)
    assert run(nb) == 0
    assert snapshot(tmp_path) == before


def test_model_failure_does_not_advance_state_or_touch_output(tmp_path):
    state = {}
    sync(tmp_path, notebook(), state)
    (tmp_path / "input.rm").write_bytes(b"second")
    before, old_state = snapshot(tmp_path), copy.deepcopy(state)
    assert sync(tmp_path, notebook(version=2, modified="second"), state, fail=True).errored
    assert state == old_state and snapshot(tmp_path) == before


def test_two_same_named_notebooks_sync_independently(tmp_path):
    state = {}
    assert sync(tmp_path, notebook(), state).synced
    first = tmp_path / "vault/Remarkable Notes/A/Same.md"
    original = first.read_bytes()
    assert sync(tmp_path, notebook(id="id-b", path="/B/Same"), state).synced
    assert first.read_bytes() == original
    one = re.findall(r"!\[\[(.*?)\]\]", first.read_text())
    two = re.findall(r"!\[\[(.*?)\]\]", (tmp_path / "vault/Remarkable Notes/B/Same.md").read_text())
    assert one and two and set(one).isdisjoint(two)


def test_rename_occupied_destination_is_conflict(tmp_path):
    state = {}
    sync(tmp_path, notebook(), state)
    target = tmp_path / "vault/Remarkable Notes/B/New.md"
    target.parent.mkdir(parents=True)
    target.write_text("unrelated user note")
    before, old_state = snapshot(tmp_path), copy.deepcopy(state)
    result = sync(tmp_path, notebook(name="New", path="/B/New", version=2), state)
    assert result.errored and not result.synced
    assert state == old_state and snapshot(tmp_path) == before


def test_writer_refuses_untracked_existing_output(tmp_path):
    nb = notebook()
    note = app.write_obsidian_note(str(tmp_path), nb, "first")
    before = note.read_bytes()
    with pytest.raises(ValueError, match="Conflict"):
        app.write_obsidian_note(str(tmp_path), nb, "replacement")
    assert note.read_bytes() == before


def test_pdf_hash_skip_retains_output_protection_and_preview_is_read_only(tmp_path):
    state = {}
    client = MagicMock()
    def export(_binary, _path, output):
        (output / "source.pdf").write_bytes(b"synthetic PDF")
        return None
    def run(nb, dry=False):
        return app.sync_notebooks([nb], state, str(tmp_path / "vault"), "forbidden", "fake", dry,
                                  client, str(tmp_path / "state.json"))
    with patch.object(app, "_extract_rm_pages", side_effect=export) as download, \
         patch.object(app, "transcribe_pdf", return_value="PDF transcription") as model:
        assert run(notebook(version=0)).synced
        digest = state["id-a"]["output_hash"]
        assert run(notebook(version=0, modified="second")).skipped
        assert state["id-a"]["output_hash"] == digest
        assert model.call_count == 1
        before, old_state = snapshot(tmp_path), copy.deepcopy(state)
        count = download.call_count
        assert run(notebook(version=0, modified="third"), dry=True).dry_runs
        assert download.call_count == count and model.call_count == 1
        assert snapshot(tmp_path) == before and state == old_state
        note = tmp_path / "vault/Remarkable Notes/A/Same.md"
        note.write_text(note.read_text() + "\nUSER EDIT")
        before = snapshot(tmp_path)
        assert run(notebook(version=0, modified="third")).errored
        assert snapshot(tmp_path) == before and state == old_state


def test_legacy_note_in_another_folder_is_not_moved_or_adopted(tmp_path):
    app.write_obsidian_note(str(tmp_path / "vault"), notebook(path="/Old/Same"), "legacy data")
    state = {"id-a": 1}
    before = snapshot(tmp_path / "vault")
    assert sync(tmp_path, notebook(version=2), state).errored
    assert snapshot(tmp_path / "vault") == before and state == {"id-a": 1}


def test_page_hash_skip_retains_output_protection(tmp_path):
    state = {}
    sync(tmp_path, notebook(version=0), state)
    digest = state["id-a"]["output_hash"]
    assert sync(tmp_path, notebook(version=0, modified="second"), state).skipped
    assert state["id-a"]["output_hash"] == digest
    note = tmp_path / "vault/Remarkable Notes/A/Same.md"
    note.write_text(note.read_text() + "\nUSER EDIT")
    assert sync(tmp_path, notebook(version=0, modified="third"), state).errored


def test_main_log_cannot_overwrite_conflicted_notebook(tmp_path, monkeypatch, caplog):
    nb = notebook(name="Sync Log", path="/Sync Log")
    vault = str(tmp_path / "vault")
    note = app.write_obsidian_note(vault, nb, "original")
    state = {nb["id"]: {"version": 1, "path": nb["path"], "name": nb["name"],
                         "output_hash": app._hash_file(note)}}
    note.write_text(note.read_text() + "\nUSER EDIT")
    before, old_state = snapshot(tmp_path), copy.deepcopy(state)
    config = {"state_file": str(tmp_path / "state.json"), "obsidian_vault": vault,
              "rmapi_bin": "forbidden", "watch_path": "/", "model": "fake"}
    monkeypatch.setenv("RM_BACKEND", "claude-cli")
    monkeypatch.setenv("RM_MODEL", "fake")
    monkeypatch.setenv("RM_ALLOW_REMOTE_LLM", "1")
    with patch.object(sys, "argv", ["sync"]), \
         patch.object(app, "load_config", return_value=config), \
         patch.object(app, "load_state", return_value=state), \
         patch.object(app, "list_notebooks", return_value=[{**nb, "version": 2}]), \
         patch.object(app, "load_ignore_patterns", return_value=[]), \
         patch.object(app, "build_note_index", return_value={}), \
         patch.object(app, "_load_custom_prompts", return_value=[]), \
         patch.object(app, "ClaudeCLIClient", return_value=MagicMock()):
        app.main()
        app.main()
    assert "Conflict" in caplog.text
    assert snapshot(tmp_path) == before and state == old_state


def test_sync_log_preserves_unrecognized_user_file(tmp_path):
    log = tmp_path / "Remarkable Notes/Sync Log.md"
    log.parent.mkdir()
    log.write_text("Personal log\nUSER EDIT")
    before = snapshot(tmp_path)
    result = app.write_sync_log(str(tmp_path), app.SyncSummary())
    assert result is None and snapshot(tmp_path) == before
