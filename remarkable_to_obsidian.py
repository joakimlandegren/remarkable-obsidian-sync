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
    """List all notebooks under watch_path using rmapi find + stat."""
    # Always search from root so paths are absolute
    result = subprocess.run(
        [rmapi_bin, "find", "/", ".*"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("rmapi find failed (exit %d). Run `rmapi` to authenticate.", result.returncode)
        sys.exit(1)

    notebooks = []
    watch_norm = watch_path.rstrip("/")
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line.startswith("[f]"):
            continue
        # Extract path: "[f] /path/to/notebook"
        path = line[4:].strip()

        # Skip trash
        if path.startswith("/trash/"):
            continue

        # Filter by watch_path
        if watch_norm != "" and watch_norm != "/":
            if path != watch_norm and not path.startswith(watch_norm + "/"):
                continue

        # Get metadata via stat
        stat_result = subprocess.run(
            [rmapi_bin, "stat", path],
            capture_output=True, text=True,
        )
        if stat_result.returncode != 0:
            log.warning("Failed to stat %s, skipping", path)
            continue

        try:
            meta = json.loads(stat_result.stdout)
        except json.JSONDecodeError:
            log.warning("Failed to parse stat output for %s, skipping", path)
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
        log.error("Failed to export %s: %s", notebook_path, result.stderr)
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
        png_data = _svg_to_png(svg_path)
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


MAX_IMAGE_DIM = 8000


def _svg_to_png(svg_path: Path) -> bytes:
    """Convert an SVG file to PNG bytes using cairosvg. Scales down if needed to fit API limits."""
    import cairosvg

    # Read SVG to check dimensions
    svg_text = svg_path.read_text()
    match = re.search(r'height="(\d+)"', svg_text)
    svg_height = int(match.group(1)) if match else RM_HEIGHT
    match = re.search(r'width="(\d+)"', svg_text)
    svg_width = int(match.group(1)) if match else RM_WIDTH

    # Scale down if either dimension exceeds API max (8000px)
    output_width = svg_width
    if svg_height > MAX_IMAGE_DIM or svg_width > MAX_IMAGE_DIM:
        scale = min(MAX_IMAGE_DIM / svg_height, MAX_IMAGE_DIM / svg_width)
        output_width = int(svg_width * scale)

    result = cairosvg.svg2png(url=str(svg_path), output_width=output_width)
    assert isinstance(result, bytes)
    return result


def _encode_page_image(page_path: Path) -> tuple[str, str]:
    """Encode a page file to base64 and return (data, media_type)."""
    suffix = page_path.suffix.lower()
    if suffix == ".svg":
        png_data = _svg_to_png(page_path)
        return base64.standard_b64encode(png_data).decode("utf-8"), "image/png"
    elif suffix == ".png":
        return base64.standard_b64encode(page_path.read_bytes()).decode("utf-8"), "image/png"
    else:
        return base64.standard_b64encode(page_path.read_bytes()).decode("utf-8"), "application/pdf"


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
    page_data, media_type = _encode_page_image(page_path)

    prompt = system_prompt or SINGLE_PAGE_PROMPT
    user_content: list[dict] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": page_data},
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


def transcribe_pages(client, page_paths: list[Path], model: str) -> str:
    """Send page images (SVG/PNG) to Claude for transcription. Returns markdown string."""
    content = []
    for page_path in page_paths:
        page_data, media_type = _encode_page_image(page_path)
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
            log.warning("Mermaid validation failed (attempt %d): %s", attempt + 1, error)
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
    page_paths: list[Path],
    vault_path: str,
    notebook_name: str,
    client=None,
    model: str = "",
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
        try:
            # Load the page as a PIL Image
            if page_path.suffix.lower() == ".svg":
                png_data = _svg_to_png(page_path)
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

            crop_name = f"{safe_name} - diagram {crop_counter}.png"
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
                        excalidraw_name = f"{safe_name} - diagram {crop_counter}.excalidraw.md"
                        excalidraw_path = attachments / excalidraw_name
                        _write_excalidraw_file(content, excalidraw_path, description)
                        saved_crops.append(excalidraw_name)
                        log.info("Converted diagram %d to Excalidraw", crop_counter)
                        return f"> *{description}*\n\n![[{excalidraw_name}]]"
                    # fallback_png: fall through to image embed
                except Exception:
                    log.warning("Failed to convert diagram %d to vector, using image", crop_counter, exc_info=True)

            # Fallback: embed cropped image
            return f"> *{description}*\n\n![[{crop_name}]]"
        except Exception:
            log.warning("Failed to crop diagram from page %d", page_num, exc_info=True)
            return match.group(0)

    updated_markdown = DIAGRAM_RE.sub(replace_diagram, markdown)
    return updated_markdown, saved_crops


def sanitize_filename(name: str) -> str:
    """Keep alphanumeric, spaces, hyphens, underscores; replace others with _."""
    return re.sub(r"[^a-zA-Z0-9 _-]", "_", name)


def save_source_pages(vault_path: str, notebook: dict, page_paths: list[Path], changed_indices: set[int] | None = None) -> list[str]:
    """Save source page images to the vault Attachments folder. Returns list of filenames.

    When *changed_indices* is provided, only pages whose index is in the set
    are actually written to disk.  Filenames for unchanged pages are still
    returned so the markdown note can embed all pages.
    """
    attachments = Path(vault_path) / "Attachments" / "reMarkable"
    attachments.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(notebook["name"])
    saved = []
    for i, page_path in enumerate(page_paths):
        # Determine the destination filename regardless of whether we write
        suffix = page_path.suffix.lower() if page_path else ".svg"
        if suffix == ".pdf":
            dest_name = f"{safe_name}.pdf"
        else:
            dest_name = f"{safe_name} - page {i+1}.png"

        # Only write to disk if this page changed (or no filter given)
        if changed_indices is None or i in changed_indices:
            if page_path is None:
                saved.append(dest_name)
                continue
            if suffix == ".svg":
                png_data = _svg_to_png(page_path)
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


def write_obsidian_note(vault_path: str, notebook: dict, markdown: str, source_files: list[str] | None = None, tasks: list[dict] | None = None) -> Path:
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

TASK_MODEL = "claude-haiku-4-5-20241022"


def extract_tasks(client, markdown: str, model: str = TASK_MODEL) -> list[dict]:
    """Extract action items from markdown text using a lightweight model. Returns list of task dicts."""
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


def infer_tags(client, markdown: str, model: str = TASK_MODEL) -> list[str]:
    """Infer semantic tags from markdown content using a lightweight model."""
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
) -> SyncSummary:
    """Process each notebook: skip unchanged, export, transcribe changed pages, write."""
    summary = SyncSummary()
    for nb in notebooks:
        nb_state = state.get(nb["id"], {})

        # Legacy state format migration: old format was {id: version}
        if not isinstance(nb_state, dict):
            nb_state = {}

        # Detect path or name changes (notebook moved/renamed in reMarkable)
        old_path = nb_state.get("path")
        old_name = nb_state.get("name")
        if old_path and (old_path != nb["path"] or old_name != nb["name"]):
            _move_obsidian_note(vault_path, old_path, old_name or Path(old_path).name, nb)
        elif not old_path and nb_state:
            # Legacy state without stored path - search for existing note by name
            _move_obsidian_note_legacy(vault_path, nb)

        if nb_state.get("version") == nb["version"] and nb["version"] != 0:
            # Update stored path/name even when skipping content sync.
            # Skip the fast-path when version is 0 - the reMarkable API
            # sometimes never increments from 0, so we fall through to
            # page-level hash comparison to detect actual content changes.
            state[nb["id"]] = {**nb_state, "path": nb["path"], "name": nb["name"]}
            save_state(state_file, state)
            log.info("Skipping %s (unchanged, version %s)", nb["name"], nb["version"])
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
            rm_pages = _extract_rm_pages(rmapi_bin, nb["path"], tmp_dir)

            # Merge page tags from .content file (rmapi stat doesn't return these)
            content_tags = _content_tags.pop(nb["path"], [])
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
                if dry_run:
                    log.info("[DRY RUN] Would transcribe %s (PDF, %d bytes)", nb["name"], pdfs[0].stat().st_size)
                    summary.results.append(SyncResult(nb["name"], nb["path"], "dry_run", "PDF"))
                    continue
                # A1: Build context for PDF
                pdf_context = f"Notebook: {nb['name']}\nPath: {nb.get('path', '')}"
                markdown = transcribe_pdf(client, pdfs[0], model, context=pdf_context, system_prompt=nb_prompt)
                markdown, _ = extract_diagram_crops(markdown, source_pdfs, vault_path, nb["name"], client=client, model=model)
                if note_index:
                    markdown = autolink_markdown(markdown, note_index)
                # B2: Extract tasks if enabled
                tasks = None
                if extract_tasks_enabled:
                    tasks = extract_tasks(client, markdown)
                    if tasks:
                        markdown = _inject_tasks(markdown, tasks)
                # B3: Infer tags if enabled
                if infer_tags_enabled:
                    inferred = infer_tags(client, markdown)
                    if inferred:
                        existing_tags = nb.get("tags", [])
                        nb["tags"] = list({*existing_tags, *inferred})
                source_files = save_source_pages(vault_path, nb, source_pdfs)
                nb["page_count"] = len(source_pdfs)
                note_path = write_obsidian_note(vault_path, nb, markdown, source_files, tasks=tasks)
                log.info("Wrote %s (%d chars)", note_path, len(markdown))
                state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"]}
                save_state(state_file, state)
                summary.results.append(SyncResult(nb["name"], nb["path"], "synced", f"{len(pdfs)} PDF pages"))
                continue

            if not rm_pages:
                continue

            # Per-page change detection
            cached_pages = nb_state.get("pages", {})
            page_hashes = {}
            changed_indices = []

            for i, rm_file in enumerate(rm_pages):
                h = _hash_file(rm_file)
                page_hashes[str(i)] = h
                cached = cached_pages.get(str(i), {})
                if cached.get("hash") != h:
                    changed_indices.append(i)

            # Detect removed pages
            current_count = len(rm_pages)
            old_count = len(cached_pages)
            removed = current_count < old_count

            if not changed_indices and not removed:
                log.info("Skipping %s (no page content changed)", nb["name"])
                state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"], "pages": cached_pages}
                save_state(state_file, state)
                summary.results.append(SyncResult(nb["name"], nb["path"], "skipped", "no page content changed"))
                continue

            if dry_run:
                log.info("[DRY RUN] Would transcribe %d/%d changed pages in %s",
                         len(changed_indices), len(rm_pages), nb["name"])
                summary.results.append(SyncResult(nb["name"], nb["path"], "dry_run", f"{len(changed_indices)}/{len(rm_pages)} pages changed"))
                continue

            # Render only changed pages to SVG (skip unchanged to save CPU)
            svg_paths = [None] * len(rm_pages)
            for i in changed_indices:
                svg_content = _render_rm_to_svg(rm_pages[i])
                svg_path = tmp_dir / f"page_{i+1}.svg"
                svg_path.write_text(svg_content)
                svg_paths[i] = svg_path

            # Transcribe only changed pages, reuse cached markdown for others
            page_markdowns = {}
            prev_markdown = ""
            for i in range(len(rm_pages)):
                if i in changed_indices:
                    # D1: Skip blank pages
                    if blank_detect and is_blank_page(svg_paths[i]):
                        log.info("Skipping page %d/%d (blank)", i + 1, len(rm_pages))
                        page_markdowns[str(i)] = ""
                        continue
                    log.info("Transcribing page %d/%d (changed)", i + 1, len(rm_pages))
                    # A1: Build context for this page
                    ctx = _build_page_context(nb, i, len(rm_pages), prev_markdown=prev_markdown)
                    md = transcribe_page(client, svg_paths[i], model, context=ctx, system_prompt=nb_prompt)
                    # Fix diagram page references to use page=1 since we send one at a time
                    md = md.replace("page=1", f"page={i+1}")
                    page_markdowns[str(i)] = md
                else:
                    log.info("Reusing cached transcription for page %d/%d", i + 1, len(rm_pages))
                    page_markdowns[str(i)] = cached_pages[str(i)]["markdown"]
                prev_markdown = page_markdowns[str(i)]

            # Assemble full markdown
            all_parts = [page_markdowns[str(i)] for i in range(len(rm_pages))]
            markdown = "\n\n---\n\n".join(all_parts)

            # Extract and crop any diagram regions
            markdown, _ = extract_diagram_crops(markdown, svg_paths, vault_path, nb["name"], client=client, model=model)

            if note_index:
                markdown = autolink_markdown(markdown, note_index)

            # B2: Extract tasks if enabled
            tasks = None
            if extract_tasks_enabled:
                tasks = extract_tasks(client, markdown)
                if tasks:
                    markdown = _inject_tasks(markdown, tasks)

            # B3: Infer tags if enabled
            if infer_tags_enabled:
                inferred = infer_tags(client, markdown)
                if inferred:
                    existing_tags = nb.get("tags", [])
                    nb["tags"] = list({*existing_tags, *inferred})

            source_files = save_source_pages(vault_path, nb, svg_paths, changed_indices=set(changed_indices))
            nb["page_count"] = len(rm_pages)
            note_path = write_obsidian_note(vault_path, nb, markdown, source_files, tasks=tasks)
            log.info("Wrote %s (%d chars, %d/%d pages transcribed)",
                     note_path, len(markdown), len(changed_indices), len(rm_pages))
            summary.results.append(SyncResult(nb["name"], nb["path"], "synced", f"{len(changed_indices)}/{len(rm_pages)} pages transcribed"))

            # Save per-page state
            new_pages = {}
            for i in range(len(rm_pages)):
                new_pages[str(i)] = {
                    "hash": page_hashes[str(i)],
                    "markdown": page_markdowns[str(i)],
                }
            state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"], "pages": new_pages}
            save_state(state_file, state)
        except Exception as exc:
            log.error("Failed to process %s", nb["name"], exc_info=True)
            summary.results.append(SyncResult(nb["name"], nb["path"], "errored", str(exc)))
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


def write_sync_log(vault_path: str, summary: SyncSummary) -> Path:
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
            lines.append(f"- {r.name} - {r.detail}")
        lines.append("")

    new_entry = "\n".join(lines)

    # Parse existing entries and prepend
    existing_entries: list[str] = []
    if log_file.exists():
        content = log_file.read_text()
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


def main():
    parser = argparse.ArgumentParser(description="Sync reMarkable notebooks to Obsidian")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--list-only", metavar="FILE", help="List notebooks to JSON file and exit")
    parser.add_argument("--notebooks-json", metavar="FILE", help="Read notebook list from JSON instead of rmapi")
    parser.add_argument("--slice", metavar="START:END", help="Process only notebooks[START:END]")
    parser.add_argument("--merge-states", nargs="+", metavar="FILE", help="Merge state files into main state and exit")
    parser.add_argument("--retag", action="store_true", help="Re-download notebooks to sync tags without re-transcribing")
    parser.add_argument("--no-autolink", action="store_true", help="Disable auto-linking of vault note names in transcriptions")
    parser.add_argument("--no-blank-detect", action="store_true", help="Disable blank page detection (skip API calls for empty pages)")
    parser.add_argument("--extract-tasks", action="store_true", help="Extract action items from transcriptions using Haiku")
    parser.add_argument("--infer-tags", action="store_true", help="Infer semantic tags from transcriptions using Haiku")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    config = load_config()

    # Merge mode: combine batch state files and exit
    if args.merge_states:
        merge_state_files(config["state_file"], args.merge_states)
        return

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

    client = None
    if not args.dry_run:
        import anthropic
        if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
            from anthropic import AnthropicVertex
            project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            if not project_id:
                log.error("ANTHROPIC_VERTEX_PROJECT_ID must be set for Vertex AI")
                sys.exit(1)
            client = AnthropicVertex(
                project_id=project_id,
                region=os.environ.get("CLOUD_ML_REGION", "europe-west1"),
            )
        else:
            client = anthropic.Anthropic()

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
    )

    # Write sync log (skip for dry-run or empty results)
    if not args.dry_run and summary.results:
        log_path = write_sync_log(config["obsidian_vault"], summary)
        log.info("Wrote sync log: %s", log_path)

    log.info("Sync complete: %d synced, %d skipped, %d errors",
             len(summary.synced), len(summary.skipped), len(summary.errored))


if __name__ == "__main__":
    main()
