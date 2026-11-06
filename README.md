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
- **A model backend**: an explicitly selected OpenAI-compatible vision server, Hendrix AI Gateway, Claude CLI, Anthropic API or Vertex AI. See [Configuration and providers](#configuration-and-providers).

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

## Configuration and providers

Use environment variables or the existing gitignored `.env` beside the script
(loaded without overriding the environment). There is no provider UI or new config
format. Model-processing runs now require an explicit `RM_BACKEND` and `RM_MODEL`.
Missing/unknown providers, contradictory selectors and empty models fail closed
before model-run discovery, downloads or output writes. Validation never probes
endpoints or downloads models.

```bash
# Local chat-completions server: replace the model identifier with one it serves.
RM_BACKEND=openai-compatible
RM_MODEL=your-literal-vision-model-id
RM_OPENAI_BASE_URL=http://127.0.0.1:8080/v1
RMAPI_BIN=/path/to/rmapi
OBSIDIAN_VAULT=/path/to/your/obsidian/vault
```

The adapter appends `/chat/completions` to the supplied base URL. It supports only
chat-completions text and ordered `image_url` data URLs, including PDF pages
rendered to PNG at 150 dpi. The endpoint and chosen model must support that vision
contract and `max_tokens`; this is not a universal OpenAI/API compatibility claim.
No endpoint probes, fallback providers or local-to-cloud fallback are performed.

| `RM_BACKEND` | Credentials / route | Consent |
|---|---|---|
| `openai-compatible` | Required `RM_OPENAI_BASE_URL`; optional env-only `RM_OPENAI_API_KEY` | No remote opt-in only at strict literal loopback |
| `anthropic` | `ANTHROPIC_API_KEY` or SDK auth token; Anthropic Messages | `RM_ALLOW_REMOTE_LLM=1` |
| `vertex` | GCP credentials, `ANTHROPIC_VERTEX_PROJECT_ID`, optional `CLOUD_ML_REGION` (default `europe-west1`) | `RM_ALLOW_REMOTE_LLM=1` |
| `hendrix` | `HENDRIX_API_KEY`; optional `RM_HENDRIX_URL` (existing gateway default retained) | `RM_ALLOW_REMOTE_LLM=1` |
| `claude-cli` | Existing Claude CLI login; optional `RM_CLAUDE_BIN` | `RM_ALLOW_REMOTE_LLM=1` |

All existing backends count as remote, including a self-hosted Hendrix gateway.
Only the literal value `1` grants remote consent, before SDK/auth construction or
CLI lookup/spawn and before model-run discovery/download/output mutation.
For example, after configuring credentials and choosing an available model:

```bash
RM_BACKEND=anthropic RM_MODEL=your-literal-vision-model-id RM_ALLOW_REMOTE_LLM=1 \
  uv run python remarkable_to_obsidian.py --pages 1-2
```

For OpenAI-compatible local inference, only `http(s)://127.0.0.1` and
`http(s)://[::1]` with valid optional ports and API paths qualify. DNS aliases
(including `localhost`), ambiguous/encoded authorities, userinfo, query strings
and fragments are rejected. Non-loopback endpoints require HTTPS and remote
consent. Existing SDK endpoint overrides also require HTTPS. HTTP adapters ignore
environment proxies and do not follow redirects, so redirects cannot forward
prompts or keys. HTTP timeouts are finite (60 seconds per operation); CLI timeout
is 900 seconds. No automatic HTTP retries; diagram validation may request up to
three conversions on the same provider/model.

`RM_TASK_MODEL`, `RM_TAG_MODEL` and `RM_DIAGRAM_MODEL` independently override the
main model for task extraction, tag inference and diagram classification/conversion
(including retries). Absent overrides inherit `RM_MODEL`; all use the same selected
provider. Supplied empty/whitespace/control-character model IDs are rejected.
Identifiers are otherwise passed literally, including CLI date suffixes; Vertex
IDs are restricted to letters, digits, `.`, `_`, `@` and `-` because the SDK puts
them into a URL path. Unsupported model IDs fail rather than being guessed or
rewritten. Optional API credentials are environment-only, never CLI arguments.

Other unchanged variables: `RM_WATCH_PATH` defaults to `/`; `RM_STATE_FILE` defaults
to `~/.remarkable_sync_state.json`; `OBSIDIAN_VAULT` defaults to `~/obsidian-vault`;
`RMAPI_BIN` defaults to `rmapi`. Existing Hendrix URL default is
`https://hendrix-genai.spotify.net/taskforce/glm-5-3-flash`.

### Deliberate migration

Old remote users must now choose `RM_BACKEND`, set `RM_MODEL`, and explicitly
consent with `RM_ALLOW_REMOTE_LLM=1`. The sole legacy mapping is
`CLAUDE_CODE_USE_VERTEX=1` without `RM_BACKEND`, which selects Vertex with a
migration warning (still requiring a model and consent). Contradictory selectors
are errors; remove the legacy variable when switching providers. Task/tag defaults
are no longer hardcoded Haiku: inheriting the main model can change cost/quality.
Provider/model changes do not invalidate transcription/PDF caches or automatically
reprocess existing notes. Existing scheduling scripts are not changed here and
must receive the same explicit environment before a model run can proceed.

`--dry-run`, `--list-only`, `--retag` and `--merge-states` never construct model
clients and need no model consent/configuration. List/preview discovery and retag
can still contact reMarkable cloud; retag/list/merge can write their normal outputs.
Preview remains download/model/write-free.

### Privacy and limits

- This is not direct-device/offline ingestion. Discovery and downloads use the
  reMarkable cloud even with local inference. Full archives can be downloaded
  before page/tag selection; only selected pages/PDF subsets reach model processing.
- Model submissions include page images or PDF content, notebook name/path/page
  context, custom prompts, diagram crops, and optional task/tag markdown. Ordinary
  full sync can include up to 300 characters of previous-page transcription;
  selected mode omits previous-page text and rejects old context-bearing cache.
  Autolinking can add matching vault titles to markdown before task/tag inference.
- A loopback destination does not establish that its server stays local: the
  server may forward requests remotely. Inspect its configuration and policies.
  Provider retention, training use and server-side logs are outside this tool's
  guarantees; remote consent does not establish a retention policy.
- State JSON caches plaintext transcription. Notes, source attachments, diagram
  assets and logs are plaintext and may be copied by vault/cloud sync. The sync
  log includes notebook metadata, but new errors use fixed sanitized diagnostics,
  not raw upstream bodies/stderr/URLs/keys/exception chains. SDK HTTP/request
  logging is suppressed. Old log entries and old excluded attachments remain.
- CLI prompts travel through stdin, not process arguments. Selected attachments
  are temporary plaintext files read by the CLI. Known unrelated API credentials
  and provider selectors are scrubbed from its environment; this is not complete
  process isolation. The CLI's own persisted settings, tool access, network behavior
  and retention are not sandboxed by this change. Configure/review that runtime
  separately; HTTP-adapter proxy/redirect controls do not control CLI internals.
- Temporary removal is best effort, not secure erasure. There is no encryption,
  retention cleanup, retroactive erasure or automatic reprocessing in this feature.

## Usage

### Dry run (preview without writing)

```bash
uv run python remarkable_to_obsidian.py --dry-run
```

Preview lists candidate notebooks without downloading pages, calling models,
moving notes or writing notes, attachments, logs or state (including in-memory
state). It does not estimate changed-page counts. Combining `--dry-run` with
`--retag`, `--merge-states` or `--list-only` is rejected before any writes.

### Full sync

```bash
uv run python remarkable_to_obsidian.py
```

### Sync a specific folder

```bash
RM_WATCH_PATH="/Work/Meeting Notes" uv run python remarkable_to_obsidian.py
```

### Select notebooks and pages by native tags

```bash
# Within the usual watched folder, include either tag and exclude private pages
RM_WATCH_PATH="/Work" uv run python remarkable_to_obsidian.py \
  --include-tag work --include-tag meeting --exclude-tag private

# Intersect native tag selection with original page numbers
uv run python remarkable_to_obsidian.py --include-tag work --pages 1,3-5

# Ordinary PDF page ranges (no native tag filters)
uv run python remarkable_to_obsidian.py --pages 2-4

# Download/model/write-free preview; page-tag selection remains unresolved
uv run python remarkable_to_obsidian.py --include-tag work --dry-run
```

`--include-tag` and `--exclude-tag` are repeatable. Matching trims leading/trailing
whitespace and uses Unicode case-insensitive matching; internal whitespace is
literal. Includes match **ANY** tag; exclusions always win. Native notebook tags
apply to every page; native `pageTags` apply only to their associated `pageId`.
No includes means all available pages minus exclusions. `--pages` accepts only
positive 1-based numbers and closed ascending ranges, separated by commas. Ranges
intersect tags and available pages; duplicates never duplicate output. Page order
and original numbers are retained in headings, context and `selected_pages`.
Folder/watch-path and `.sync_ignore` restrictions still apply, including JSON
notebook lists. Inferred, frontmatter and built-in tags are never selection evidence.

Filtering happens **before AI processing, not necessarily before downloading**:
the full native archive may be downloaded locally to read `.content`. Only
selected pages reach transcription, task/tag inference, source embeds and diagram
assets. This is not an offline mode: discovery/downloads use reMarkable cloud and
selected content uses the configured model backend. Preview does neither download
nor model calls and reports unresolved selection rather than guessing page tags.

Supported native orderings are legacy `.content.pages` ID arrays and live
`cPages.pages` with unique `idx.value` ordering indices (lexicographic order).
Missing/ambiguous order, tag associations or selected stroke files fail closed.
PDF ranges create single-page subsets before model submission and source export.
Ordinary PDF files and unannotated PDF archives are supported; archives with page
IDs must provide an explicit identity `redirectionPageMap` and matching counts.
Annotated/reordered PDFs, PDF native-tag filters, EPUBs and unsupported native
metadata formats return errors, never fall back to whole-document processing.

No matching pages is a skip, not an empty overwrite or successful source-state
advance. Selection runs inspect metadata even if source version/time is unchanged.
Safe context-free transcription is reused by native page ID and content hash;
ordinary/full-sync cache is not trusted across an exclusion boundary. Selective
transcription omits previous-page text so changing selection cannot leak cached
excluded context. Returning to full sync without flags rebuilds complete output;
previously selective notebooks continue metadata inspection and context-free
caching. Notebooks never selectively synced keep ordinary sync behavior.

Selection is **not retroactive erasure**: old notes on no-match skips and old
attachments remain on disk, including previously exported excluded content.
Successful nonempty selections replace only an unedited managed note; edit and
legacy-note conflicts leave output/source state untouched. Selection cannot be
combined with `--retag`, `--merge-states` or `--list-only`.

### Re-tag without re-transcribing

Update tags from reMarkable metadata without re-running transcription:

```bash
uv run python remarkable_to_obsidian.py --retag
```

### Task extraction and tag inference

Optionally extract action items or infer semantic tags using the selected provider and task/tag models:

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

Source page images are saved to `<vault>/Attachments/reMarkable/`. New source
and diagram filenames use the notebook ID and content hashes, not the display
name. Same-named notebooks remain separate; renames retain valid references.
Old attachments are retained, not deleted or renamed. Partial updates supply
unchanged-page images too, including diagram sources from cached transcription.

A rolling sync log is maintained at `<vault>/Remarkable Notes/Sync Log.md` (newest first, max 10 entries).
If a notebook or other non-log note already occupies that path, logging reports
a conflict and leaves it untouched; it does not select another output path.

### Diagram conversion

Handwritten diagrams are automatically converted to editable formats:

- **Mermaid** — flowcharts, sequence diagrams, state diagrams, architecture diagrams. Embedded inline as ```` ```mermaid ```` code blocks that Obsidian renders natively.
- **Excalidraw** — freeform sketches, mind maps, arbitrary layouts. Saved as `.excalidraw.md` files compatible with the [Obsidian Excalidraw plugin](https://github.com/zsviczian/obsidian-excalidraw-plugin).

The original cropped PNG is always kept as a fallback in `Attachments/reMarkable/`.

## Incremental sync

The tool tracks sync state per-page using SHA-256 hashes of the raw `.rm` stroke files. On subsequent runs:

- **Unchanged notebook version or modified time**: Skipped entirely (no download). The reMarkable cloud often reports version `0` forever, so the `ModifiedClient` timestamp from the last successful sync is the main check.
- **Changed, but unchanged pages**: Downloads but reuses cached transcriptions (no API calls)
- **Changed pages only**: Only sends modified pages to the model
- **PDF notebooks**: No per-page tracking; a SHA-256 of the exported PDF is stored and the notebook is re-transcribed only when it changes

Notebook metadata is fetched with one rmapi shell session for all `stat` calls. Every separate rmapi invocation re-authenticates and refetches the document tree, and about 30 in a row get HTTP 429 from the reMarkable cloud.

Notebooks with a `/` in their name can't be addressed by path through rmapi and are skipped; rename them on the device.

This minimizes API usage and sync time.

### Obsidian edits and first upgrade

Successful syncs record a SHA-256 of the generated note in that notebook's
state entry (`output_hash`). Before moving a note or regenerating any assets,
sync checks the existing note against that hash. Any Obsidian edit—including
custom frontmatter or completed tasks—produces a visible `Conflict` error.
The original note and its assets stay untouched, and the new source version is
not marked synced. An occupied rename destination is also a conflict.

Existing notes without a recorded output hash are treated as conflicts even
when they look generated. Sync never assumes a legacy note is safe to replace.
To deliberately regenerate or migrate one notebook:

1. Stop scheduled sync and back up the note, its attachments and the state file.
2. Move the existing note outside `Remarkable Notes/` (including its old path
   if the notebook was renamed). Keep its attachments in place for old embeds.
3. Remove only that notebook ID's entry from `notebooks` in `RM_STATE_FILE`.
4. Run sync again. A fresh note and ID-scoped assets will be generated; manually
   copy any desired edits from the saved note. Later syncs protect those edits
   by reporting conflicts again rather than merging them.

There is no two-way sync or automatic merge. Content updates render all pages
locally to supply complete assets; only changed pages need transcription calls.
Run one sync process per notebook; this is not a concurrent-editor locking or
crash-transaction system. Re-tagging a protected note also counts as an edit on
the next content sync and requires the same deliberate resolution workflow.

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
uv run pytest tests -v
```

## License

MIT
