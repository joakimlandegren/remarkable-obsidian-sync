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
from pathlib import Path

from rmscene import read_blocks, SceneLineItemBlock


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


TRANSCRIPTION_PROMPT = """Transcribe all handwritten text in this document to clean markdown.

The images are numbered in order starting from 1 (image 1, image 2, etc.).

Rules:
- Infer structure: use headings, bullet lists, numbered lists as appropriate
- For diagrams, sketches, or graphical elements that cannot be represented as text, output:
  > [Diagram(page=P, top=T, bottom=B): description]
  where P is the image number (1-based), T and B are the vertical position as
  percentages (0=top of image, 100=bottom). This will be used to crop and embed
  that region of the source image.
- Mark illegible sections as *[illegible]*
- Output ONLY the markdown transcription, no preamble or explanation"""


def transcribe_pdf(client, pdf_path: Path, model: str) -> str:
    """Send a PDF to Claude for handwriting transcription. Returns markdown string."""
    pdf_data = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data,
                        },
                    },
                    {"type": "text", "text": TRANSCRIPTION_PROMPT},
                ],
            }
        ],
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
- For diagrams, sketches, or graphical elements that cannot be represented as text, output:
  > [Diagram(page=1, top=T, bottom=B): description]
  where T and B are the vertical position as percentages (0=top, 100=bottom).
  This will be used to crop and embed that region of the source image.
- Mark illegible sections as *[illegible]*
- Output ONLY the markdown transcription, no preamble or explanation"""


def transcribe_page(client, page_path: Path, model: str) -> str:
    """Send a single page image to Claude for transcription. Returns markdown string."""
    page_data, media_type = _encode_page_image(page_path)

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": page_data},
                },
                {"type": "text", "text": SINGLE_PAGE_PROMPT},
            ],
        }],
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

    content.append({"type": "text", "text": TRANSCRIPTION_PROMPT})

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        messages=[{"role": "user", "content": content}],
    )
    return message.content[0].text


DIAGRAM_RE = re.compile(
    r'> \[Diagram\(page=(\d+),\s*top=(\d+),\s*bottom=(\d+)\):\s*(.+?)\]',
    re.DOTALL,
)

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
    Returns (format, content) where format is 'mermaid' or 'excalidraw'."""
    image_data = base64.standard_b64encode(crop_image).decode("utf-8")

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_data},
                },
                {"type": "text", "text": DIAGRAM_CONVERT_PROMPT},
            ],
        }],
    )

    response = message.content[0].text.strip()
    lines = response.split("\n", 1)

    if lines[0].startswith("FORMAT: mermaid"):
        content = lines[1].strip() if len(lines) > 1 else ""
        # Strip outer ```mermaid fences if present
        content = re.sub(r'^```mermaid\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content)
        return "mermaid", content
    elif lines[0].startswith("FORMAT: excalidraw"):
        content = lines[1].strip() if len(lines) > 1 else ""
        # Strip outer ```json fences if present
        content = re.sub(r'^```json\s*\n?', '', content)
        content = re.sub(r'\n?```\s*$', '', content)
        return "excalidraw", content
    else:
        # Fallback: try to detect from content
        if "```mermaid" in response or "graph " in response or "flowchart " in response:
            content = re.sub(r'^```mermaid\s*\n?', '', response)
            content = re.sub(r'\n?```\s*$', '', content)
            return "mermaid", content
        return "excalidraw", response


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

        if page_num < 1 or page_num > len(page_paths):
            return match.group(0)  # leave as-is if page out of range

        page_path = page_paths[page_num - 1]
        try:
            # Load the PNG (convert SVG first if needed)
            if page_path.suffix.lower() == ".svg":
                png_data = _svg_to_png(page_path)
                img = Image.open(io.BytesIO(png_data))
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
                    if fmt == "mermaid" and content.strip():
                        log.info("Converted diagram %d to Mermaid", crop_counter)
                        return f"> *{description}*\n\n```mermaid\n{content}\n```"
                    elif fmt == "excalidraw" and content.strip():
                        excalidraw_name = f"{safe_name} - diagram {crop_counter}.excalidraw.md"
                        excalidraw_path = attachments / excalidraw_name
                        _write_excalidraw_file(content, excalidraw_path, description)
                        saved_crops.append(excalidraw_name)
                        log.info("Converted diagram %d to Excalidraw", crop_counter)
                        return f"> *{description}*\n\n![[{excalidraw_name}]]"
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


def save_source_pages(vault_path: str, notebook: dict, page_paths: list[Path]) -> list[str]:
    """Save source page images to the vault Attachments folder. Returns list of filenames."""
    attachments = Path(vault_path) / "Attachments" / "reMarkable"
    attachments.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(notebook["name"])
    saved = []
    for i, page_path in enumerate(page_paths):
        suffix = page_path.suffix.lower()
        if suffix == ".svg":
            # Convert SVG to PNG for display in Obsidian
            png_data = _svg_to_png(page_path)
            dest_name = f"{safe_name} - page {i+1}.png"
            (attachments / dest_name).write_bytes(png_data)
        elif suffix == ".pdf":
            dest_name = f"{safe_name}.pdf"
            shutil.copy2(page_path, attachments / dest_name)
        else:
            dest_name = f"{safe_name} - page {i+1}{suffix}"
            shutil.copy2(page_path, attachments / dest_name)
        saved.append(dest_name)
        log.info("Saved source: %s", dest_name)

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


def write_obsidian_note(vault_path: str, notebook: dict, markdown: str, source_files: list[str] | None = None) -> Path:
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


def sync_notebooks(
    notebooks: list[dict],
    state: dict,
    vault_path: str,
    rmapi_bin: str,
    model: str,
    dry_run: bool,
    client,
    state_file: str,
) -> None:
    """Process each notebook: skip unchanged, export, transcribe changed pages, write."""
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
            # Legacy state without stored path — search for existing note by name
            _move_obsidian_note_legacy(vault_path, nb)

        if nb_state.get("version") == nb["version"]:
            # Update stored path/name even when skipping content sync
            state[nb["id"]] = {**nb_state, "path": nb["path"], "name": nb["name"]}
            save_state(state_file, state)
            log.info("Skipping %s (unchanged, version %s)", nb["name"], nb["version"])
            continue

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
                # PDF-based notebook — no per-page tracking, full re-transcription
                pdfs = list(tmp_dir.glob("*.pdf"))
                if not pdfs:
                    continue
                if dry_run:
                    log.info("[DRY RUN] Would transcribe %s (PDF, %d bytes)", nb["name"], pdfs[0].stat().st_size)
                    continue
                markdown = transcribe_pdf(client, pdfs[0], model)
                markdown, _ = extract_diagram_crops(markdown, pdfs, vault_path, nb["name"], client=client, model=model)
                source_files = save_source_pages(vault_path, nb, pdfs)
                nb["page_count"] = len(pdfs)
                note_path = write_obsidian_note(vault_path, nb, markdown, source_files)
                log.info("Wrote %s (%d chars)", note_path, len(markdown))
                state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"]}
                save_state(state_file, state)
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
                continue

            if dry_run:
                log.info("[DRY RUN] Would transcribe %d/%d changed pages in %s",
                         len(changed_indices), len(rm_pages), nb["name"])
                continue

            # Render all pages to SVG (needed for source images)
            svg_paths = []
            for i, rm_file in enumerate(rm_pages):
                svg_content = _render_rm_to_svg(rm_file)
                svg_path = tmp_dir / f"page_{i+1}.svg"
                svg_path.write_text(svg_content)
                svg_paths.append(svg_path)

            # Transcribe only changed pages, reuse cached markdown for others
            page_markdowns = {}
            for i in range(len(rm_pages)):
                if i in changed_indices:
                    log.info("Transcribing page %d/%d (changed)", i + 1, len(rm_pages))
                    md = transcribe_page(client, svg_paths[i], model)
                    # Fix diagram page references to use page=1 since we send one at a time
                    md = md.replace("page=1", f"page={i+1}")
                    page_markdowns[str(i)] = md
                else:
                    log.info("Reusing cached transcription for page %d/%d", i + 1, len(rm_pages))
                    page_markdowns[str(i)] = cached_pages[str(i)]["markdown"]

            # Assemble full markdown
            all_parts = [page_markdowns[str(i)] for i in range(len(rm_pages))]
            markdown = "\n\n---\n\n".join(all_parts)

            # Extract and crop any diagram regions
            markdown, _ = extract_diagram_crops(markdown, svg_paths, vault_path, nb["name"], client=client, model=model)

            source_files = save_source_pages(vault_path, nb, svg_paths)
            nb["page_count"] = len(rm_pages)
            note_path = write_obsidian_note(vault_path, nb, markdown, source_files)
            log.info("Wrote %s (%d chars, %d/%d pages transcribed)",
                     note_path, len(markdown), len(changed_indices), len(rm_pages))

            # Save per-page state
            new_pages = {}
            for i in range(len(rm_pages)):
                new_pages[str(i)] = {
                    "hash": page_hashes[str(i)],
                    "markdown": page_markdowns[str(i)],
                }
            state[nb["id"]] = {"version": nb["version"], "path": nb["path"], "name": nb["name"], "pages": new_pages}
            save_state(state_file, state)
        except Exception:
            log.error("Failed to process %s", nb["name"], exc_info=True)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def merge_state_files(target_file: str, source_files: list[str]) -> None:
    """Merge multiple state files into the target, combining notebook entries."""
    merged = load_state(target_file)
    for src in source_files:
        src_state = load_state(src)
        merged.update(src_state)
    save_state(target_file, merged)
    log.info("Merged %d state files into %s (%d notebooks)", len(source_files), target_file, len(merged))


def main():
    parser = argparse.ArgumentParser(description="Sync reMarkable notebooks to Obsidian")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--list-only", metavar="FILE", help="List notebooks to JSON file and exit")
    parser.add_argument("--notebooks-json", metavar="FILE", help="Read notebook list from JSON instead of rmapi")
    parser.add_argument("--slice", metavar="START:END", help="Process only notebooks[START:END]")
    parser.add_argument("--merge-states", nargs="+", metavar="FILE", help="Merge state files into main state and exit")
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
    sync_notebooks(
        notebooks, state, config["obsidian_vault"], config["rmapi_bin"],
        config["model"], args.dry_run, client, config["state_file"],
    )

    log.info("Sync complete.")


if __name__ == "__main__":
    main()
