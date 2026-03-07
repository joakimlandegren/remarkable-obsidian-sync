# remarkable-obsidian-sync

Syncs handwritten reMarkable notebooks to Obsidian as markdown via Claude vision transcription.

## Obsidian vault

Set `OBSIDIAN_VAULT` to your vault path. Default: `~/obsidian-vault`.

To open a file in Obsidian (not the default text editor), use the Obsidian URI scheme:
```
open "obsidian://open?vault=VAULT_NAME&file=<path-within-vault>"
```
The file path is relative to the vault root, without `.md` extension, and URL-encoded.

## Running

```bash
# With Anthropic API key
ANTHROPIC_API_KEY=sk-... \
OBSIDIAN_VAULT=~/my-vault \
uv run python remarkable_to_obsidian.py

# With Vertex AI
CLAUDE_CODE_USE_VERTEX=1 \
ANTHROPIC_VERTEX_PROJECT_ID=your-project-id \
CLOUD_ML_REGION=europe-west1 \
OBSIDIAN_VAULT=~/my-vault \
uv run python remarkable_to_obsidian.py
```

Use `--dry-run` to preview without writing files. Use `RM_WATCH_PATH="/folder"` to sync a specific folder.

## Testing

```bash
uv run pytest tests/test_sync.py -v
```

## Architecture

Single-file script (`remarkable_to_obsidian.py`). Uses `rmapi` CLI for reMarkable cloud access, `rmscene` for parsing .rm v6 stroke files, `cairosvg` for SVG-to-PNG conversion, and Anthropic SDK for handwriting transcription. Output goes to `Remarkable Notes/` in the vault with source page images saved to `Attachments/reMarkable/`.
