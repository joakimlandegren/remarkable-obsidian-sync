"""Sync reMarkable handwritten notebooks to Obsidian via Claude vision."""

import json
import logging
import os
import subprocess
import sys
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


def list_notebooks(rmapi_bin: str, watch_path: str) -> list[dict]:
    """Recursively list all notebooks under watch_path using rmapi ls --json."""
    notebooks = []
    _walk_directory(rmapi_bin, watch_path, notebooks)
    return notebooks


def _walk_directory(rmapi_bin: str, path: str, notebooks: list[dict]) -> None:
    """Recursively walk a reMarkable directory, collecting notebooks."""
    result = subprocess.run(
        [rmapi_bin, "ls", "--json", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("rmapi ls failed (exit %d). Run `rmapi` to authenticate.", result.returncode)
        sys.exit(1)

    entries = json.loads(result.stdout)
    for entry in entries:
        full_path = f"{path.rstrip('/')}/{entry['name']}"
        if entry["type"] == "DocumentType":
            notebooks.append({
                "id": entry["id"],
                "name": entry["name"],
                "version": entry["version"],
                "modified": entry["modifiedClient"],
                "path": full_path,
            })
        elif entry["type"] == "CollectionType":
            _walk_directory(rmapi_bin, full_path, notebooks)


def export_notebook_pdf(rmapi_bin: str, notebook_path: str, name: str, output_dir: Path) -> Path | None:
    """Export a notebook as annotated PDF using rmapi geta. Returns path to PDF or None on failure."""
    result = subprocess.run(
        [rmapi_bin, "geta", "-o", str(output_dir), notebook_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Failed to export %s: %s", notebook_path, result.stderr)
        return None

    pdf_path = output_dir / f"{name}.pdf"
    if not pdf_path.exists():
        # rmapi may use a slightly different name; find any PDF in the dir
        pdfs = list(output_dir.glob("*.pdf"))
        if pdfs:
            pdf_path = pdfs[0]
        else:
            log.error("No PDF found after exporting %s", notebook_path)
            return None
    return pdf_path
