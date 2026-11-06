"""Sync reMarkable handwritten notebooks to Obsidian via Claude vision."""

import argparse
import base64
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rmscene import read_blocks, SceneLineItemBlock


@dataclass
class SyncResult:
    """Outcome for a single notebook sync attempt."""
    name: str
    path: str
    action: str  # "synced", "skipped", "errored", "dry_run"
    detail: str = ""


@dataclass
class SyncSummary:
    """Collects results from a full sync run."""
    results: list[SyncResult] = field(default_factory=list)

    @property
    def synced(self) -> list[SyncResult]:
        return [r for r in self.results if r.action == "synced"]

    @property
    def skipped(self) -> list[SyncResult]:
        return [r for r in self.results if r.action == "skipped"]

    @property
    def errored(self) -> list[SyncResult]:
        return [r for r in self.results if r.action == "errored"]

    @property
    def dry_runs(self) -> list[SyncResult]:
        return [r for r in self.results if r.action == "dry_run"]


def _load_dotenv():
    """Load .env.local from the script's directory (does not override existing env vars)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and _ and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

log = logging.getLogger(__name__)

# Cache for page tags extracted from .content files during _extract_rm_pages.
# Keyed by notebook_path, consumed by sync_notebooks.
_content_tags: dict[str, list[str]] = {}


def load_ignore_patterns() -> list[str]:
    """Load glob patterns from .sync_ignore file. One pattern per line, # for comments."""
    ignore_file = Path(__file__).parent / ".sync_ignore"
    if not ignore_file.exists():
        return []
    patterns = []
    for line in ignore_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def is_ignored(notebook_name: str, notebook_path: str, patterns: list[str]) -> bool:
    """Check if a notebook matches any ignore pattern (matched against name and path)."""
    for pattern in patterns:
        if fnmatch.fnmatch(notebook_name, pattern) or fnmatch.fnmatch(notebook_path, pattern):
            return True
    return False

# reMarkable page dimensions
RM_WIDTH = 1404
RM_HEIGHT = 1872


class ProviderError(RuntimeError):
    """A fixed diagnostic, never an upstream response or exception string."""


def _quiet_provider_logs():
    # SDK DEBUG logs contain request bodies; HTTP INFO logs contain URLs.
    prefixes = ('anthropic', 'httpx', 'httpcore', 'google')
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if name.split('.')[0] in prefixes:
            logger = logging.getLogger(name)
            logger.disabled = True
            logger.setLevel(logging.CRITICAL + 1)


def _require_remote_consent():
    if os.environ.get('RM_ALLOW_REMOTE_LLM') != '1':
        raise ProviderError('Remote LLM denied; set RM_ALLOW_REMOTE_LLM=1 to consent')


def _model_value(name, fallback=None):
    value = os.environ.get(name, fallback)
    if not isinstance(value, str) or not value or value.startswith('-') or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProviderError('Invalid or missing model configuration')
    return value


def _endpoint(value, *, require_https=False):
    """Validate without DNS, probing or normalization of an ambiguous authority."""
    from urllib.parse import urlsplit
    import ipaddress
    try:
        if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value) or '\\' in value:
            raise ValueError
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.netloc or any(c in value for c in ('?', '#')):
            raise ValueError
        if '@' in url.netloc or '%' in url.netloc or url.username is not None:
            raise ValueError
        host = url.hostname
        if not host or url.port == 0 or url.netloc.endswith(':'):
            raise ValueError
        local = host in ('127.0.0.1', '::1')
        # Reject alternate loopback spellings rather than treating them as remote.
        if not local:
            if host.rstrip('.').lower() == 'localhost':
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or re.fullmatch(r'[0-9.xXa-fA-F]+', host):
                    raise ValueError
            else:
                if address.is_loopback or address.version == 6 and address.ipv4_mapped:
                    raise ValueError
        if (require_https or not local) and url.scheme != 'https':
            raise ValueError
        return value.rstrip('/'), local
    except (ValueError, TypeError):
        raise ProviderError('Invalid endpoint configuration (strict URL/HTTPS required)') from None


def resolve_provider_config():
    backend = os.environ.get('RM_BACKEND')
    legacy = os.environ.get('CLAUDE_CODE_USE_VERTEX')
    if legacy not in (None, '', '0', '1'):
        raise ProviderError('Invalid legacy provider selector')
    if backend is None and legacy == '1':
        backend = 'vertex'
        log.warning('Legacy Vertex selector: migrate to RM_BACKEND=vertex')
    if backend not in ('anthropic', 'vertex', 'hendrix', 'claude-cli', 'openai-compatible'):
        raise ProviderError('RM_BACKEND must explicitly select a supported provider')
    if legacy == '1' and backend != 'vertex' or legacy == '0' and backend == 'vertex':
        raise ProviderError('Conflicting provider selectors')
    model = _model_value('RM_MODEL')
    config = {'backend': backend, 'model': model,
              'task_model': _model_value('RM_TASK_MODEL', model),
              'tag_model': _model_value('RM_TAG_MODEL', model),
              'diagram_model': _model_value('RM_DIAGRAM_MODEL', model)}
    # Vertex interpolates model IDs into a URL path, unlike JSON-based APIs.
    if backend == 'vertex' and any(not re.fullmatch(r'[A-Za-z0-9._@-]+', config[key]) for key in ('model', 'task_model', 'tag_model', 'diagram_model')):
        raise ProviderError('Invalid Vertex model identifier (URL path delimiters forbidden)')
    if backend == 'openai-compatible':
        endpoint, local = _endpoint(os.environ.get('RM_OPENAI_BASE_URL'))
        config['base_url'] = endpoint
        if not local:
            _require_remote_consent()
    else:
        _require_remote_consent()
    return config


def _http_client():
    import httpx
    _quiet_provider_logs()
    # No environment proxies or redirect forwarding, even with remote consent.
    return httpx.Client(trust_env=False, follow_redirects=False, timeout=60.0)


def create_client(config):
    # Resolve again at the boundary: callers cannot bypass consent with a dict.
    selected = resolve_provider_config()
    if selected != config:
        raise ProviderError('Provider configuration changed before client creation')
    _quiet_provider_logs()
    backend = config['backend']
    try:
        if backend == 'openai-compatible':
            return OpenAICompatibleClient()
        if backend == 'claude-cli':
            return ClaudeCLIClient()
        if backend == 'hendrix':
            return HendrixClient()
        import anthropic
        if backend == 'vertex':
            project = os.environ.get('ANTHROPIC_VERTEX_PROJECT_ID')
            region = os.environ.get('CLOUD_ML_REGION', 'europe-west1')
            if not project or not re.fullmatch(r'[a-z0-9-]+', project) or not re.fullmatch(r'[a-z0-9-]+', region):
                raise ProviderError('Invalid Vertex project/region configuration')
            default = 'https://aiplatform.googleapis.com/v1' if region == 'global' else f'https://{region}-aiplatform.googleapis.com/v1'
            endpoint, _ = _endpoint(os.environ.get('ANTHROPIC_VERTEX_BASE_URL', default), require_https=True)
            return anthropic.AnthropicVertex(project_id=project, region=region, base_url=endpoint,
                                              http_client=_http_client(), max_retries=0, timeout=60.0)
        endpoint, _ = _endpoint(os.environ.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com'), require_https=True)
        return anthropic.Anthropic(base_url=endpoint, http_client=_http_client(), max_retries=0, timeout=60.0)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError('Provider client initialization failed') from None


def _text_message(text):
    from types import SimpleNamespace
    if not isinstance(text, str) or not text.strip():
        raise ProviderError('Provider returned no usable text')
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=text)])


class OpenAICompatibleClient:
    """Only the chat-completions text/image subset used by this pipeline."""

    def __init__(self):
        config = resolve_provider_config()
        if config['backend'] != 'openai-compatible':
            raise ProviderError('OpenAI-compatible provider not selected')
        self.base_url = config['base_url']
        self._client = _http_client()
        self.messages = self

    def create(self, *, model, max_tokens, system=None, messages, **_):
        try:
            converted = []
            if system:
                text = system if isinstance(system, str) else '\n\n'.join(b['text'] for b in system if b['type'] == 'text')
                converted.append({'role': 'system', 'content': text})
            for message in messages:
                role, content = message['role'], message['content']
                if role not in ('user', 'assistant'):
                    raise ProviderError('Unsupported message role')
                if not isinstance(content, str):
                    blocks = []
                    for block in content:
                        if block['type'] == 'text':
                            blocks.append({'type': 'text', 'text': block['text']})
                        elif block['type'] in ('image', 'document'):
                            source = block['source']
                            if source['type'] != 'base64':
                                raise ProviderError('Unsupported attachment source')
                            images = HendrixClient._pdf_to_images(block) if source['media_type'] == 'application/pdf' else [block]
                            for image in images:
                                source = image['source']
                                if source['media_type'] not in ('image/png', 'image/jpeg', 'image/webp'):
                                    raise ProviderError('Unsupported attachment media type')
                                blocks.append({'type': 'image_url', 'image_url': {'url': f"data:{source['media_type']};base64,{source['data']}"}})
                        else:
                            raise ProviderError('Unsupported message block')
                    content = blocks
                converted.append({'role': role, 'content': content})
            headers = {}
            key = os.environ.get('RM_OPENAI_API_KEY')
            if key:
                headers['Authorization'] = 'Bearer ' + key
            response = self._client.post(self.base_url + '/chat/completions',
                                         json={'model': model, 'max_tokens': max_tokens, 'messages': converted}, headers=headers)
            if not 200 <= response.status_code < 300:
                raise ProviderError('Model HTTP request failed')
            content = response.json()['choices'][0]['message']['content']
            if isinstance(content, list):
                if not content or any(not isinstance(b, dict) or b.get('type') != 'text' or not isinstance(b.get('text'), str) for b in content):
                    raise ProviderError('Provider returned invalid text response')
                content = '\n'.join(b['text'] for b in content)
            return _text_message(content)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('Model request or response failed') from None


class ClaudeCLIClient:
    """Drop-in replacement for the anthropic SDK client that shells out to the
    Claude Code CLI (`claude -p`), authenticating via the local subscription
    login instead of an API key or Vertex project (RM_BACKEND=claude-cli).

    Supports the messages.create subset this script uses: system text plus user
    content made of text / base64 image / base64 document blocks. Base64 blocks
    are written to temp files that the CLI reads with its Read tool.
    """

    # Vertex-era vars must not leak into the spawned CLI or it will try the
    # deprecated GCP route and 403.
    _SCRUB_ENV = (
        "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
        "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
        "HENDRIX_API_KEY", "RM_OPENAI_API_KEY", "RM_OPENAI_BASE_URL",
        "RM_HENDRIX_URL", "GOOGLE_APPLICATION_CREDENTIALS",
    )
    _EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "application/pdf": ".pdf"}

    def __init__(self, claude_bin: str | None = None, timeout: int = 900):
        _require_remote_consent()
        _quiet_provider_logs()
        self.claude_bin = claude_bin or os.environ.get("RM_CLAUDE_BIN") or shutil.which("claude")
        if not self.claude_bin:
            raise RuntimeError("claude CLI not found on PATH; install Claude Code or set RM_CLAUDE_BIN")
        self.timeout = timeout
        self.messages = self

    @staticmethod
    def _cli_model(model: str) -> str:
        # Model identifiers are opaque; unsupported identifiers fail at the CLI.
        return model

    def create(self, *, model: str, max_tokens: int = 4096, system=None, messages=None, **_):
        system_text = system if isinstance(system, str) else "\n\n".join(
            b["text"] for b in (system or []) if isinstance(b, dict) and b.get("type") == "text"
        )
        texts: list[str] = []
        attachments: list[tuple[bytes, str]] = []  # (data, media_type)
        for msg in messages or []:
            texts.append("ROLE: " + msg["role"])
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
                continue
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    texts.append(block["text"])
                elif btype in ("image", "document") and block.get("source", {}).get("type") == "base64":
                    src = block["source"]
                    attachments.append((base64.standard_b64decode(src["data"]), src["media_type"]))

        with tempfile.TemporaryDirectory(prefix="rm-ocr-") as tmp:
            paths = []
            for i, (data, media_type) in enumerate(attachments, 1):
                p = Path(tmp) / f"input-{i:02d}{self._EXT.get(media_type, '.bin')}"
                p.write_bytes(data)
                paths.append(str(p))
            parts = [system_text]
            if paths:
                parts.append(
                    "Input files (read each with the Read tool, in this order, before responding):\n"
                    + "\n".join(paths)
                )
            parts.extend(texts)
            parts.append("Respond with the final output only — no preamble, no commentary on the files.")
            prompt = "\n\n".join(p for p in parts if p)

            env = {k: v for k, v in os.environ.items() if k not in self._SCRUB_ENV}
            try:
                result = subprocess.run(
                    [self.claude_bin, "-p",
                     "--output-format", "text",
                     "--model", self._cli_model(model),
                     "--allowedTools", "Read",
                     "--settings", '{"disableAllHooks": true}'],
                    input=prompt, capture_output=True, text=True, timeout=self.timeout, env=env, cwd=tmp,
                )
            except (subprocess.SubprocessError, OSError):
                raise ProviderError("claude CLI execution failed or timed out") from None
        if result.returncode != 0:
            raise ProviderError(f"claude CLI failed (exit {result.returncode})")
        return _text_message(result.stdout.strip())


class HendrixClient:
    """Anthropic SDK client pointed at a Hendrix AI Gateway model route
    (RM_BACKEND=hendrix), e.g. self-hosted GLM-5.3 Flash.

    The gateway speaks Anthropic Messages but rejects `document` blocks, so PDF
    blocks are rendered to per-page PNG images first. Thinking is disabled and
    any thinking blocks dropped so callers can keep reading `content[0].text`.
    """

    DEFAULT_URL = "https://hendrix-genai.spotify.net/taskforce/glm-5-3-flash"

    def __init__(self):
        _require_remote_consent()
        _quiet_provider_logs()
        import anthropic
        api_key = os.environ.get("HENDRIX_API_KEY")
        if not api_key:
            raise RuntimeError("HENDRIX_API_KEY must be set for RM_BACKEND=hendrix")
        self.base_url, _ = _endpoint(os.environ.get("RM_HENDRIX_URL", self.DEFAULT_URL), require_https=True)
        self._client = anthropic.Anthropic(base_url=self.base_url, api_key=api_key, auth_token="",
                                           http_client=_http_client(), max_retries=0, timeout=60.0)
        # Empty token prevents SDK environment inference; None omits Bearer entirely.
        self._client.auth_token = None
        self.messages = self

    @staticmethod
    def _pdf_to_images(block: dict) -> list[dict]:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=base64.b64decode(block["source"]["data"]), filetype="pdf")
        images = []
        for page in doc:
            png = page.get_pixmap(dpi=150).tobytes("png")
            images.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.standard_b64encode(png).decode("utf-8")},
            })
        return images

    def create(self, *, messages, **kwargs):
        converted = []
        for msg in messages:
            content = msg["content"]
            if isinstance(content, list):
                new_content = []
                for block in content:
                    if block.get("type") == "document":
                        new_content.extend(self._pdf_to_images(block))
                    else:
                        new_content.append(block)
                msg = {**msg, "content": new_content}
            converted.append(msg)
        # GLM reasons by default and can burn the whole max_tokens budget on
        # long PDFs before emitting any text; OCR doesn't benefit from it.
        kwargs.setdefault("thinking", {"type": "disabled"})
        message = self._client.messages.create(messages=converted, **kwargs)
        message.content = [b for b in message.content if getattr(b, "type", "") == "text"] or message.content
        return message


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


def _batch_stat(rmapi_bin: str, paths: list[str]) -> list[dict | None]:
    """Stat many paths in one rmapi shell session.

    Each separate rmapi invocation re-authenticates and re-fetches the whole
    document tree, and ~30 of those in a row get HTTP 429 from the reMarkable
    cloud. The shell answers each `stat` with either a JSON object or an
    `Error:` line, in order, so results map back to paths positionally.
    """
    if not paths:
        return []
    commands = "".join('stat "%s"\n' % p.replace('"', '\\"') for p in paths)
    result = subprocess.run([rmapi_bin], input=commands, capture_output=True, text=True)
    decoder = json.JSONDecoder()
    out, pos, results = result.stdout, 0, []
    while len(results) < len(paths):
        brace = out.find("{", pos)
        err = out.find("Error:", pos)
        if brace == -1 and err == -1:
            break
        if err != -1 and (brace == -1 or err < brace):
            results.append(None)
            pos = err + len("Error:")
            continue
        try:
            meta, pos = decoder.raw_decode(out, brace)
            results.append(meta)
        except json.JSONDecodeError:
            results.append(None)
            pos = brace + 1
    if len(results) < len(paths):
        log.warning("rmapi batch stat returned %d/%d results", len(results), len(paths))
    return results + [None] * (len(paths) - len(results))


def list_notebooks(rmapi_bin: str, watch_path: str) -> list[dict]:
    """List all notebooks under watch_path using rmapi find + stat."""
    # Always search from root so paths are absolute
    result = subprocess.run(
        [rmapi_bin, "find", "/", ".*"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("rmapi find failed (exit %d). Run `rmapi` to authenticate.", result.returncode)
        sys.exit(1)

    watch_norm = watch_path.rstrip("/")
    paths = []
    for line in result.stdout.splitlines():
        line = line.lstrip().rstrip("\r")
        if not line.startswith("[f]"):
            continue
        # Extract path: "[f] /path/to/notebook". Keep trailing spaces: they
        # are part of the name, and rmapi can't find the notebook without them.
        path = line[4:].lstrip()

        # Skip trash
        if path.startswith("/trash/"):
            continue

        # Filter by watch_path
        if watch_norm != "" and watch_norm != "/":
            if path != watch_norm and not path.startswith(watch_norm + "/"):
                continue
        paths.append(path)

    notebooks = []
    for path, meta in zip(paths, _batch_stat(rmapi_bin, paths)):
        if meta is None:
            log.warning("Failed to stat %s, skipping", path)
            continue

        notebooks.append({
            "id": meta["ID"],
            "name": meta["Name"],
            "version": meta["Version"],
            "modified": meta["ModifiedClient"],
            "path": path,
            "starred": meta.get("Starred", False),
            "tags": meta.get("Tags") or [],
            "type": meta.get("Type", "DocumentType"),
        })

    return notebooks


def _walk_directory(rmapi_bin: str, path: str, notebooks: list[dict]) -> None:
    """Recursively walk a reMarkable directory, collecting notebooks (legacy, for tests)."""
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


def _parse_rm_v5(data: bytes) -> list[dict]:
    """Parse a .rm v5 binary file and return a list of stroke dicts."""
    V5_HEADER = b'reMarkable .lines file, version=5'
    offset = len(V5_HEADER) + 10  # header + padding

    COLOR_MAP = {0: "black", 1: "#808080", 2: "white", 3: "#FFD700", 4: "#0000FF", 5: "#FF0000"}
    strokes = []

    num_layers = struct.unpack_from('<I', data, offset)[0]
    offset += 4

    for _ in range(num_layers):
        num_lines = struct.unpack_from('<I', data, offset)[0]
        offset += 4

        for _ in range(num_lines):
            brush_type, color, _unknown, brush_size = struct.unpack_from('<IIIf', data, offset)
            offset += 16
            _unknown2 = struct.unpack_from('<I', data, offset)[0]  # v5-specific field
            offset += 4
            num_points = struct.unpack_from('<I', data, offset)[0]
            offset += 4

            points = []
            for _ in range(num_points):
                x, y, speed, direction, width, pressure = struct.unpack_from('<ffffff', data, offset)
                offset += 24
                points.append({"x": x, "y": y, "width": width, "pressure": pressure})

            strokes.append({
                "color": COLOR_MAP.get(color, "black"),
                "width": max(1.0, brush_size * 2.0),
                "points": points,
            })

    return strokes


def _render_rm_to_svg(rm_path: Path) -> str:
    """Parse a .rm file (v5 or v6) and render strokes to SVG string."""
    data = rm_path.read_bytes()

    # Color enum mapping
    COLOR_MAP = {0: "black", 1: "#808080", 2: "white", 3: "#FFD700", 4: "#0000FF", 5: "#FF0000"}

    # Detect format version
    if data.startswith(b'reMarkable .lines file, version=5'):
        parsed_strokes = _parse_rm_v5(data)
        # v5 coordinates are absolute (0-based, top-left origin)
        cx, cy = 0, 0
    else:
        # v6 format via rmscene — coordinates are centered (x=0 is page center)
        cx, cy = RM_WIDTH / 2, RM_HEIGHT / 2
        blocks = list(read_blocks(io.BytesIO(data)))
        line_blocks = [b for b in blocks if isinstance(b, SceneLineItemBlock)]
        parsed_strokes = []
        for lb in line_blocks:
            item = lb.item
            if item is None or item.value is None:
                continue
            line = item.value
            if not line.points or len(line.points) < 2:
                continue
            color_val = line.color.value if hasattr(line.color, 'value') else 0
            parsed_strokes.append({
                "color": COLOR_MAP.get(color_val, "black"),
                "width": max(1.0, line.thickness_scale * 2.0),
                "points": [{"x": pt.x, "y": pt.y} for pt in line.points],
            })

    # Render strokes to SVG
    min_y, max_y = float('inf'), float('-inf')
    strokes_svg = []
    for stroke in parsed_strokes:
        points = stroke["points"]
        if len(points) < 2:
            continue

        path_data = f"M {points[0]['x'] + cx:.1f} {points[0]['y'] + cy:.1f}"
        for pt in points[1:]:
            path_data += f" L {pt['x'] + cx:.1f} {pt['y'] + cy:.1f}"

        for pt in points:
            min_y = min(min_y, pt['y'] + cy)
            max_y = max(max_y, pt['y'] + cy)

        strokes_svg.append(
            f'<path d="{path_data}" stroke="{stroke["color"]}" '
            f'stroke-width="{stroke["width"]:.1f}" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # Use actual content height if it exceeds the standard page
    margin = 50
    content_top = max(0, min_y - margin) if min_y != float('inf') else 0
    content_bottom = max(RM_HEIGHT, max_y + margin) if max_y != float('-inf') else RM_HEIGHT
    page_height = int(content_bottom - content_top)

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 {int(content_top)} {RM_WIDTH} {page_height}" '
        f'width="{RM_WIDTH}" height="{page_height}">'
        f'<rect x="0" y="{int(content_top)}" width="{RM_WIDTH}" height="{page_height}" fill="white"/>'
        + "\n".join(strokes_svg)
        + '</svg>'
    )
    return svg


def parse_page_ranges(value: str) -> tuple[tuple[int, int], ...]:
    """Parse closed 1-based ranges without expanding potentially huge intervals."""
    ranges = []
    for part in value.split(","):
        match = re.fullmatch(r"\s*([0-9]+)(?:-([0-9]+))?\s*", part)
        if not match:
            raise ValueError("--pages requires 1-based numbers or closed ranges, e.g. 1,3-5")
        start = int(match[1])
        end = int(match[2] or match[1])
        if start < 1 or end < start:
            raise ValueError("--pages requires positive, non-reversed ranges")
        ranges.append((start, end))
    return tuple(ranges)


@dataclass(frozen=True)
class Selection:
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    pages: tuple[tuple[int, int], ...] = ()

    def matches(self, number: int, tags: set[str]) -> bool:
        normalize = lambda values: {v.strip().casefold() for v in values}
        tags = normalize(tags)
        include = normalize(self.include_tags)
        exclude = normalize(self.exclude_tags)
        return (not include or bool(include & tags)) and not (exclude & tags) and (
            not self.pages or any(start <= number <= end for start, end in self.pages))


def _native_selection_metadata(content: dict) -> tuple[list[str], list[set[str]]]:
    """Resolve native order and tag associations; never infer ordering from files."""
    if content.get("fileType", "notebook") not in ("notebook", ""):
        raise ValueError("Unsupported selection: PDF/EPUB archive mapping is not supported")
    deleted = set()
    if "cPages" in content:
        entries = content["cPages"]["pages"]
        all_ids = [page["id"] for page in entries]
        if any(not isinstance(pid, str) or not pid for pid in all_ids) or len(set(all_ids)) != len(all_ids):
            raise ValueError("Unsupported selection: duplicate/malformed cPages page IDs")
        live = []
        for page in entries:
            if page.get("deleted", {}).get("value") is True:
                deleted.add(page["id"])
            elif page.get("deleted", {}).get("value", False) is not False:
                raise ValueError("Unsupported selection: ambiguous deleted page metadata")
            else:
                live.append(page)
        ranks = [p.get("idx", {}).get("value") for p in live]
        if any(not isinstance(rank, str) or not rank for rank in ranks) or len(set(ranks)) != len(ranks):
            raise ValueError("Unsupported selection: missing/duplicate cPages ordering indices")
        ids = [p["id"] for p in sorted(live, key=lambda p: p["idx"]["value"])]
    else:
        ids = content.get("pages")
    if not isinstance(ids, list) or any(not isinstance(pid, str) or not pid for pid in ids) or len(set(ids)) != len(ids):
        raise ValueError("Unsupported selection: missing/duplicate native page IDs")

    def tag_names(values):
        if not isinstance(values, list) or any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in values):
            raise ValueError("Unsupported selection: malformed native tags")
        return {t["name"] for t in values}

    notebook_tags = tag_names(content.get("tags", []))
    tags = {pid: set(notebook_tags) for pid in ids}
    page_tags = content.get("pageTags", [])
    tag_names(page_tags)  # Validate even when no include filters were supplied.
    for tag in page_tags:
        pid = tag.get("pageId")
        if pid in deleted:
            continue
        if pid not in tags:
            raise ValueError("Unsupported selection: unknown/missing pageId in native pageTags")
        tags[pid].add(tag["name"])
    return ids, [tags[pid] for pid in ids]


def _prepare_selected_pages(rmapi_bin: str, notebook_path: str, output: Path,
                            selection: Selection) -> tuple[list[Path | None], list[str], list[str]]:
    """Download locally, then expose only selected native strokes or PDF subsets."""
    result = subprocess.run([rmapi_bin, "get", notebook_path], capture_output=True, text=True, cwd=str(output))
    if result.returncode:
        raise ValueError("Selection download failed")
    archives = list(output.glob("*.zip")) + list(output.glob("*.rmdoc"))
    pdfs = list(output.glob("*.pdf"))
    pdf_metadata = None
    if len(archives) == 1 and not pdfs:
        with zipfile.ZipFile(archives[0]) as archive:
            names = archive.namelist()
            manifests = [name for name in names if name.endswith(".content")]
            if len(manifests) != 1 or len(set(names)) != len(names):
                raise ValueError("Unsupported selection: ambiguous archive metadata")
            content = json.loads(archive.read(manifests[0]))
            if content.get("fileType") == "pdf":
                # ponytail: PDF archives with annotation/redirection metadata need a
                # verified compositor before we can safely honor native page tags.
                if selection.include_tags or selection.exclude_tags or any(name.endswith(".rm") for name in names) or "cPages" in content:
                    raise ValueError("Unsupported selection: annotated/reordered/tag-filtered PDF archive")
                pdf_metadata = content
                originals = [name for name in names if name.lower().endswith(".pdf")]
                if len(originals) != 1:
                    raise ValueError("Unsupported selection: ambiguous PDF archive")
                pdf = output / "original.pdf"
                pdf.write_bytes(archive.read(originals[0]))
                pdfs = [pdf]
            else:
                ids, tags = _native_selection_metadata(content)
                paths = []
                output_tags = set()
                for i, pid in enumerate(ids):
                    if not selection.matches(i + 1, tags[i]):
                        paths.append(None)
                        continue
                    members = [name for name in names if Path(name).name == pid + ".rm"]
                    if len(members) != 1:
                        raise ValueError("Unsupported selection: missing/ambiguous native stroke page")
                    page = output / f"selected-{i+1}.rm"
                    page.write_bytes(archive.read(members[0]))
                    paths.append(page)
                    output_tags.update(tags[i])
                return paths, ids, sorted(output_tags)
    elif archives or len(pdfs) != 1:
        raise ValueError("Unsupported selection: expected one native archive or ordinary PDF")
    if selection.include_tags or selection.exclude_tags:
        raise ValueError("Unsupported selection: native tags unavailable for ordinary PDF")
    import fitz
    with fitz.open(pdfs[0]) as document:
        if pdf_metadata is not None:
            count = len(document)
            pages = pdf_metadata.get("pages", [])
            mapping = pdf_metadata.get("redirectionPageMap", [])
            if (pdf_metadata.get("pageCount", count) != count
                    or pdf_metadata.get("originalPageCount", count) != count
                    or (pages and (not isinstance(pages, list) or len(pages) != count
                                  or any(not isinstance(pid, str) for pid in pages)
                                  or len(set(pages)) != count))
                    or ((pages or mapping) and (mapping != list(range(count))
                                               or any(type(index) is not int for index in mapping)))):
                raise ValueError("Unsupported selection: PDF archive needs explicit identity page mapping")
        paths = []
        # Page number is the stable identity available for an ordinary PDF. Hashes
        # still invalidate cache when content at that number changes.
        ids = [f"pdf:{i+1}" for i in range(len(document))]
        for i in range(len(document)):
            if not selection.matches(i + 1, set()):
                paths.append(None)
                continue
            path = output / f"selected-{i+1}.pdf"
            with fitz.open() as subset:
                subset.insert_pdf(document, from_page=i, to_page=i)
                subset.save(path, garbage=4, deflate=True, no_new_id=True)
            paths.append(path)
    return paths, ids, []


def _extract_content_tags(zip_path: Path, notebook_path: str) -> None:
    """Extract pageTags from a .content file inside a zip/rmdoc and cache them."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith(".content"):
                    content = json.loads(zf.read(name))
                    page_tags = content.get("pageTags", [])
                    tag_names = list({t["name"] for t in page_tags if "name" in t})
                    if tag_names:
                        _content_tags[notebook_path] = tag_names
                    break
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        pass


def _extract_rm_pages(rmapi_bin: str, notebook_path: str, output_dir: Path) -> list[Path] | None:
    """Download notebook and extract ordered .rm files. Returns None for PDF-based notebooks."""
    # Try geta first (works for PDF-based notebooks)
    subprocess.run(
        [rmapi_bin, "geta", notebook_path],
        capture_output=True, text=True,
        cwd=str(output_dir),
    )

    # Check for PDF output — no per-page tracking for PDFs
    pdfs = list(output_dir.glob("*.pdf"))
    if pdfs:
        log.info("Exported annotated PDF: %s", pdfs[0].name)
        # Still try to extract pageTags from any .rmdoc left alongside the PDF
        rmdocs = list(output_dir.glob("*.rmdoc"))
        if rmdocs:
            _extract_content_tags(rmdocs[0], notebook_path)
        # For PDF-based notebooks (uploaded PDFs with annotations), geta produces
        # a tiny annotations-only PDF + a zip containing the original full PDF.
        # Extract the original so callers can use it for source display and cropping.
        zips = list(output_dir.glob("*.zip"))
        if zips:
            try:
                with zipfile.ZipFile(zips[0]) as zf:
                    for name in zf.namelist():
                        if name.lower().endswith(".pdf"):
                            pdf_data = zf.read(name)
                            # Only use if substantially larger than the annotations stub
                            if len(pdf_data) > pdfs[0].stat().st_size:
                                original_path = output_dir / "original.pdf"
                                original_path.write_bytes(pdf_data)
                                log.info("Extracted original PDF from zip: %d bytes", len(pdf_data))
                            break
            except (zipfile.BadZipFile, KeyError):
                pass
        return None

    # geta may have downloaded a zip/.rmdoc with .rm files instead
    zips = list(output_dir.glob("*.zip")) + list(output_dir.glob("*.rmdoc"))
    if not zips:
        subprocess.run(
            [rmapi_bin, "get", notebook_path],
            capture_output=True, text=True,
            cwd=str(output_dir),
        )
        zips = list(output_dir.glob("*.zip")) + list(output_dir.glob("*.rmdoc"))

    if not zips:
        log.error("No PDF or zip found after exporting %s", notebook_path)
        return []

    # Extract zip
    extract_dir = output_dir / "extracted"
    with zipfile.ZipFile(zips[0]) as zf:
        zf.extractall(extract_dir)

    # Find page order from .content file
    content_files = list(extract_dir.glob("*.content"))
    page_ids = []
    if content_files:
        try:
            content = json.loads(content_files[0].read_text())
            pages = content.get("cPages", {}).get("pages", [])
            page_ids = [
                p["id"] for p in pages
                if not p.get("deleted", {}).get("value")
            ]
        except (json.JSONDecodeError, KeyError):
            pass

    # Extract pageTags from .content (reMarkable stores user tags here, not in API metadata)
    _extract_content_tags(zips[0], notebook_path)

    # Find and order .rm files
    rm_files = list(extract_dir.rglob("*.rm"))
    if not rm_files:
        log.error("No .rm files found in zip for %s", notebook_path)
        return []

    if page_ids:
        ordered = []
        for pid in page_ids:
            for rm in rm_files:
                if rm.stem == pid:
                    ordered.append(rm)
                    break
        rm_files = ordered if ordered else rm_files

    return rm_files


def export_notebook(rmapi_bin: str, notebook_path: str, name: str, output_dir: Path) -> list[Path]:
    """Export a notebook. Returns list of renderable files (PDFs, SVGs, or PNGs)."""
    rm_pages = _extract_rm_pages(rmapi_bin, notebook_path, output_dir)

    if rm_pages is None:
        # PDF-based notebook
        return list(output_dir.glob("*.pdf"))

    if not rm_pages:
        return []

    # Render each page to SVG
    svg_paths = []
    for i, rm_file in enumerate(rm_pages):
        svg_content = _render_rm_to_svg(rm_file)
        svg_path = output_dir / f"page_{i+1}.svg"
        svg_path.write_text(svg_content)
        svg_paths.append(svg_path)
        log.info("Rendered page %d from %s", i + 1, rm_file.name)

    return svg_paths


# Keep old function name for test compatibility
def export_notebook_pdf(rmapi_bin: str, notebook_path: str, name: str, output_dir: Path) -> Path | None:
    """Export a notebook as annotated PDF using rmapi geta. Returns path to PDF or None on failure."""
    result = subprocess.run(
        [rmapi_bin, "geta", "-o", str(output_dir), notebook_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("rmapi export failed (exit %d)", result.returncode)
        return None

    pdf_path = output_dir / f"{name}.pdf"
    if not pdf_path.exists():
        pdfs = list(output_dir.glob("*.pdf"))
        if pdfs:
            pdf_path = pdfs[0]
        else:
            log.error("No PDF found after exporting %s", notebook_path)
            return None
    return pdf_path


def is_blank_page(svg_path: Path) -> bool:
    """Check if an SVG page is blank (no strokes). Uses path element check with PIL fallback."""
    svg_text = svg_path.read_text()
    if "<path " not in svg_text:
        return True
    # Fallback: render to PNG and check pixel variance
    try:
        from PIL import Image, ImageStat
        png_data = _svg_to_png(svg_path)[0]  # first tile is sufficient for blank check
        img = Image.open(io.BytesIO(png_data)).convert("L")
        if ImageStat.Stat(img).stddev[0] < 1.0:
            return True
    except Exception:
        pass
    return False


TRANSCRIPTION_PROMPT = """Transcribe all handwritten text in this document to clean markdown.

The images are numbered in order starting from 1 (image 1, image 2, etc.).

Rules:
- Infer structure: use headings, bullet lists, numbered lists as appropriate
- Detect headings: text that is underlined, circled, or written larger should be treated as headings. Use ## for major topics and ### for subtopics.
- For diagrams, sketches, or graphical elements that cannot be represented as text, output:
  > [Diagram(page=P, top=T, bottom=B): description]
  where P is the image number (1-based), T and B are the vertical position as
  percentages (0=top of image, 100=bottom). This will be used to crop and embed
  that region of the source image.
- Mark illegible sections as *[illegible]*
- Output ONLY the markdown transcription, no preamble or explanation"""


def transcribe_pdf(client, pdf_path: Path, model: str, context: str = "", system_prompt: str | None = None) -> str:
    """Send a PDF to Claude for handwriting transcription. Returns markdown string."""
    pdf_data = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")

    prompt = system_prompt or TRANSCRIPTION_PROMPT
    user_content: list[dict] = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_data,
            },
        },
    ]
    if context:
        user_content.append({"type": "text", "text": f"Context:\n{context}"})

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text


# Claude API: images are auto-resized so the long edge ≤ 1568px.  For a
# standard reMarkable page (1404×1872) this is fine — width stays ~1175px.
# But for very tall scrollable pages (e.g. 1404×10000) the width shrinks to
# ~220px, making text unreadable.  We tile tall pages so each chunk keeps
# enough width to remain legible after the API's auto-resize.
API_LONG_EDGE = 1568
MIN_READABLE_WIDTH = 800  # if API resize would push width below this, tile


def _svg_to_png(svg_path: Path) -> list[bytes]:
    """Convert an SVG file to PNG byte chunks using cairosvg.

    For tall SVGs where API auto-resize would shrink the width below
    MIN_READABLE_WIDTH, the image is split into vertical tiles sized so
    that each tile's width stays readable after resize.
    Returns a list of PNG byte strings (one per tile).
    """
    import cairosvg
    from PIL import Image

    svg_text = svg_path.read_text()
    match = re.search(r'height="(\d+)"', svg_text)
    svg_height = int(match.group(1)) if match else RM_HEIGHT
    match = re.search(r'width="(\d+)"', svg_text)
    svg_width = int(match.group(1)) if match else RM_WIDTH

    # What would the API do with this image?  It resizes so long_edge ≤ 1568.
    long_edge = max(svg_width, svg_height)
    if long_edge <= API_LONG_EDGE:
        # Fits as-is, no resize needed
        result = cairosvg.svg2png(url=str(svg_path), output_width=svg_width)
        assert isinstance(result, bytes)
        return [result]

    api_scale = API_LONG_EDGE / long_edge
    api_width = svg_width * api_scale

    if api_width >= MIN_READABLE_WIDTH:
        # API resize keeps width readable — send as single image
        result = cairosvg.svg2png(url=str(svg_path), output_width=svg_width)
        assert isinstance(result, bytes)
        return [result]

    # Width would be too narrow after API resize.  Tile so each chunk's
    # long edge ≤ API_LONG_EDGE with width = svg_width (i.e. height ≤ API_LONG_EDGE).
    tile_height = API_LONG_EDGE

    # Render full-resolution PNG, then split into vertical tiles
    full_png = cairosvg.svg2png(url=str(svg_path), output_width=svg_width, output_height=svg_height)
    assert isinstance(full_png, bytes)

    full_img = Image.open(io.BytesIO(full_png))
    tiles = []
    for y_offset in range(0, svg_height, tile_height):
        box = (0, y_offset, svg_width, min(y_offset + tile_height, svg_height))
        tile = full_img.crop(box)
        buf = io.BytesIO()
        tile.save(buf, format="PNG")
        tiles.append(buf.getvalue())

    return tiles


def _svg_to_full_png(svg_path: Path) -> bytes:
    """Convert SVG to a single full-resolution PNG, stitching tiles if needed."""
    from PIL import Image
    chunks = _svg_to_png(svg_path)
    if len(chunks) == 1:
        return chunks[0]
    # Stitch tiles vertically
    images = [Image.open(io.BytesIO(c)) for c in chunks]
    total_height = sum(img.height for img in images)
    width = images[0].width
    stitched = Image.new("RGB", (width, total_height))
    y = 0
    for img in images:
        stitched.paste(img, (0, y))
        y += img.height
    buf = io.BytesIO()
    stitched.save(buf, format="PNG")
    return buf.getvalue()


def _encode_page_image(page_path: Path) -> list[tuple[str, str]]:
    """Encode a page file to base64. Returns list of (data, media_type) tuples.

    For tall SVGs this may return multiple tiles to preserve resolution.
    """
    suffix = page_path.suffix.lower()
    if suffix == ".svg":
        png_chunks = _svg_to_png(page_path)
        return [
            (base64.standard_b64encode(chunk).decode("utf-8"), "image/png")
            for chunk in png_chunks
        ]
    elif suffix == ".png":
        return [(base64.standard_b64encode(page_path.read_bytes()).decode("utf-8"), "image/png")]
    else:
        return [(base64.standard_b64encode(page_path.read_bytes()).decode("utf-8"), "application/pdf")]


SINGLE_PAGE_PROMPT = """Transcribe all handwritten text in this page to clean markdown.

Rules:
- Infer structure: use headings, bullet lists, numbered lists as appropriate
- Detect headings: text that is underlined, circled, or written larger should be treated as headings. Use ## for major topics and ### for subtopics.
- For diagrams, sketches, or graphical elements that cannot be represented as text, output:
  > [Diagram(page=1, top=T, bottom=B): description]
  where T and B are the vertical position as percentages (0=top, 100=bottom).
  This will be used to crop and embed that region of the source image.
- Mark illegible sections as *[illegible]*
- Output ONLY the markdown transcription, no preamble or explanation"""


def transcribe_page(client, page_path: Path, model: str, context: str = "", system_prompt: str | None = None) -> str:
    """Send a single page image to Claude for transcription. Returns markdown string."""
    page_tiles = _encode_page_image(page_path)

    prompt = system_prompt or SINGLE_PAGE_PROMPT
    user_content: list[dict] = []
    for page_data, media_type in page_tiles:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": page_data},
        })
    if context:
        user_content.append({"type": "text", "text": f"Context:\n{context}"})

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text


def transcribe_pages(client, page_paths: list[Path], model: str) -> str:
    """Send page images (SVG/PNG) to Claude for transcription. Returns markdown string."""
    content = []
    for page_path in page_paths:
        for page_data, media_type in _encode_page_image(page_path):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": page_data},
            })

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        system=[{"type": "text", "text": TRANSCRIPTION_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text


DIAGRAM_RE = re.compile(
    r'> \[Diagram\(page=(\d+),\s*top=(\d+),\s*bottom=(\d+)\):\s*(.+?)\]',
    re.DOTALL,
)

DIAGRAM_CLASSIFY_PROMPT = """Classify this handwritten diagram into exactly one category.
Reply with ONLY the category name, nothing else.

Categories: flowchart, sequence, mindmap, table, architecture, sketch"""

DIAGRAM_TYPES: dict[str, str] = {
    "flowchart": "Convert this flowchart to Mermaid syntax using 'flowchart TD' or 'flowchart LR'. Preserve all node labels and connections.",
    "sequence": "Convert this sequence diagram to Mermaid syntax. Preserve all actors, messages, and ordering.",
    "mindmap": "Convert this mind map to Mermaid mindmap syntax. Preserve the hierarchy and all labels.",
    "table": "Convert this table to a markdown table. Use | column | separators and alignment rows.",
    "architecture": "Convert this architecture/system diagram to Mermaid syntax. Preserve all components, connections, and labels.",
    "sketch": "This is a freeform sketch. Output Excalidraw JSON (the 'elements' array) to reproduce it.",
}


def _renumber_diagrams(markdown: str, page_number: int) -> str:
    """Change only parsed marker page fields, never literal transcribed page= text."""
    return DIAGRAM_RE.sub(lambda match: match[0].replace(
        f"page={match[1]},", f"page={page_number},", 1), markdown)


def _classify_diagram(client, crop_image: bytes, model: str) -> str:
    """Classify a diagram image into a category. Returns category string."""
    image_data = base64.standard_b64encode(crop_image).decode("utf-8")
    message = client.messages.create(
        model=model,
        max_tokens=50,
        system=[{"type": "text", "text": DIAGRAM_CLASSIFY_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_data},
                },
            ],
        }],
    )
    category = message.content[0].text.strip().lower()
    if category not in DIAGRAM_TYPES:
        category = "sketch"
    return category


def _validate_mermaid(code: str) -> tuple[bool, str]:
    """Validate Mermaid syntax. Returns (is_valid, error_message)."""
    # Try mmdc CLI if available
    if shutil.which("mmdc"):
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".mmd", delete=False) as f:
                f.write(code)
                f.flush()
                result = subprocess.run(
                    ["mmdc", "-i", f.name, "-o", "/dev/null"],
                    capture_output=True, text=True, timeout=10,
                )
                os.unlink(f.name)
                if result.returncode == 0:
                    return True, ""
                return False, result.stderr.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: regex heuristics
    lines = code.strip().split("\n")
    if not lines:
        return False, "Empty diagram"

    first_line = lines[0].strip()
    valid_types = [
        "graph ", "flowchart ", "sequenceDiagram", "classDiagram",
        "stateDiagram", "erDiagram", "gantt", "pie", "mindmap",
        "gitGraph", "journey", "quadrantChart", "xychart",
    ]
    if not any(first_line.startswith(t) for t in valid_types):
        return False, f"Invalid diagram type declaration: {first_line}"

    # Check matched brackets
    open_count = code.count("{") + code.count("[") + code.count("(")
    close_count = code.count("}") + code.count("]") + code.count(")")
    if abs(open_count - close_count) > 2:
        return False, f"Unmatched brackets: {open_count} open vs {close_count} close"

    return True, ""


DIAGRAM_CONVERT_PROMPT = """Look at this handwritten diagram. Reproduce it as accurately as possible.

First decide the best format:
- If it's a flowchart, sequence diagram, state diagram, or architecture diagram with boxes/arrows: use Mermaid
- If it's a freeform sketch, mind map, or drawing with arbitrary positioning: use Excalidraw JSON

Rules:
- Preserve all text labels, connections, and layout direction
- Keep it simple and readable
- For Mermaid: output ONLY a valid mermaid code block, no explanation
- For Excalidraw: output ONLY valid Excalidraw JSON (the "elements" array), no explanation

Start your response with exactly one of these lines:
FORMAT: mermaid
FORMAT: excalidraw

Then output the content."""


def _convert_diagram_to_vector(client, crop_image: bytes, model: str) -> tuple[str, str]:
    """Send a cropped diagram image to Claude to reproduce as Mermaid or Excalidraw.
    Returns (format, content) where format is 'mermaid', 'excalidraw', 'markdown_table', or 'fallback_png'."""
    image_data = base64.standard_b64encode(crop_image).decode("utf-8")

    # Step 1: Classify the diagram type
    category = _classify_diagram(client, crop_image, model)

    # Tables get markdown format directly
    if category == "table":
        type_prompt = DIAGRAM_TYPES["table"]
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[{"type": "text", "text": type_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": image_data},
                    },
                ],
            }],
        )
        content = message.content[0].text.strip()
        # Strip code fences if present
        content = re.sub(r'^```(?:markdown)?\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content)
        return "markdown_table", content

    # Step 2: Use type-specific prompt for conversion
    type_prompt = DIAGRAM_TYPES.get(category, DIAGRAM_CONVERT_PROMPT)
    if category == "sketch":
        convert_prompt = DIAGRAM_CONVERT_PROMPT
    else:
        convert_prompt = type_prompt + "\n\nOutput ONLY the code, no explanation."

    # Retry loop for Mermaid validation
    messages: list[dict] = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image_data},
            },
        ],
    }]

    for attempt in range(3):
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[{"type": "text", "text": convert_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )

        response = message.content[0].text.strip()
        lines = response.split("\n", 1)

        if lines[0].startswith("FORMAT: mermaid"):
            content = lines[1].strip() if len(lines) > 1 else ""
        elif lines[0].startswith("FORMAT: excalidraw"):
            content = lines[1].strip() if len(lines) > 1 else ""
            content = re.sub(r'^```json\s*\n?', '', content)
            content = re.sub(r'\n?```\s*$', '', content)
            return "excalidraw", content
        elif "```mermaid" in response or "graph " in response or "flowchart " in response:
            content = response
        elif category == "sketch":
            return "excalidraw", response
        else:
            content = response

        # Strip mermaid fences
        content = re.sub(r'^```mermaid\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content)

        # Validate Mermaid output
        if category not in ("sketch",):
            valid, error = _validate_mermaid(content)
            if valid:
                return "mermaid", content
            log.warning("Mermaid validation failed (attempt %d)", attempt + 1)
            if attempt < 2:
                # Add error feedback for retry
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"The Mermaid syntax is invalid: {error}\nPlease fix and output only valid Mermaid code."})
                continue
            # All retries exhausted - fall back to PNG
            return "fallback_png", ""
        else:
            return "mermaid", content

    return "fallback_png", ""


def _write_excalidraw_file(elements_json: str, file_path: Path, description: str) -> None:
    """Write an Excalidraw .excalidraw.md file for Obsidian."""
    # Parse elements, wrapping in full Excalidraw structure if needed
    try:
        elements = json.loads(elements_json)
        if isinstance(elements, dict) and "elements" in elements:
            elements = elements["elements"]
    except json.JSONDecodeError:
        elements = []

    excalidraw_data = {
        "type": "excalidraw",
        "version": 2,
        "source": "remarkable-obsidian-sync",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }

    content = (
        "---\n\nexcalidraw-plugin: parsed\ntags: [excalidraw]\n\n---\n"
        "==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠==\n\n\n"
        f"# Text Elements\n{description}\n\n"
        "%%\n# Drawing\n```json\n"
        + json.dumps(excalidraw_data, indent=2)
        + "\n```\n%%"
    )
    file_path.write_text(content)


def extract_diagram_crops(
    markdown: str,
    page_paths: Sequence[Path | None],
    vault_path: str,
    notebook_name: str,
    client=None,
    model: str = "",
    notebook_id: str = "",
) -> tuple[str, list[str]]:
    """Find Diagram markers in markdown, convert to Mermaid/Excalidraw, return updated markdown and saved filenames."""
    from PIL import Image

    attachments = Path(vault_path) / "Attachments" / "reMarkable"
    attachments.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(notebook_name)

    saved_crops = []
    crop_counter = 0

    def replace_diagram(match):
        nonlocal crop_counter
        page_num = int(match.group(1))
        top_pct = int(match.group(2))
        bottom_pct = int(match.group(3))
        description = match.group(4).strip()

        if page_num < 1 or page_num > len(page_paths) or page_paths[page_num - 1] is None:
            return match.group(0)  # leave as-is if page out of range or not rendered

        page_path = page_paths[page_num - 1]
        assert page_path is not None
        try:
            # Load the page as a PIL Image
            if page_path.suffix.lower() == ".svg":
                png_data = _svg_to_full_png(page_path)
                img = Image.open(io.BytesIO(png_data))
            elif page_path.suffix.lower() == ".pdf":
                import fitz  # PyMuPDF
                doc = fitz.open(str(page_path))
                # For single-PDF sources, page_num selects the page within the PDF
                pdf_page_idx = page_num - 1 if len(page_paths) == 1 else 0
                if pdf_page_idx >= len(doc):
                    return match.group(0)
                pix = doc[pdf_page_idx].get_pixmap(dpi=150)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                doc.close()
            else:
                img = Image.open(page_path)

            width, height = img.size
            # Add some padding around the crop region
            pad = int(height * 0.02)
            y_top = max(0, int(height * top_pct / 100) - pad)
            y_bottom = min(height, int(height * bottom_pct / 100) + pad)

            cropped = img.crop((0, y_top, width, y_bottom))

            crop_counter += 1

            # Save cropped PNG for reference
            buf = io.BytesIO()
            cropped.save(buf, "PNG")
            crop_bytes = buf.getvalue()

            # ID isolates notebooks; content hash keeps older note assets intact.
            asset_name = (f"rm-{notebook_id.encode().hex()}-{hashlib.sha256(crop_bytes).hexdigest()}"
                          if notebook_id else safe_name)
            crop_name = f"{asset_name} - diagram {crop_counter}.png"
            crop_path = attachments / crop_name
            crop_path.write_bytes(crop_bytes)
            saved_crops.append(crop_name)
            log.info("Cropped diagram %d from page %d (%d%%-%d%%)", crop_counter, page_num, top_pct, bottom_pct)

            # Convert to vector format if client available
            if client and model:
                try:
                    fmt, content = _convert_diagram_to_vector(client, crop_bytes, model)
                    if fmt == "markdown_table" and content.strip():
                        log.info("Converted diagram %d to markdown table", crop_counter)
                        return f"> *{description}*\n\n{content}"
                    elif fmt == "mermaid" and content.strip():
                        log.info("Converted diagram %d to Mermaid", crop_counter)
                        return f"> *{description}*\n\n```mermaid\n{content}\n```"
                    elif fmt == "excalidraw" and content.strip():
                        vector_name = (f"{asset_name}-{hashlib.sha256((description + content).encode()).hexdigest()}"
                                       if notebook_id else safe_name)
                        excalidraw_name = f"{vector_name} - diagram {crop_counter}.excalidraw.md"
                        excalidraw_path = attachments / excalidraw_name
                        _write_excalidraw_file(content, excalidraw_path, description)
                        saved_crops.append(excalidraw_name)
                        log.info("Converted diagram %d to Excalidraw", crop_counter)
                        return f"> *{description}*\n\n![[{excalidraw_name}]]"
                    # fallback_png: fall through to image embed
                except Exception:
                    log.warning("Failed to convert diagram %d to vector, using image", crop_counter)

            # Fallback: embed cropped image
            return f"> *{description}*\n\n![[{crop_name}]]"
        except Exception:
            log.warning("Failed to crop diagram from page %d", page_num)
            return match.group(0)

    updated_markdown = DIAGRAM_RE.sub(replace_diagram, markdown)
    return updated_markdown, saved_crops


def sanitize_filename(name: str) -> str:
    """Keep alphanumeric, spaces, hyphens, underscores; replace others with _."""
    return re.sub(r"[^a-zA-Z0-9 _-]", "_", name)


def save_source_pages(vault_path: str, notebook: dict, page_paths: Sequence[Path | None], changed_indices: set[int] | None = None) -> list[str]:
    """Save source page images to the vault Attachments folder. Returns list of filenames.

    When *changed_indices* is provided, unchanged assets are reused if present;
    missing assets are supplied too, so upgrades never emit dangling embeds.
    """
    attachments = Path(vault_path) / "Attachments" / "reMarkable"
    attachments.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(notebook["name"])
    saved = []
    for i, page_path in enumerate(page_paths):
        if page_path is None and notebook.get("selected_pages"):
            continue
        # Names do not depend on display names, which can collide or change.
        if notebook.get("id"):
            if page_path is None:
                raise ValueError("Source page required to supply namespaced assets")
            safe_name = f"rm-{notebook['id'].encode().hex()}-{_hash_file(page_path)}"
        # Determine the destination filename regardless of whether we write
        suffix = page_path.suffix.lower() if page_path else ".svg"
        if suffix == ".pdf":
            dest_name = f"{safe_name}.pdf"
        else:
            dest_name = f"{safe_name} - page {i+1}.png"

        # Only write to disk if this page changed (or no filter given)
        if changed_indices is None or i in changed_indices or not (attachments / dest_name).exists():
            if page_path is None:
                saved.append(dest_name)
                continue
            if suffix == ".svg":
                png_data = _svg_to_full_png(page_path)
                (attachments / dest_name).write_bytes(png_data)
            elif suffix == ".pdf":
                shutil.copy2(page_path, attachments / dest_name)
            else:
                dest_name = f"{safe_name} - page {i+1}{suffix}"
                shutil.copy2(page_path, attachments / dest_name)
            log.info("Saved source: %s", dest_name)

        saved.append(dest_name)

    return saved


def _move_obsidian_note(vault_path: str, old_rm_path: str, old_name: str, notebook: dict) -> None:
    """Move an Obsidian note when its reMarkable notebook has been moved or renamed."""
    vault = Path(vault_path)
    base = vault / "Remarkable Notes"

    # Compute old location
    old_parent = str(Path(old_rm_path).parent).lstrip("/")
    old_dir = base / old_parent if old_parent and old_parent != "." else base
    old_file = old_dir / f"{sanitize_filename(old_name)}.md"

    # Compute new location
    new_parent = str(Path(notebook["path"]).parent).lstrip("/")
    new_dir = base / new_parent if new_parent and new_parent != "." else base
    new_file = new_dir / f"{sanitize_filename(notebook['name'])}.md"

    if old_file == new_file:
        return
    if not old_file.exists():
        log.warning("Cannot move %s — old file not found", old_file)
        return

    if new_file.exists():
        raise ValueError(f"Conflict: move destination already exists: {new_file}")
    new_dir.mkdir(parents=True, exist_ok=True)
    old_file.rename(new_file)
    log.info("Moved %s → %s", old_file.relative_to(vault), new_file.relative_to(vault))

    # Clean up empty parent directories
    try:
        old_dir.rmdir()  # only removes if empty
    except OSError:
        pass


def _move_obsidian_note_legacy(vault_path: str, notebook: dict) -> None:
    """Move an Obsidian note when state has no stored path (legacy/first-run migration).

    Searches for an existing note by filename under Remarkable Notes/ and moves it
    to the location implied by the notebook's current reMarkable path.
    """
    vault = Path(vault_path)
    base = vault / "Remarkable Notes"
    safe_name = f"{sanitize_filename(notebook['name'])}.md"

    # Where the note *should* be based on current reMarkable path
    new_parent = str(Path(notebook["path"]).parent).lstrip("/")
    new_dir = base / new_parent if new_parent and new_parent != "." else base
    new_file = new_dir / safe_name

    if new_file.exists():
        return  # already in the right place

    # Search for the note anywhere under Remarkable Notes/
    matches = list(base.rglob(safe_name))
    if len(matches) == 1:
        old_file = matches[0]
        old_dir = old_file.parent
        new_dir.mkdir(parents=True, exist_ok=True)
        old_file.rename(new_file)
        log.info("Moved %s → %s (legacy migration)",
                 old_file.relative_to(vault), new_file.relative_to(vault))
        try:
            old_dir.rmdir()
        except OSError:
            pass
    elif len(matches) > 1:
        log.warning("Multiple notes named %s found — skipping legacy move", safe_name)


def _note_path(vault_path: str, notebook: dict) -> Path:
    parent = str(Path(notebook.get("path", "")).parent).lstrip("/")
    return Path(vault_path) / "Remarkable Notes" / parent / f"{sanitize_filename(notebook['name'])}.md"


def _check_note_output(path: Path, expected_hash: str | None) -> None:
    if path.exists() and (not expected_hash or _hash_file(path) != expected_hash):
        raise ValueError(f"Conflict: edited or legacy note preserved: {path}; move it outside Remarkable Notes and remove its state entry before regenerating")


def write_obsidian_note(vault_path: str, notebook: dict, markdown: str, source_files: list[str] | None = None, tasks: list[dict] | None = None, expected_hash: str | None = None) -> Path:
    """Write a markdown note with YAML frontmatter to the Obsidian vault inbox."""
    # Preserve reMarkable folder structure under Remarkable Notes/
    rm_path = notebook.get("path", "")
    parent_dir = str(Path(rm_path).parent).lstrip("/")
    inbox = Path(vault_path) / "Remarkable Notes"
    if parent_dir and parent_dir != ".":
        inbox = inbox / parent_dir
    inbox.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(notebook["name"])
    filename = f"{safe_name}.md"

    # Map reMarkable Type to a readable label
    rm_type = notebook.get("type", "DocumentType")
    type_label = "notebook" if rm_type == "DocumentType" else rm_type.lower()

    lines = [
        "---",
        f'title: "{notebook["name"]}"',
        f'modified: {notebook["modified"]}',
        "source: reMarkable",
        f'type: {type_label}',
        f'remarkable_id: "{notebook["id"]}"',
        f'remarkable_path: "{rm_path}"',
    ]
    if notebook.get("starred"):
        lines.append("starred: true")
    page_count = notebook.get("page_count")
    if page_count is not None:
        lines.append(f"page_count: {page_count}")
    if notebook.get("selected_pages"):
        lines.append(f"selected_pages: {json.dumps(notebook['selected_pages'])}")

    # Merge hardcoded tags with reMarkable tags
    tags = ["handwritten", "inbox"]
    for t in notebook.get("tags", []):
        tag = t.strip().lower().replace(" ", "-")
        if tag and tag not in tags:
            tags.append(tag)
    lines.append("tags:")
    for tag in tags:
        lines.append(f"  - {tag}")

    if tasks:
        lines.append("tasks:")
        for task in tasks:
            lines.append(f'  - "{task.get("text", "")}"')

    lines.append("---")
    frontmatter = "\n".join(lines) + "\n"

    body = "\n" + markdown + "\n"

    if source_files:
        body += "\n---\n\n## Handwritten source\n\n"
        for fname in source_files:
            body += f"![[{fname}]]\n\n"

    note_path = inbox / filename
    _check_note_output(note_path, expected_hash)
    note_path.write_text(frontmatter + body)
    return note_path


def _hash_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file's contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- A1: Context-aware prompts ---


def _build_page_context(notebook: dict, page_index: int, total_pages: int, prev_markdown: str = "") -> str:
    """Build context string for a page transcription request."""
    parts = [
        f"Notebook: {notebook['name']}",
        f"Path: {notebook.get('path', '')}",
        f"Page {page_index + 1} of {total_pages}",
    ]
    if prev_markdown:
        preview = prev_markdown[:300]
        if len(prev_markdown) > 300:
            preview += "..."
        parts.append(f"Previous page content:\n{preview}")
    return "\n".join(parts)


# --- A2: Per-notebook prompt customization ---


def _load_custom_prompts() -> list[tuple[str, str]]:
    """Read prompts/*.txt from the script directory. Returns [(glob_pattern, prompt_text)] sorted by specificity."""
    prompts_dir = Path(__file__).parent / "prompts"
    if not prompts_dir.is_dir():
        return []
    entries = []
    for txt_file in sorted(prompts_dir.glob("*.txt")):
        pattern = txt_file.stem  # filename without .txt is the glob pattern
        prompt_text = txt_file.read_text().strip()
        if prompt_text:
            entries.append((pattern, prompt_text))
    # Sort by specificity: longer patterns first (more specific)
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    return entries


def resolve_prompt(notebook_name: str, notebook_path: str, custom_prompts: list[tuple[str, str]], default_prompt: str) -> str:
    """Resolve which prompt to use for a notebook. Glob-matches against custom prompts."""
    for pattern, prompt_text in custom_prompts:
        if fnmatch.fnmatch(notebook_name, pattern) or fnmatch.fnmatch(notebook_path, pattern):
            return prompt_text
    return default_prompt


# --- B2: Task extraction ---


TASK_EXTRACTION_PROMPT = """Extract action items from this text. Return a JSON array of objects with these fields:
- "text": the action item description
- "assignee": person responsible (null if unclear)
- "due": due date if mentioned (null if none)

Return ONLY valid JSON, no explanation. If no tasks found, return [].

Example: [{"text": "Send report to team", "assignee": "Alice", "due": "2026-03-15"}]"""

def extract_tasks(client, markdown: str, model: str | None = None) -> list[dict]:
    """Extract action items using the selected provider and task model."""
    model = model if model is not None else _model_value("RM_TASK_MODEL", os.environ.get("RM_MODEL"))
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": TASK_EXTRACTION_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": markdown}],
    )
    try:
        text = message.content[0].text.strip()
        # Strip code fences if present
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        tasks = json.loads(text)
        if isinstance(tasks, list):
            return tasks
    except (json.JSONDecodeError, IndexError):
        log.warning("Failed to parse task extraction response as JSON")
    return []


def _inject_tasks(markdown: str, tasks: list[dict]) -> str:
    """Append an Extracted Tasks section with checkboxes to the markdown."""
    if not tasks:
        return markdown
    lines = ["\n\n## Extracted Tasks\n"]
    for task in tasks:
        text = task.get("text", "")
        assignee = task.get("assignee")
        due = task.get("due")
        suffix = ""
        if assignee:
            suffix += f" @{assignee}"
        if due:
            suffix += f" (due: {due})"
        lines.append(f"- [ ] {text}{suffix}")
    return markdown + "\n".join(lines) + "\n"


# --- B3: Tag inference ---


TAG_INFERENCE_PROMPT = """Analyze this text and suggest 3-5 semantic tags that describe its content.
Return ONLY a JSON array of lowercase tag strings, no explanation.
Tags should be short (1-2 words), use hyphens for multi-word tags.

Example: ["meeting-notes", "strategy", "quarterly-planning", "finance"]"""


def infer_tags(client, markdown: str, model: str | None = None) -> list[str]:
    """Infer semantic tags using the selected provider and tag model."""
    model = model if model is not None else _model_value("RM_TAG_MODEL", os.environ.get("RM_MODEL"))
    message = client.messages.create(
        model=model,
        max_tokens=256,
        system=[{"type": "text", "text": TAG_INFERENCE_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": markdown}],
    )
    try:
        text = message.content[0].text.strip()
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)
        tags = json.loads(text)
        if isinstance(tags, list):
            # Normalize: lowercase, strip, replace spaces with hyphens
            return [t.strip().lower().replace(" ", "-") for t in tags if isinstance(t, str) and t.strip()]
    except (json.JSONDecodeError, IndexError):
        log.warning("Failed to parse tag inference response as JSON")
    return []


def sync_notebooks(
    notebooks: list[dict],
    state: dict,
    vault_path: str,
    rmapi_bin: str,
    model: str,
    dry_run: bool,
    client,
    state_file: str,
    note_index: dict[str, str] | None = None,
    extract_tasks_enabled: bool = False,
    infer_tags_enabled: bool = False,
    blank_detect: bool = True,
    custom_prompts: list[tuple[str, str]] | None = None,
    selection: Selection | None = None,
    task_model: str | None = None,
    tag_model: str | None = None,
    diagram_model: str | None = None,
) -> SyncSummary:
    """Process each notebook: skip unchanged, export, transcribe changed pages, write."""
    task_model = task_model if task_model is not None else model
    tag_model = tag_model if tag_model is not None else model
    diagram_model = diagram_model if diagram_model is not None else model
    summary = SyncSummary()
    for nb in notebooks:
        nb = dict(nb)
        nb_state = state.get(nb["id"], {})

        # Legacy state format migration: old format was {id: version}
        if not isinstance(nb_state, dict):
            nb_state = {}

        selected_mode = selection is not None or "selection" in nb_state

        # Preview exits before persistent mutations or model calls, including
        # rename, legacy migration and unchanged-state refresh paths.
        if dry_run:
            detail = ("selection unresolved: requires archive inspection; no downloads, model calls or writes"
                      if selected_mode else "would check/sync notebook (no writes or model calls)")
            log.info("[DRY RUN] %s (%s): %s", nb["name"], nb["path"], detail)
            summary.results.append(SyncResult(nb["name"], nb["path"], "dry_run", detail))
            continue

        # Check old note and occupied destinations before moves or asset writes.
        # Legacy outputs are never adopted as a trusted baseline.
        target = _note_path(vault_path, nb)
        old_note = _note_path(vault_path, {**nb, "path": nb_state.get("path", nb["path"]),
                                         "name": nb_state.get("name", nb["name"])})
        try:
            _check_note_output(old_note, nb_state.get("output_hash"))
            if target != old_note:
                _check_note_output(target, None)
            if not nb_state.get("path"):
                for legacy_note in (Path(vault_path) / "Remarkable Notes").rglob(f"{sanitize_filename(nb['name'])}.md"):
                    ids = re.findall(r'^remarkable_id: *"?([^"\n]+)"? *$', legacy_note.read_text(), re.MULTILINE)
                    if not ids or nb["id"] in ids:
                        _check_note_output(legacy_note, None)
        except ValueError as exc:
            detail = "Conflict: managed note changed or destination occupied; preserved output"
            log.error("%s", detail)
            summary.results.append(SyncResult(nb["name"], nb["path"], "errored", detail))
            continue

        # Detect path or name changes (notebook moved/renamed in reMarkable)
        old_path = nb_state.get("path")
        old_name = nb_state.get("name")
        if not selected_mode and old_path and (old_path != nb["path"] or old_name != nb["name"]):
            _move_obsidian_note(vault_path, old_path, old_name or Path(old_path).name, nb)

        if not selected_mode and nb_state.get("version") == nb["version"] and nb["version"] != 0:
            # Update stored path/name even when skipping content sync.
            # Skip the fast-path when version is 0 - the reMarkable API
            # sometimes never increments from 0, so we fall through to
            # page-level hash comparison to detect actual content changes.
            state[nb["id"]] = {**nb_state, "path": nb["path"], "name": nb["name"]}
            save_state(state_file, state)
            log.info("Skipping %s (unchanged, version %s)", nb["name"], nb["version"])
            summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "unchanged"))
            continue

        # Version is often stuck at 0, so also skip when the modified time is
        # unchanged since a successful sync. Saves an export per notebook,
        # which otherwise trips reMarkable cloud rate limits (HTTP 429).
        if (not selected_mode and nb.get("modified") and nb_state.get("modified") == nb["modified"]
                and ("pages" in nb_state or "pdf_hash" in nb_state)):
            state[nb["id"]] = {**nb_state, "path": nb["path"], "name": nb["name"]}
            save_state(state_file, state)
            log.info("Skipping %s (unchanged, modified %s)", nb["name"], nb["modified"])
            summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "unchanged"))
            continue

        # A2: Resolve custom prompt for this notebook
        nb_prompt = None
        if custom_prompts:
            nb_prompt = resolve_prompt(nb["name"], nb.get("path", ""), custom_prompts, SINGLE_PAGE_PROMPT)
            if nb_prompt == SINGLE_PAGE_PROMPT:
                nb_prompt = None  # no override, use default

        log.info("Processing: %s", nb["name"])
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            # Extract raw .rm pages (returns None for PDF notebooks)
            selection_state = None
            page_ids = []
            if selected_mode:
                rm_pages, page_ids, native_tags = _prepare_selected_pages(
                    rmapi_bin, nb["path"], tmp_dir, selection or Selection())
                nb["tags"] = native_tags  # Never use stat/inferred/frontmatter tags as evidence.
                selected_indices = [i for i, path in enumerate(rm_pages) if path is not None]
                if not selected_indices:
                    summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "no pages match selection; existing output retained"))
                    continue
                selection_state = {"ids": [page_ids[i] for i in selected_indices],
                                   "numbers": selected_indices, "tags": native_tags,
                                   "total": len(rm_pages)}
                if old_path and (old_path != nb["path"] or old_name != nb["name"]):
                    _move_obsidian_note(vault_path, old_path, old_name or Path(old_path).name, nb)
            else:
                rm_pages = _extract_rm_pages(rmapi_bin, nb["path"], tmp_dir)
                selected_indices = list(range(len(rm_pages or [])))

            # Merge page tags from .content file (rmapi stat doesn't return these)
            content_tags = _content_tags.pop(nb["path"], []) if not selected_mode else []
            if content_tags:
                existing = nb.get("tags", [])
                nb["tags"] = list({*existing, *content_tags})

            if rm_pages is None:
                # PDF-based notebook - no per-page tracking, full re-transcription
                pdfs = list(tmp_dir.glob("*.pdf"))
                if not pdfs:
                    continue
                # Use original.pdf (extracted from zip) for source display and cropping
                # if available; the annotations PDF is a tiny stub for uploaded PDFs
                original_pdf = tmp_dir / "original.pdf"
                source_pdfs = [original_pdf] if original_pdf.exists() else pdfs
                # Version is often stuck at 0, so compare content hashes too
                pdf_hash = _hash_file(pdfs[0])
                if nb_state.get("pdf_hash") == pdf_hash:
                    log.info("Skipping %s (PDF content unchanged)", nb["name"])
                    state[nb["id"]] = {**nb_state, "version": nb["version"], "path": nb["path"], "name": nb["name"], "modified": nb.get("modified")}
                    save_state(state_file, state)
                    summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "PDF content unchanged"))
                    continue

                # A1: Build context for PDF
                pdf_context = f"Notebook: {nb['name']}\nPath: {nb.get('path', '')}"
                markdown = transcribe_pdf(client, pdfs[0], model, context=pdf_context, system_prompt=nb_prompt)
                markdown, _ = extract_diagram_crops(markdown, source_pdfs, vault_path, nb["name"], client=client, model=diagram_model, notebook_id=nb["id"])
                if note_index:
                    markdown = autolink_markdown(markdown, note_index)
                # B2: Extract tasks if enabled
                tasks = None
                if extract_tasks_enabled:
                    tasks = extract_tasks(client, markdown, task_model)
                    if tasks:
                        markdown = _inject_tasks(markdown, tasks)
                # B3: Infer tags if enabled
                if infer_tags_enabled:
                    inferred = infer_tags(client, markdown, tag_model)
                    if inferred:
                        existing_tags = nb.get("tags", [])
                        nb["tags"] = list({*existing_tags, *inferred})
                source_files = save_source_pages(vault_path, nb, source_pdfs)
                nb["page_count"] = len(source_pdfs)
                note_path = write_obsidian_note(vault_path, nb, markdown, source_files, tasks=tasks, expected_hash=nb_state.get("output_hash"))
                log.info("Wrote %s (%d chars)", note_path, len(markdown))
                state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"], "modified": nb.get("modified"), "pdf_hash": pdf_hash, "output_hash": _hash_file(note_path)}
                save_state(state_file, state)
                summary.results.append(SyncResult(nb["name"], nb["path"], "synced", f"{len(pdfs)} PDF pages"))
                continue

            if not rm_pages:
                continue

            # Per-page change detection
            cached_pages = nb_state.get("pages", {})
            if selected_mode:
                # Old context-bearing cache can contain information from excluded pages.
                # Only our context-free page records can cross a selection boundary.
                by_id = {page.get("id"): page for page in cached_pages.values() if page.get("context_free")}
                cached_pages = {str(i): by_id.get(page_ids[i], {}) for i in selected_indices}
            page_hashes = {}
            changed_indices = []

            for i in selected_indices:
                rm_file = rm_pages[i]
                assert rm_file is not None
                h = _hash_file(rm_file)
                page_hashes[str(i)] = h
                cached = cached_pages.get(str(i), {})
                if cached.get("hash") != h or "markdown" not in cached:
                    changed_indices.append(i)

            # Detect removed pages
            current_count = len(rm_pages)
            old_count = len(cached_pages)
            removed = current_count < old_count

            if not changed_indices and not removed and (not selected_mode or nb_state.get("selection") == selection_state):
                log.info("Skipping %s (no page content changed)", nb["name"])
                state[nb["id"]] = {**nb_state, "version": nb["version"], "path": nb["path"], "name": nb["name"], "modified": nb.get("modified"), "pages": cached_pages}
                save_state(state_file, state)
                summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "no page content changed"))
                continue
            # Supply every source on upgrade/rename and for cached diagrams.
            # Only changed pages are transcribed by the model.
            svg_paths: list[Path | None] = [None] * len(rm_pages)
            for i in selected_indices:
                rm_file = rm_pages[i]
                assert rm_file is not None
                if selected_mode and rm_file.suffix == ".pdf":
                    svg_paths[i] = rm_file
                    continue
                svg_content = _render_rm_to_svg(rm_file)
                svg_path = tmp_dir / f"page_{i+1}.svg"
                svg_path.write_text(svg_content)
                svg_paths[i] = svg_path

            # Transcribe only changed pages, reuse cached markdown for others
            page_markdowns = {}
            prev_markdown = ""
            for i in selected_indices:
                page_path = svg_paths[i]
                assert page_path is not None
                if i in changed_indices:
                    # D1: Skip blank pages
                    if blank_detect and page_path.suffix == ".svg" and is_blank_page(page_path):
                        log.info("Skipping page %d/%d (blank)", i + 1, len(rm_pages))
                        page_markdowns[str(i)] = ""
                        continue
                    log.info("Transcribing page %d/%d (changed)", i + 1, len(rm_pages))
                    # A1: Build context for this page
                    ctx = _build_page_context(nb, i, len(rm_pages), prev_markdown="" if selected_mode else prev_markdown)
                    if selected_mode and page_path.suffix == ".pdf":
                        md = transcribe_pdf(client, page_path, model, context=ctx,
                                            system_prompt=nb_prompt or SINGLE_PAGE_PROMPT)
                    else:
                        md = transcribe_page(client, page_path, model, context=ctx, system_prompt=nb_prompt)
                    # Each selected input is one page, regardless of its original number.
                    md = _renumber_diagrams(md, i + 1) if selected_mode else md.replace("page=1", f"page={i+1}")
                    page_markdowns[str(i)] = md
                else:
                    log.info("Reusing cached transcription for page %d/%d", i + 1, len(rm_pages))
                    md = cached_pages[str(i)]["markdown"]
                    page_markdowns[str(i)] = _renumber_diagrams(md, i + 1) if selected_mode else md
                prev_markdown = page_markdowns[str(i)]

            # Assemble full markdown
            all_parts = [(f"## Page {i+1}\n\n" if selected_mode else "") + page_markdowns[str(i)] for i in selected_indices]
            markdown = "\n\n---\n\n".join(all_parts)

            # Extract and crop any diagram regions
            markdown, _ = extract_diagram_crops(markdown, svg_paths, vault_path, nb["name"], client=client, model=diagram_model, notebook_id=nb["id"])

            if note_index:
                markdown = autolink_markdown(markdown, note_index)

            # B2: Extract tasks if enabled
            tasks = None
            if extract_tasks_enabled:
                tasks = extract_tasks(client, markdown, task_model)
                if tasks:
                    markdown = _inject_tasks(markdown, tasks)

            # B3: Infer tags if enabled
            if infer_tags_enabled:
                inferred = infer_tags(client, markdown, tag_model)
                if inferred:
                    existing_tags = nb.get("tags", [])
                    nb["tags"] = list({*existing_tags, *inferred})

            if selected_mode:
                nb["selected_pages"] = [i + 1 for i in selected_indices]
            source_files = save_source_pages(vault_path, nb, svg_paths, changed_indices=set(changed_indices))
            nb["page_count"] = len(selected_indices)
            note_path = write_obsidian_note(vault_path, nb, markdown, source_files, tasks=tasks, expected_hash=nb_state.get("output_hash"))
            log.info("Wrote %s (%d chars, %d/%d pages transcribed)",
                     note_path, len(markdown), len(changed_indices), len(rm_pages))
            summary.results.append(SyncResult(nb["name"], nb["path"], "synced", f"{len(changed_indices)}/{len(rm_pages)} pages transcribed"))

            # Save per-page state
            new_pages = {}
            for i in selected_indices:
                new_pages[str(i)] = {
                    "hash": page_hashes[str(i)],
                    "markdown": page_markdowns[str(i)],
                    **({"id": page_ids[i], "context_free": True} if selected_mode else {}),
                }
            state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"], "modified": nb.get("modified"), "pages": new_pages, "output_hash": _hash_file(note_path), **({"selection": selection_state} if selected_mode else {})}
            save_state(state_file, state)
        except Exception as exc:
            log.error("Notebook processing failed; output/state not advanced")
            summary.results.append(SyncResult(nb["name"], nb["path"], "errored", "Notebook processing failed"))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return summary


def merge_state_files(target_file: str, source_files: list[str]) -> None:
    """Merge multiple state files into the target, combining notebook entries."""
    merged = load_state(target_file)
    for src in source_files:
        src_state = load_state(src)
        merged.update(src_state)
    save_state(target_file, merged)
    log.info("Merged %d state files into %s (%d notebooks)", len(source_files), target_file, len(merged))


def retag_notebooks(notebooks: list[dict], vault_path: str, rmapi_bin: str) -> None:
    """Download each notebook to extract tags from .content and update Obsidian frontmatter."""
    vault = Path(vault_path)
    base = vault / "Remarkable Notes"
    updated = 0

    for nb in notebooks:
        safe_name = sanitize_filename(nb["name"])
        rm_path = nb["path"]
        parent = str(Path(rm_path).parent).lstrip("/")
        note_dir = base / parent if parent and parent != "." else base
        note_file = note_dir / f"{safe_name}.md"

        if not note_file.exists():
            continue

        # Download notebook to temp dir
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            subprocess.run(
                [rmapi_bin, "get", rm_path],
                capture_output=True, text=True,
                cwd=str(tmp_dir),
            )
            archives = list(tmp_dir.glob("*.zip")) + list(tmp_dir.glob("*.rmdoc"))
            if not archives:
                continue

            _extract_content_tags(archives[0], rm_path)
            content_tags = _content_tags.pop(rm_path, [])
            if not content_tags:
                continue

            # Read existing note and update frontmatter tags
            text = note_file.read_text()
            if "---" not in text:
                continue

            parts = text.split("---", 2)
            if len(parts) < 3:
                continue

            frontmatter = parts[1]

            # Parse existing tags from frontmatter
            existing_tags: list[str] = []
            fm_lines = frontmatter.split("\n")
            in_tags = False
            tag_start = -1
            tag_end = -1
            for i, line in enumerate(fm_lines):
                if line.strip() == "tags:":
                    in_tags = True
                    tag_start = i
                    continue
                if in_tags:
                    if line.strip().startswith("- "):
                        existing_tags.append(line.strip().removeprefix("- ").strip())
                        tag_end = i
                    else:
                        break

            # Merge new tags
            merged = list(existing_tags)
            for t in content_tags:
                tag = t.strip().lower().replace(" ", "-")
                if tag and tag not in merged:
                    merged.append(tag)

            if merged == existing_tags:
                continue  # no new tags

            # Rebuild frontmatter with updated tags
            new_tag_lines = ["tags:"] + [f"  - {t}" for t in merged]
            if tag_start >= 0 and tag_end >= 0:
                fm_lines[tag_start:tag_end + 1] = new_tag_lines
            else:
                # No tags section — add before closing ---
                fm_lines.extend(new_tag_lines)

            new_frontmatter = "\n".join(fm_lines)
            note_file.write_text(f"---{new_frontmatter}---{parts[2]}")
            updated += 1
            log.info("Updated tags for %s: %s", nb["name"], merged)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info("Retag complete: %d notes updated", updated)


def write_sync_log(vault_path: str, summary: SyncSummary) -> Path | None:
    """Write/update a rolling sync log in the vault (newest first, max 10 entries)."""
    log_dir = Path(vault_path) / "Remarkable Notes"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "Sync Log.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## Sync - {now}", ""]
    lines.append(f"{len(summary.synced)} synced, {len(summary.skipped)} skipped, {len(summary.errored)} errors")
    lines.append("")

    if summary.synced:
        lines.append("### Synced")
        for r in summary.synced:
            # Build wiki link from the reMarkable path
            rm_parent = str(Path(r.path).parent).lstrip("/")
            if rm_parent and rm_parent != ".":
                link_path = f"Remarkable Notes/{rm_parent}/{sanitize_filename(r.name)}"
            else:
                link_path = f"Remarkable Notes/{sanitize_filename(r.name)}"
            lines.append(f"- [[{link_path}|{r.name}]] - {r.detail}")
        lines.append("")

    if summary.errored:
        lines.append("### Errors")
        for r in summary.errored:
            detail = "Conflict: output preserved" if r.detail.startswith("Conflict:") else "Notebook processing failed"
            lines.append(f"- {r.name} - {detail}")
        lines.append("")

    new_entry = "\n".join(lines)

    # Parse existing entries and prepend
    existing_entries: list[str] = []
    if log_file.exists():
        content = log_file.read_text()
        if not content.startswith("## Sync - "):
            log.error("Conflict: sync log destination is not a generated log; preserved %s", log_file)
            return None
        # Split on ## Sync - headings
        parts = re.split(r"(?=^## Sync - )", content, flags=re.MULTILINE)
        for part in parts:
            part = part.strip()
            if part.startswith("## Sync -"):
                existing_entries.append(part)

    # Rolling limit: keep newest 9 existing + 1 new = 10 total
    all_entries = [new_entry.strip()] + existing_entries[:9]
    full_content = "\n\n".join(all_entries) + "\n"
    log_file.write_text(full_content)
    return log_file


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_note_index(vault_path: str) -> dict[str, str]:
    """Scan vault for .md files and return {lowercase_stem: actual_stem} mapping."""
    index: dict[str, str] = {}
    vault = Path(vault_path)
    for md_file in vault.rglob("*.md"):
        stem = md_file.stem
        # Exclude short names (< 4 chars) to avoid false positives
        if len(stem) < 4:
            continue
        # Exclude date-pattern daily notes
        if _DATE_PATTERN.match(stem):
            continue
        # Exclude numeric-only names
        if stem.isdigit():
            continue
        index[stem.lower()] = stem
    return index


def autolink_markdown(text: str, note_index: dict[str, str]) -> str:
    """Insert wiki links for known vault note names, preserving protected regions."""
    if not note_index:
        return text

    # Phase 1: Find protected regions (byte offsets of regions to skip)
    protected: list[tuple[int, int]] = []

    # Frontmatter (--- ... ---)
    fm_match = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    if fm_match:
        protected.append((fm_match.start(), fm_match.end()))

    # Existing wiki links [[...]]
    for m in re.finditer(r"\[\[.*?\]\]", text):
        protected.append((m.start(), m.end()))

    # Embeds ![[...]]
    for m in re.finditer(r"!\[\[.*?\]\]", text):
        protected.append((m.start(), m.end()))

    # Inline code `...`
    for m in re.finditer(r"`[^`]+`", text):
        protected.append((m.start(), m.end()))

    # Fenced code blocks ```...```
    for m in re.finditer(r"```.*?```", text, re.DOTALL):
        protected.append((m.start(), m.end()))

    def is_protected(start: int, end: int) -> bool:
        for ps, pe in protected:
            if start < pe and end > ps:
                return True
        return False

    # Phase 2: Sort note names longest-first to prevent partial matches
    sorted_names = sorted(note_index.items(), key=lambda x: len(x[1]), reverse=True)

    for lower_name, actual_name in sorted_names:
        # Build regex with word boundaries
        pattern = re.compile(r"\b" + re.escape(actual_name) + r"\b", re.IGNORECASE)
        # Process from end to start to preserve offsets
        matches = list(pattern.finditer(text))
        for m in reversed(matches):
            if is_protected(m.start(), m.end()):
                continue
            matched_text = m.group(0)
            if matched_text == actual_name:
                replacement = f"[[{actual_name}]]"
            else:
                replacement = f"[[{actual_name}|{matched_text}]]"
            text = text[:m.start()] + replacement + text[m.end():]
            # Update protected regions: shift all after this point
            shift = len(replacement) - len(matched_text)
            protected_new = []
            for ps, pe in protected:
                if ps >= m.end():
                    protected_new.append((ps + shift, pe + shift))
                elif pe <= m.start():
                    protected_new.append((ps, pe))
                else:
                    protected_new.append((ps, pe))
            # Also protect the new link itself
            protected_new.append((m.start(), m.start() + len(replacement)))
            protected = protected_new

    return text


def _main():
    parser = argparse.ArgumentParser(description="Sync reMarkable notebooks to Obsidian")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--list-only", metavar="FILE", help="List notebooks to JSON file and exit")
    parser.add_argument("--notebooks-json", metavar="FILE", help="Read notebook list from JSON instead of rmapi")
    parser.add_argument("--slice", metavar="START:END", help="Process only notebooks[START:END]")
    parser.add_argument("--merge-states", nargs="+", metavar="FILE", help="Merge state files into main state and exit")
    parser.add_argument("--retag", action="store_true", help="Re-download notebooks to sync tags without re-transcribing")
    parser.add_argument("--no-autolink", action="store_true", help="Disable auto-linking of vault note names in transcriptions")
    parser.add_argument("--no-blank-detect", action="store_true", help="Disable blank page detection (skip API calls for empty pages)")
    parser.add_argument("--extract-tasks", action="store_true", help="Extract action items using the configured task model")
    parser.add_argument("--infer-tags", action="store_true", help="Infer semantic tags using the configured tag model")
    parser.add_argument("--include-tag", action="append", default=[], help="Include ANY native tag (repeatable)")
    parser.add_argument("--exclude-tag", action="append", default=[], help="Exclude native tag; wins over includes (repeatable)")
    parser.add_argument("--pages", type=parse_page_ranges, help="1-based pages/closed ranges, e.g. 1,3-5")
    args = parser.parse_args()

    selection = None
    if args.include_tag or args.exclude_tag or args.pages is not None:
        if any(not value.strip() for value in args.include_tag + args.exclude_tag):
            parser.error("tag filters cannot be empty/whitespace")
        selection = Selection(tuple(args.include_tag), tuple(args.exclude_tag), args.pages or ())
        if args.merge_states or args.retag or args.list_only:
            parser.error("selection cannot be combined with --merge-states, --retag or --list-only")

    if args.dry_run and (args.merge_states or args.retag or args.list_only):
        parser.error("--dry-run cannot be combined with --merge-states, --retag or --list-only")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    config = load_config()

    # Merge mode: combine batch state files and exit
    if args.merge_states:
        merge_state_files(config["state_file"], args.merge_states)
        return

    provider_config = None
    if not (args.dry_run or args.list_only or args.retag):
        provider_config = resolve_provider_config()
        config.update(provider_config)

    state = load_state(config["state_file"])

    # Get notebook list
    if args.notebooks_json:
        notebooks = json.loads(Path(args.notebooks_json).read_text())
        log.info("Loaded %d notebooks from %s", len(notebooks), args.notebooks_json)
    else:
        log.info("Starting reMarkable sync (watch: %s)", config["watch_path"])
        notebooks = list_notebooks(config["rmapi_bin"], config["watch_path"])
        log.info("Found %d notebooks", len(notebooks))

    # List-only mode: save and exit
    if args.list_only:
        Path(args.list_only).write_text(json.dumps(notebooks, indent=2))
        log.info("Saved notebook list to %s", args.list_only)
        return

    # Lists loaded from JSON obey the same folder restriction as live discovery.
    watch = config["watch_path"].rstrip("/")
    if watch:
        notebooks = [nb for nb in notebooks if nb["path"] == watch or nb["path"].startswith(watch + "/")]

    # Filter ignored notebooks
    ignore_patterns = load_ignore_patterns()
    if ignore_patterns:
        before = len(notebooks)
        notebooks = [nb for nb in notebooks if not is_ignored(nb["name"], nb["path"], ignore_patterns)]
        skipped = before - len(notebooks)
        if skipped:
            log.info("Skipped %d notebooks matching .sync_ignore patterns", skipped)

    # Apply slice
    if args.slice:
        start, end = args.slice.split(":")
        start = int(start) if start else 0
        end = int(end) if end else len(notebooks)
        notebooks = notebooks[start:end]
        log.info("Processing slice [%d:%d] (%d notebooks)", start, end, len(notebooks))

    # Retag mode: download notebooks for tag extraction only, no transcription
    if args.retag:
        retag_notebooks(notebooks, config["obsidian_vault"], config["rmapi_bin"])
        return

    client = create_client(provider_config) if provider_config is not None else None

    # Build note index for auto-linking (unless disabled)
    note_index = None
    if not args.no_autolink:
        note_index = build_note_index(config["obsidian_vault"])
        log.info("Built note index: %d entries", len(note_index))

    # A2: Load custom prompts
    custom_prompts = _load_custom_prompts()
    if custom_prompts:
        log.info("Loaded %d custom prompt(s)", len(custom_prompts))

    summary = sync_notebooks(
        notebooks, state, config["obsidian_vault"], config["rmapi_bin"],
        config["model"], args.dry_run, client, config["state_file"],
        note_index=note_index,
        extract_tasks_enabled=args.extract_tasks,
        infer_tags_enabled=args.infer_tags,
        blank_detect=not args.no_blank_detect,
        custom_prompts=custom_prompts or None,
        selection=selection,
        task_model=config.get("task_model"),
        tag_model=config.get("tag_model"),
        diagram_model=config.get("diagram_model"),
    )

    # Write sync log (skip for dry-run or empty results)
    if not args.dry_run and summary.results:
        log_path = write_sync_log(config["obsidian_vault"], summary)
        if log_path is not None:
            log.info("Wrote sync log: %s", log_path)

    log.info("Sync complete: %d synced, %d skipped, %d errors",
             len(summary.synced), len(summary.skipped), len(summary.errored))


def main():
    try:
        _main()
    except ProviderError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    except Exception:
        log.error("Sync failed (operation failed; sensitive diagnostics omitted)")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
