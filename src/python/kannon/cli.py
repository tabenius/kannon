from __future__ import annotations

import argparse
import base64
import getpass
import io
import os
import pwd
import re
import select
import subprocess
import shutil
import sys
import tempfile
import termios
import textwrap
import tty
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from PIL import Image, ImageDraw, ImageFont


SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown"}
PDF_EXTENSIONS = {".pdf"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
CACHE_VERSION = 1


@dataclass(frozen=True)
class RenderMode:
    name: str
    forced: bool = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = [Path(p).expanduser() for p in args.paths] or [Path.cwd()]
    cache_path = Path(args.cache).expanduser()
    if not cache_path.is_absolute():
        cache_path = Path.cwd() / cache_path

    documents = discover_documents(paths)
    if not documents:
        print("No supported documents found. Kannon currently scans PDF and Markdown files.")
        return 1

    cache = load_cache(cache_path)
    records = build_records(
        documents=documents,
        cache=cache,
        thumbnail_size=args.size,
        refresh=args.refresh,
    )
    save_cache(cache_path, records, args.size)

    if args.scan_only:
        print(f"Wrote {len(records)} document record(s) to {cache_path}")
        return 0

    mode = choose_render_mode(args)
    if args.no_tui or not (sys.stdin.isatty() and sys.stdout.isatty()):
        render_document(records[0], mode)
        return 0

    run_tui(records, mode)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kannon",
        description="Build thumbnails and browse document metadata in the terminal.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=512,
        help="Maximum thumbnail edge in pixels before terminal scaling. Default: 512.",
    )
    parser.add_argument(
        "--cache",
        default="kannon.yaml",
        help="YAML cache file for metadata and base64 thumbnail PNGs. Default: kannon.yaml.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Regenerate thumbnails and metadata even when cached source stats match.",
    )
    parser.add_argument(
        "--ansi",
        action="store_true",
        help="Force ANSI block rendering instead of sixel.",
    )
    parser.add_argument(
        "--sixel",
        action="store_true",
        help="Force sixel output even if terminal detection is unsure.",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Render the first record once and exit instead of opening the arrow-key TUI.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan and update kannon.yaml; do not render a preview.",
    )
    return parser


def discover_documents(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if not path.exists():
            print(f"warning: {path} does not exist", file=sys.stderr)
            continue
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.name == "kannon.yaml":
                continue
            if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                found.append(resolved)
                seen.add(resolved)
    return sorted(found, key=lambda p: str(p).lower())


def load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}
    with cache_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    return data


def save_cache(cache_path: Path, records: list[dict[str, Any]], thumbnail_size: int) -> None:
    payload = {
        "version": CACHE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thumbnail_size": thumbnail_size,
        "documents": records,
    }
    with cache_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=False, width=100)


def build_records(
    documents: list[Path],
    cache: dict[str, Any],
    thumbnail_size: int,
    refresh: bool,
) -> list[dict[str, Any]]:
    old_records = {
        record.get("path_abs"): record
        for record in cache.get("documents", [])
        if isinstance(record, dict) and record.get("path_abs")
    }
    records: list[dict[str, Any]] = []
    for document in documents:
        stat = document.stat()
        old = old_records.get(str(document))
        if old and not refresh and record_is_current(old, stat, thumbnail_size):
            records.append(old)
            continue
        try:
            records.append(index_document(document, stat, thumbnail_size))
        except Exception as exc:
            records.append(error_record(document, stat, exc))
    return records


def record_is_current(record: dict[str, Any], stat: os.stat_result, thumbnail_size: int) -> bool:
    source = record.get("source", {})
    return (
        source.get("mtime_ns") == stat.st_mtime_ns
        and source.get("size_bytes") == stat.st_size
        and record.get("thumbnail", {}).get("max_edge") == thumbnail_size
    )


def index_document(document: Path, stat: os.stat_result, thumbnail_size: int) -> dict[str, Any]:
    suffix = document.suffix.lower()
    if suffix in PDF_EXTENSIONS:
        metadata, image = render_pdf(document, thumbnail_size)
        kind = "pdf"
    elif suffix in MARKDOWN_EXTENSIONS:
        metadata, image = render_markdown(document, thumbnail_size)
        kind = "markdown"
    else:
        raise ValueError(f"unsupported document type: {suffix}")

    png = image_to_png_bytes(image)
    owner = owner_name(stat.st_uid)
    current_user = getpass.getuser()
    title = clean_metadata_value(metadata.get("title")) or document.stem

    record = {
        "path": display_path(document),
        "path_abs": str(document),
        "kind": kind,
        "title": title,
        "source": {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "owner_user": owner,
        },
        "metadata": compact_metadata(metadata),
        "thumbnail": {
            "media_type": "image/png",
            "encoding": "base64",
            "max_edge": thumbnail_size,
            "width": image.width,
            "height": image.height,
            "data": base64.b64encode(png).decode("ascii"),
        },
    }
    if owner and owner != current_user:
        record["user_name"] = owner
    return record


def error_record(document: Path, stat: os.stat_result, exc: Exception) -> dict[str, Any]:
    owner = owner_name(stat.st_uid)
    record: dict[str, Any] = {
        "path": display_path(document),
        "path_abs": str(document),
        "kind": document.suffix.lower().lstrip(".") or "unknown",
        "title": document.stem,
        "source": {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "owner_user": owner,
        },
        "metadata": {"error": str(exc)},
    }
    if owner and owner != getpass.getuser():
        record["user_name"] = owner
    return record


def render_pdf(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    try:
        return render_pdf_with_pymupdf(document, thumbnail_size)
    except ModuleNotFoundError:
        return render_pdf_with_poppler(document, thumbnail_size)


def render_pdf_with_pymupdf(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    import fitz

    with fitz.open(document) as pdf:
        if pdf.page_count < 1:
            raise ValueError("PDF has no pages")
        page = pdf.load_page(0)
        rect = page.rect
        scale = thumbnail_size / max(rect.width, rect.height)
        scale = min(max(scale, 0.1), 4.0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
        image.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
        metadata = dict(pdf.metadata or {})
        metadata.update(
            {
                "page_count": pdf.page_count,
                "format": metadata.get("format") or "PDF",
                "page_1_size_points": f"{rect.width:.1f} x {rect.height:.1f}",
            }
        )
        return metadata, image


def render_pdf_with_poppler(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    if not shutil.which("pdftoppm"):
        raise RuntimeError(
            "PDF rendering requires PyMuPDF or the poppler-utils command 'pdftoppm'"
        )
    metadata = pdfinfo_metadata(document)
    with tempfile.TemporaryDirectory(prefix="kannon-pdf-") as temp_dir:
        prefix = Path(temp_dir) / "page"
        command = [
            "pdftoppm",
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-png",
            "-scale-to",
            str(thumbnail_size),
            str(document),
            str(prefix),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        image_path = prefix.with_suffix(".png")
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
    metadata.setdefault("renderer", "poppler-utils")
    return metadata, image


def pdfinfo_metadata(document: Path) -> dict[str, Any]:
    if not shutil.which("pdfinfo"):
        return {}
    result = subprocess.run(
        ["pdfinfo", str(document)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    metadata: dict[str, Any] = {}
    if result.returncode != 0:
        return metadata
    key_map = {
        "Title": "title",
        "Subject": "subject",
        "Author": "author",
        "Creator": "creator",
        "Producer": "producer",
        "CreationDate": "creationDate",
        "ModDate": "modDate",
        "Pages": "page_count",
        "PDF version": "format",
        "Page size": "page_1_size_points",
    }
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        mapped = key_map.get(key.strip())
        if not mapped:
            continue
        value = value.strip()
        if mapped == "page_count" and value.isdigit():
            metadata[mapped] = int(value)
        elif mapped == "format":
            metadata[mapped] = f"PDF {value}"
        else:
            metadata[mapped] = value
    return metadata


def render_markdown(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    text = document.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    title = markdown_title(lines) or document.stem
    metadata = {
        "title": title,
        "line_count": len(lines),
        "word_count": len(re.findall(r"\S+", text)),
        "renderer": "lightweight-pillow-markdown",
    }
    image = draw_markdown_page(lines[:80], thumbnail_size, title)
    return metadata, image


def markdown_title(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#\s+(.+)$", stripped)
        if heading:
            return heading.group(1).strip()
        return stripped[:80]
    return None


def draw_markdown_page(lines: list[str], thumbnail_size: int, title: str) -> Image.Image:
    width = thumbnail_size
    height = int(thumbnail_size * 1.33)
    image = Image.new("RGB", (width, height), "#fbfbf8")
    draw = ImageDraw.Draw(image)
    font_regular = load_font(16)
    font_bold = load_font(22)
    font_code = load_font(14, monospace=True)

    margin = max(24, width // 18)
    y = margin
    draw.text((margin, y), title[:70], fill="#111827", font=font_bold)
    y += 38
    draw.line((margin, y, width - margin, y), fill="#d1d5db", width=1)
    y += 18

    in_code = False
    for raw_line in lines:
        if y > height - margin:
            break
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            y += 8
            continue
        if not line:
            y += 12
            continue
        font = font_code if in_code else font_regular
        fill = "#374151"
        prefix = ""
        if not in_code:
            heading = re.match(r"^(#{1,3})\s+(.+)$", line)
            if heading:
                line = heading.group(2)
                font = load_font(18)
                fill = "#111827"
            elif re.match(r"^\s*[-*]\s+", line):
                line = re.sub(r"^\s*[-*]\s+", "", line)
                prefix = "- "
        wrap_width = max(20, (width - margin * 2) // 9)
        for wrapped in textwrap.wrap(line, width=wrap_width) or [""]:
            if y > height - margin:
                break
            draw.text((margin, y), prefix + wrapped, fill=fill, font=font)
            y += 20 if font == font_code else 22
            prefix = "  " if prefix else ""
    return image


def load_font(size: int, monospace: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        ]
        if monospace
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        ]
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in metadata.items():
        cleaned = clean_metadata_value(value)
        if cleaned is None:
            continue
        if key.lower() in {"creationdate", "moddate"}:
            compact[key] = normalize_pdf_date(cleaned) or cleaned
        else:
            compact[key] = cleaned
    return compact


def clean_metadata_value(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    return text or None


def normalize_pdf_date(value: str) -> str | None:
    match = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    second = int(match.group(6) or 0)
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        return None


def owner_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def choose_render_mode(args: argparse.Namespace) -> RenderMode:
    if args.ansi and args.sixel:
        raise SystemExit("--ansi and --sixel are mutually exclusive")
    if args.ansi:
        return RenderMode("ansi", forced=True)
    if args.sixel:
        return RenderMode("sixel", forced=True)
    if terminal_supports_sixel():
        return RenderMode("sixel")
    return RenderMode("ansi")


def terminal_supports_sixel() -> bool:
    if os.environ.get("KANNON_FORCE_SIXEL"):
        return True
    term = os.environ.get("TERM", "").lower()
    program = os.environ.get("TERM_PROGRAM", "").lower()
    indicators = [
        "sixel" in term,
        "mlterm" in term,
        "xterm" in term and os.environ.get("XTERM_VERSION"),
        "wezterm" in program or os.environ.get("WEZTERM_EXECUTABLE"),
        "foot" in term,
        "contour" in term,
        bool(os.environ.get("KONSOLE_VERSION")),
    ]
    return any(indicators)


def run_tui(records: list[dict[str, Any]], mode: RenderMode) -> None:
    index = 0
    with RawTerminal():
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        try:
            while True:
                render_document(records[index], mode, index=index, total=len(records))
                key = read_key()
                if key in {"q", "Q", "\x03", "\x04"}:
                    break
                if key in {"right", "down", "j", "n", " "}:
                    index = min(index + 1, len(records) - 1)
                elif key in {"left", "up", "k", "p"}:
                    index = max(index - 1, 0)
                elif key == "home":
                    index = 0
                elif key == "end":
                    index = len(records) - 1
        finally:
            sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()


class RawTerminal:
    def __enter__(self) -> "RawTerminal":
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd) if sys.stdin.isatty() else None
        if self.old is not None:
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def read_key() -> str:
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return "esc"
    seq = sys.stdin.read(1)
    if seq != "[":
        return "esc"
    tail = sys.stdin.read(1)
    mapping = {
        "A": "up",
        "B": "down",
        "C": "right",
        "D": "left",
        "H": "home",
        "F": "end",
    }
    return mapping.get(tail, "esc")


def render_document(
    record: dict[str, Any],
    mode: RenderMode,
    index: int | None = None,
    total: int | None = None,
) -> None:
    terminal = shutil.get_terminal_size((100, 30))
    if mode.name == "sixel" and record.get("thumbnail"):
        render_sixel_screen(record, terminal, index, total)
    else:
        render_ansi_screen(record, terminal, index, total)


def render_sixel_screen(
    record: dict[str, Any],
    terminal: os.terminal_size,
    index: int | None,
    total: int | None,
) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    image = record_image(record)
    if image:
        target_px = max(96, min(1200, int(terminal.columns * 0.75 * 8)))
        image = resize_for_width(image, target_px)
        sys.stdout.write(image_to_sixel(image))
    else:
        sys.stdout.write("(no thumbnail)")

    meta_col = max(2, int(terminal.columns * 0.75) + 2)
    for row, line in enumerate(metadata_lines(record, index, total, terminal.columns - meta_col), start=1):
        sys.stdout.write(f"\x1b[{row};{meta_col}H{line}")
    footer_row = terminal.lines
    sys.stdout.write(f"\x1b[{footer_row};1H{footer(index, total, mode='sixel')}")
    sys.stdout.flush()


def render_ansi_screen(
    record: dict[str, Any],
    terminal: os.terminal_size,
    index: int | None,
    total: int | None,
) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    image_width = max(16, int(terminal.columns * 0.75))
    meta_width = max(20, terminal.columns - image_width - 3)
    image = record_image(record)
    if image:
        image_lines = image_to_ansi_lines(image, image_width, max(4, terminal.lines - 2))
    else:
        image_lines = ["(no thumbnail)"]
    meta = metadata_lines(record, index, total, meta_width)
    rows = max(len(image_lines), len(meta), 1)
    for row in range(rows):
        left = image_lines[row] if row < len(image_lines) else " " * image_width
        right = meta[row] if row < len(meta) else ""
        sys.stdout.write(left + "\x1b[0m  " + right + "\n")
    sys.stdout.write("\x1b[0m" + footer(index, total, mode="ansi") + "\n")
    sys.stdout.flush()


def record_image(record: dict[str, Any]) -> Image.Image | None:
    thumbnail = record.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return None
    data = thumbnail.get("data")
    if not data:
        return None
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")


def metadata_lines(
    record: dict[str, Any],
    index: int | None,
    total: int | None,
    width: int,
) -> list[str]:
    width = max(12, width)
    lines: list[str] = []
    if index is not None and total is not None:
        lines.append(f"{index + 1}/{total}")
        lines.append("")
    add_field(lines, "Title", record.get("title"), width)
    add_field(lines, "Kind", record.get("kind"), width)
    add_field(lines, "Path", record.get("path"), width)
    if record.get("user_name"):
        add_field(lines, "User", record.get("user_name"), width)
    source = record.get("source", {})
    add_field(lines, "Modified", source.get("modified"), width)
    add_field(lines, "Size", human_size(source.get("size_bytes")), width)

    metadata = record.get("metadata", {})
    preferred = [
        "author",
        "Author",
        "creationDate",
        "modDate",
        "page_count",
        "line_count",
        "word_count",
        "subject",
        "keywords",
        "creator",
        "producer",
        "error",
    ]
    used: set[str] = set()
    for key in preferred:
        if key in metadata:
            add_field(lines, title_key(key), metadata[key], width)
            used.add(key)
    for key, value in metadata.items():
        if key in used or key.lower() == "title":
            continue
        add_field(lines, title_key(key), value, width)
    return lines


def add_field(lines: list[str], label: str, value: Any, width: int) -> None:
    if value is None or value == "":
        return
    prefix = f"{label}: "
    wrapped = textwrap.wrap(str(value), width=max(8, width - len(prefix))) or [""]
    lines.append(prefix + wrapped[0])
    for continuation in wrapped[1:]:
        lines.append(" " * len(prefix) + continuation)


def title_key(key: str) -> str:
    key = re.sub(r"([a-z])([A-Z])", r"\1 \2", key)
    return key.replace("_", " ").strip().title()


def human_size(value: Any) -> str | None:
    if not isinstance(value, int):
        return None
    size = float(value)
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return None


def footer(index: int | None, total: int | None, mode: str) -> str:
    position = f" {index + 1}/{total}" if index is not None and total is not None else ""
    return f"[{mode}{position}] arrows/j/k navigate, Home/End jump, q quits"


def resize_for_width(image: Image.Image, width: int) -> Image.Image:
    if image.width == width:
        return image
    height = max(1, int(image.height * (width / image.width)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def image_to_ansi_lines(image: Image.Image, width_chars: int, max_rows: int) -> list[str]:
    image = resize_for_width(image, width_chars)
    max_pixel_height = max(2, max_rows * 2)
    if image.height > max_pixel_height:
        image.thumbnail((width_chars, max_pixel_height), Image.Resampling.LANCZOS)
    if image.height % 2:
        padded = Image.new("RGB", (image.width, image.height + 1), "white")
        padded.paste(image, (0, 0))
        image = padded

    pixels = image.load()
    lines: list[str] = []
    for y in range(0, image.height, 2):
        line_parts: list[str] = []
        for x in range(image.width):
            top = pixels[x, y]
            bottom = pixels[x, y + 1]
            line_parts.append(
                f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀"
            )
        lines.append("".join(line_parts))
    return lines


def image_to_sixel(image: Image.Image) -> str:
    palette_image = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=128)
    palette = palette_image.getpalette()[: 128 * 3]
    pixels = palette_image.load()
    width, height = palette_image.size

    chunks = ['\x1bPq"1;1']
    for color in range(128):
        red, green, blue = palette[color * 3 : color * 3 + 3]
        chunks.append(f"#{color};2;{red * 100 // 255};{green * 100 // 255};{blue * 100 // 255}")

    for y in range(0, height, 6):
        if y:
            chunks.append("-")
        for color in range(128):
            runs: list[str] = []
            active = False
            current_char = 63
            count = 0
            for x in range(width):
                bits = 0
                for bit in range(6):
                    yy = y + bit
                    if yy < height and pixels[x, yy] == color:
                        bits |= 1 << bit
                char_code = 63 + bits
                if bits:
                    active = True
                if count and char_code == current_char:
                    count += 1
                else:
                    if count:
                        runs.append(repeat_sixel_char(current_char, count))
                    current_char = char_code
                    count = 1
            if count:
                runs.append(repeat_sixel_char(current_char, count))
            if active:
                chunks.append(f"#{color}" + "".join(runs) + "$")
    chunks.append("\x1b\\")
    return "".join(chunks)


def repeat_sixel_char(char_code: int, count: int) -> str:
    char = chr(char_code)
    if count >= 4:
        return f"!{count}{char}"
    return char * count
