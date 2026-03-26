# remarkable-obsidian-sync

Sync handwritten [reMarkable](https://remarkable.com/) notebooks to [Obsidian](https://obsidian.md/) as searchable markdown, using Claude's vision capabilities to transcribe handwriting.

## What it does

1. Lists notebooks on your reMarkable cloud via [`rmapi`](https://github.com/ddvk/rmapi)
2. Downloads and renders handwritten pages (parses `.rm` v5/v6 stroke files to SVG, converts to PNG)
3. Sends each page to Claude for handwriting transcription
4. Converts handwritten diagrams to **Mermaid** (flowcharts, sequences) or **Excalidraw** (freeform drawings)
5. Writes the transcription as a markdown note with YAML frontmatter, preserving your reMarkable folder structure
6. Embeds the original handwritten page images alongside the transcription
7. Tracks state per-page so subsequent syncs only re-transcribe changed pages

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

Create a `.env` file in the project root (gitignored). The script loads it automatically without overriding existing env vars:

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

All configuration variables can also be set as environment variables:

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
uv run python remarkable_to_obsidian.py --dry-run
```

### Full sync

```bash
uv run python remarkable_to_obsidian.py
```

### Sync a specific folder

```bash
RM_WATCH_PATH="/Work/Meeting Notes" uv run python remarkable_to_obsidian.py
```

### Re-tag without re-transcribing

Update tags from reMarkable metadata without re-running transcription:

```bash
uv run python remarkable_to_obsidian.py --retag
```

### Task extraction and tag inference

Optionally extract action items or infer semantic tags from transcriptions using a lightweight model:

```bash
# Extract action items (inserted as task lists in each note)
uv run python remarkable_to_obsidian.py --extract-tasks

# Infer semantic tags (added to YAML frontmatter)
uv run python remarkable_to_obsidian.py --infer-tags
```

### Disabling auto-linking and blank detection

By default, the sync auto-links known vault note names as Obsidian `[[wikilinks]]` and skips blank pages to save API calls. Both can be disabled:

```bash
uv run python remarkable_to_obsidian.py --no-autolink --no-blank-detect
```

### Excluding notebooks

Create a `.sync_ignore` file in the project root to skip specific notebooks. One pattern per line, supports glob wildcards:

```
Kvitto*
Financial Core*
*.docx
/trash/*
```

Patterns match against both the notebook name and its full reMarkable path.

### Custom prompts

Place `.txt` files in a `prompts/` directory next to the script. Each filename is a glob pattern matched against notebook names/paths — the file content is used as the transcription prompt for matching notebooks.

### Parallel batch sync

For large libraries, split work across parallel processes:

```bash
# 1. List notebooks to JSON (avoids repeated rmapi queries)
uv run python remarkable_to_obsidian.py --list-only /tmp/notebooks.json

# 2. Run batches in parallel (each with its own state file)
RM_STATE_FILE=/tmp/state_0.json uv run python remarkable_to_obsidian.py \
  --notebooks-json /tmp/notebooks.json --slice 0:28 &
RM_STATE_FILE=/tmp/state_1.json uv run python remarkable_to_obsidian.py \
  --notebooks-json /tmp/notebooks.json --slice 28:56 &
# ... etc

# 3. Merge batch states into main state
uv run python remarkable_to_obsidian.py --merge-states /tmp/state_*.json
```

## Output

Notes preserve your reMarkable folder hierarchy under `<vault>/Remarkable Notes/`. For example, `/1. Projects/Planning/Note` becomes `Remarkable Notes/1. Projects/Planning/Note.md`.

Each note includes:

- YAML frontmatter (title, modification date, reMarkable ID/path, tags)
- Transcribed markdown content
- Embedded handwritten source images at the bottom

Source page images are saved to `<vault>/Attachments/reMarkable/`.

A rolling sync log is maintained at `<vault>/Remarkable Notes/Sync Log.md` (newest first, max 10 entries).

### Diagram conversion

Handwritten diagrams are automatically converted to editable formats:

- **Mermaid** — flowcharts, sequence diagrams, state diagrams, architecture diagrams. Embedded inline as ```` ```mermaid ```` code blocks that Obsidian renders natively.
- **Excalidraw** — freeform sketches, mind maps, arbitrary layouts. Saved as `.excalidraw.md` files compatible with the [Obsidian Excalidraw plugin](https://github.com/zsviczian/obsidian-excalidraw-plugin).

The original cropped PNG is always kept as a fallback in `Attachments/reMarkable/`.

## Incremental sync

The tool tracks sync state per-page using SHA-256 hashes of the raw `.rm` stroke files. On subsequent runs:

- **Unchanged notebook version**: Skipped entirely (no download)
- **Changed version, unchanged pages**: Downloads but reuses cached transcriptions (no API calls)
- **Changed pages only**: Only sends modified pages to Claude

This minimizes API usage and sync time.

## Automatic sync (macOS)

The included `sync.sh` wrapper runs the sync hourly between 8am and 10pm. Create a launchd plist to schedule it:

```bash
cat > ~/Library/LaunchAgents/com.remarkable.obsidian-sync.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.remarkable.obsidian-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>SYNC_SH_PATH</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>/tmp/rm_sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/rm_sync.log</string>
</dict>
</plist>
EOF
```

Replace `SYNC_SH_PATH` with the absolute path to `sync.sh` (e.g., `/Users/you/remarkable-obsidian-sync/sync.sh`).

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.remarkable.obsidian-sync.plist
```

Logs go to `/tmp/rm_sync.log`.

```bash
# Run immediately
launchctl start com.remarkable.obsidian-sync

# Stop
launchctl unload ~/Library/LaunchAgents/com.remarkable.obsidian-sync.plist

# Check logs
cat /tmp/rm_sync.log
```

Edit `sync.sh` to adjust the time window.

## Running tests

```bash
uv run pytest tests/test_sync.py -v
```

## License

MIT
