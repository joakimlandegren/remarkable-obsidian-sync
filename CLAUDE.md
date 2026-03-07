# remarkable-obsidian-sync

Syncs handwritten reMarkable notebooks to Obsidian as markdown via Claude vision transcription.

## Obsidian vault

Set `OBSIDIAN_VAULT` to your vault path. Default: `~/obsidian-vault`.

**iCloud vault path:** `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/` — set `OBSIDIAN_VAULT` to this when syncing to an iCloud-backed Obsidian vault. The default `~/obsidian-vault` is a local-only directory that won't sync to other devices.

To open a file in Obsidian (not the default text editor), use the Obsidian URI scheme:
```
open "obsidian://open?vault=VAULT_NAME&file=<path-within-vault>"
```
The file path is relative to the vault root, without `.md` extension, and URL-encoded.

## Prerequisites

- `rmapi` — reMarkable cloud CLI. Set `RMAPI_BIN` to its path (e.g. `~/go/bin/rmapi`). **May not be on PATH** — always set `RMAPI_BIN` explicitly.
- `uv` — Python package manager. Install with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- `cairo` — Required by `cairosvg` for SVG-to-PNG. Install with `brew install cairo`.
- Authenticate rmapi first: run `rmapi` interactively to complete the one-time device registration.
- **gcloud ADC** — Required for Vertex AI. Run `gcloud auth application-default login` if you get `invalid_grant` errors.

## Configuration

Create a `.env` file in the project root (gitignored). The script loads it automatically without overriding existing env vars.

```bash
# .env
RMAPI_BIN=/path/to/rmapi
OBSIDIAN_VAULT=/path/to/your/obsidian/vault

# For Vertex AI (preferred)
CLAUDE_CODE_USE_VERTEX=1
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id
CLOUD_ML_REGION=europe-west1

# Or for direct Anthropic API
# ANTHROPIC_API_KEY=sk-ant-...
```

## Running

```bash
uv run python remarkable_to_obsidian.py
```

Use `--dry-run` to preview without writing files. Use `RM_WATCH_PATH="/folder"` to sync a specific folder.

### Common errors

| Error | Cause | Fix |
|---|---|---|
| `FileNotFoundError: 'rmapi'` | `RMAPI_BIN` not set | Set `RMAPI_BIN` in `.env` |
| `PermissionDeniedError: 403` on Vertex | Wrong GCP project ID | Check `ANTHROPIC_VERTEX_PROJECT_ID` in `.env` |
| `invalid_grant` | ADC credentials expired | Run `gcloud auth application-default login` |
| `rmapi` auth failure | Device not registered | Run `rmapi` interactively once to register |
| Notes not in iCloud vault | `OBSIDIAN_VAULT` not set | Set `OBSIDIAN_VAULT` in `.env` to iCloud path |

### Parallel batch sync

Split work across agents for faster sync:

```bash
# 1. List notebooks to JSON (one-time, avoids repeated rmapi queries)
uv run python remarkable_to_obsidian.py --list-only /tmp/notebooks.json

# 2. Run batches in parallel (each with its own state file)
RM_STATE_FILE=/tmp/state_0.json uv run python remarkable_to_obsidian.py --notebooks-json /tmp/notebooks.json --slice 0:28 &
RM_STATE_FILE=/tmp/state_1.json uv run python remarkable_to_obsidian.py --notebooks-json /tmp/notebooks.json --slice 28:56 &
# ... etc

# 3. Merge batch states into main state
uv run python remarkable_to_obsidian.py --merge-states /tmp/state_*.json
```

## Testing

```bash
uv run pytest tests/test_sync.py -v
```

## Architecture

Single-file script (`remarkable_to_obsidian.py`). Uses `rmapi` CLI for reMarkable cloud access, `rmscene` for parsing .rm v6 stroke files, `cairosvg` for SVG-to-PNG conversion, and Anthropic SDK for handwriting transcription. Output goes to `Remarkable Notes/` in the vault with source page images saved to `Attachments/reMarkable/`.
