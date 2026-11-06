# remarkable-obsidian-sync

Syncs handwritten reMarkable notebooks to Obsidian as markdown via configurable vision models.

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

# Local vision server; replace the model ID with one the server supports
RM_BACKEND=openai-compatible
RM_MODEL=your-literal-vision-model-id
RM_OPENAI_BASE_URL=http://127.0.0.1:8080/v1
```

Model runs require explicit `RM_BACKEND` and `RM_MODEL`. Supported backends are
`openai-compatible`, `anthropic`, `vertex`, `hendrix`, and `claude-cli`.
`RM_TASK_MODEL`, `RM_TAG_MODEL`, and `RM_DIAGRAM_MODEL` independently override
the main model; absent overrides inherit `RM_MODEL` on the same provider.

Remote AI requires the user's explicit `RM_ALLOW_REMOTE_LLM=1`. Never enable
remote consent automatically or fall back from local processing to a cloud
provider. Anthropic, Vertex, Hendrix and Claude CLI all count as remote. Only
strict literal loopback OpenAI-compatible endpoints qualify without remote
consent (`127.0.0.1` or `[::1]`, not `localhost`). Other allowed endpoints require
HTTPS and remote consent. Unknown/conflicting settings fail closed, without a
fallback provider. Missing model configuration fails before model-run discovery,
downloads or output writes. Provider/model changes do not reprocess cached notes.

Use README **Configuration and providers** for credentials, remote examples,
endpoint restrictions and legacy Vertex migration. Keep credentials in the
environment/gitignored `.env`, never command arguments or committed files.
Do not change live credentials or data-sharing settings as part of setup advice.

Local inference is not fully offline: reMarkable cloud discovery/downloads can
fetch whole archives before selection. Only selected content enters selective
model processing. State, notes, assets and logs remain plaintext; old exports are
not erased. CLI runtimes/local servers may forward or retain data outside this
tool's control. Do not claim sandboxing, secure erasure or guaranteed retention.

`--dry-run`, `--list-only`, `--retag`, and `--merge-states` never construct model
clients or need model consent/configuration; their normal cloud/file behavior
still applies. Preview remains download/model/write-free.

## Running

```bash
uv run python remarkable_to_obsidian.py
```

Use `--dry-run` to preview without writing files. Use `RM_WATCH_PATH="/folder"` to sync a specific folder.

### Common errors

| Error | Cause | Fix |
|---|---|---|
| `FileNotFoundError: 'rmapi'` | `RMAPI_BIN` not set | Set `RMAPI_BIN` in `.env` |
| Missing/invalid model configuration | No explicit valid provider/model | Set `RM_BACKEND` and `RM_MODEL` using README examples; never guess a fallback provider |
| Remote model processing denied | Explicit remote consent absent | Use a supported loopback model, or ask whether to set `RM_ALLOW_REMOTE_LLM=1`; never enable it automatically |
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

## Automatic sync

`sync.sh` + launchd plist for hourly sync during daytime (8am–10pm). The plist lives at `~/Library/LaunchAgents/com.remarkable.obsidian-sync.plist`. Manage with `launchctl load/unload/start`. Logs: `/tmp/rm_sync.log`.

## Testing

```bash
uv run pytest tests -v
```

## Architecture

Single-file script (`remarkable_to_obsidian.py`). Uses `rmapi` CLI for reMarkable cloud access, `rmscene` for parsing .rm v6 stroke files (with built-in v5 parser for older notebooks), `cairosvg` for SVG-to-PNG conversion, and the configured provider adapter for transcription.

**Output structure:** Notes preserve the reMarkable folder hierarchy under the vault root (e.g. `/1. Projects/Planning/Note` → `Remarkable Notes/1. Projects/Planning/Note.md`). Source page images go to `Attachments/reMarkable/`.

**Diagram conversion:** Handwritten diagrams detected during transcription use the selected provider and `RM_DIAGRAM_MODEL` (or `RM_MODEL`) for Mermaid/Excalidraw conversion. Mermaid is embedded inline; Excalidraw is saved as `.excalidraw.md` files. The original cropped PNG is kept as fallback.
