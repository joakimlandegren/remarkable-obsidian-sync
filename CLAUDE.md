# remarkable-obsidian-sync

Syncs handwritten reMarkable notebooks to Obsidian as markdown via Claude vision transcription.

## Obsidian vault

The default Obsidian vault is at:
`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/joakimlandegren`

Vault name: `joakimlandegren`

To open a file in Obsidian (not the default text editor), use the Obsidian URI scheme:
```
open "obsidian://open?vault=joakimlandegren&file=<path-within-vault>"
```
The file path is relative to the vault root, without `.md` extension, and URL-encoded.
Example: `open "obsidian://open?vault=joakimlandegren&file=Remarkable%20Notes%2F2026-03-07%201104%20Quick%20sheets"`

## Running

```bash
CLAUDE_CODE_USE_VERTEX=1 \
CLOUD_ML_REGION=europe-west1 \
ANTHROPIC_VERTEX_PROJECT_ID=spotify-claude-code-trial \
RMAPI_BIN=~/go/bin/rmapi \
uv run python remarkable_to_obsidian.py
```

Use `--dry-run` to preview without writing files. Use `RM_WATCH_PATH="/folder"` to sync a specific folder.

## Testing

```bash
uv run pytest tests/test_sync.py -v
```

## Architecture

Single-file script (`remarkable_to_obsidian.py`). Uses `rmapi` CLI for reMarkable cloud access, `rmscene` for parsing .rm v6 stroke files, `cairosvg` for SVG-to-PNG conversion, and Anthropic SDK (via Vertex AI) for handwriting transcription. Output goes to `Remarkable Notes/` in the vault with source page images saved to `Attachments/reMarkable/`.
