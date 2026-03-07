# remarkable-obsidian-sync

Sync handwritten [reMarkable](https://remarkable.com/) notebooks to [Obsidian](https://obsidian.md/) as searchable markdown, using Claude's vision capabilities to transcribe handwriting.

## What it does

1. Lists notebooks on your reMarkable cloud via [`rmapi`](https://github.com/ddvk/rmapi)
2. Downloads and renders handwritten pages (parses `.rm` v6 stroke files to SVG, converts to PNG)
3. Sends each page to Claude for handwriting transcription
4. Writes the transcription as a markdown note with YAML frontmatter to your Obsidian vault
5. Embeds the original handwritten page images alongside the transcription
6. Tracks state per-page so subsequent syncs only re-transcribe changed pages

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **[rmapi](https://github.com/ddvk/rmapi)** for reMarkable cloud access (Go binary)
- **An Anthropic API key** or **Google Cloud Vertex AI** access

### Install rmapi

```bash
# macOS with Homebrew
brew install rmapi

# Or build from source (requires Go)
go install github.com/ddvk/rmapi@latest
```

Then authenticate:

```bash
rmapi
# Follow the one-time device code auth flow
```

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/remarkable-obsidian-sync.git
cd remarkable-obsidian-sync
uv sync
```

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OBSIDIAN_VAULT` | `~/obsidian-vault` | Path to your Obsidian vault |
| `RMAPI_BIN` | `rmapi` | Path to the rmapi binary |
| `RM_WATCH_PATH` | `/` | reMarkable folder to sync (e.g., `/Work/Notes`) |
| `RM_MODEL` | `claude-opus-4-6` | Claude model to use for transcription |
| `RM_STATE_FILE` | `~/.remarkable_sync_state.json` | Path to the sync state file |
| `ANTHROPIC_API_KEY` | *(required unless using Vertex)* | Your Anthropic API key |
| `CLAUDE_CODE_USE_VERTEX` | *(unset)* | Set to `1` to use Vertex AI instead |
| `ANTHROPIC_VERTEX_PROJECT_ID` | *(required for Vertex)* | GCP project ID |
| `CLOUD_ML_REGION` | `europe-west1` | GCP region for Vertex AI |

## Usage

### Dry run (preview without writing)

```bash
OBSIDIAN_VAULT=~/my-vault uv run python remarkable_to_obsidian.py --dry-run
```

### Full sync

```bash
# Using Anthropic API directly
ANTHROPIC_API_KEY=sk-ant-... \
OBSIDIAN_VAULT=~/my-vault \
uv run python remarkable_to_obsidian.py

# Using Vertex AI
CLAUDE_CODE_USE_VERTEX=1 \
ANTHROPIC_VERTEX_PROJECT_ID=my-project \
OBSIDIAN_VAULT=~/my-vault \
uv run python remarkable_to_obsidian.py
```

### Sync a specific folder

```bash
RM_WATCH_PATH="/Work/Meeting Notes" \
OBSIDIAN_VAULT=~/my-vault \
uv run python remarkable_to_obsidian.py
```

## Output

Notes are written to `<vault>/Remarkable Notes/<notebook name>.md` with:

- YAML frontmatter (title, modification date, reMarkable ID, tags)
- Transcribed markdown content
- Embedded handwritten source images at the bottom

Source page images are saved to `<vault>/Attachments/reMarkable/`.

Diagrams and sketches that can't be represented as text are cropped from the source images and embedded inline.

## Incremental sync

The tool tracks sync state per-page using SHA-256 hashes of the raw `.rm` stroke files. On subsequent runs:

- **Unchanged notebook version**: Skipped entirely (no download)
- **Changed version, unchanged pages**: Downloads but reuses cached transcriptions (no API calls)
- **Changed pages only**: Only sends modified pages to Claude

This minimizes API usage and sync time.

## Running tests

```bash
uv run pytest tests/test_sync.py -v
```

## License

MIT
