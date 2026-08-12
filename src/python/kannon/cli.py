from __future__ import annotations

import argparse
import base64
import getpass
import importlib.util
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
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree


def abort_missing_startup_dependency(
    import_name: str,
    package_name: str,
    purpose: str,
    commands: list[str],
) -> None:
    print(
        "\n".join(
            [
                f"Kannon cannot start because Python import '{import_name}' is missing.",
                f"Missing dependency: {package_name}.",
                f"Why it matters: Kannon needs {package_name} for {purpose}.",
                "Consequence: no documents can be scanned or displayed until this is installed.",
                "Try one of these commands from the repository root:",
                *[f"  {command}" for command in commands],
            ]
        ),
        file=sys.stderr,
    )
    raise SystemExit(2)


try:
    import yaml
except ModuleNotFoundError as exc:
    if exc.name != "yaml":
        raise
    abort_missing_startup_dependency(
        import_name="yaml",
        package_name="PyYAML",
        purpose="reading and writing kannon.yaml cache files",
        commands=[
            "python -m pip install -e .",
            "python -m pip install -r requirements.txt",
            "python -m pip install PyYAML",
        ],
    )

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:
    if exc.name != "PIL":
        raise
    abort_missing_startup_dependency(
        import_name="PIL",
        package_name="Pillow",
        purpose="creating thumbnails and rendering ANSI/sixel previews",
        commands=[
            "python -m pip install -e .",
            "python -m pip install -r requirements.txt",
            "python -m pip install Pillow",
        ],
    )


PDF_EXTENSIONS = {".pdf"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
RTF_EXTENSIONS = {".rtf"}
DOCX_EXTENSIONS = {".docx"}
ODT_EXTENSIONS = {".odt"}
SUPPORTED_EXTENSIONS = (
    PDF_EXTENSIONS
    | MARKDOWN_EXTENSIONS
    | RTF_EXTENSIONS
    | DOCX_EXTENSIONS
    | ODT_EXTENSIONS
)
CACHE_VERSION = 1
DOCUMENT_TYPES = "PDF, Markdown, RTF, DOCX, and ODT"


@dataclass(frozen=True)
class RenderMode:
    name: str
    forced: bool = False


class KannonError(Exception):
    def __init__(
        self,
        summary: str,
        consequences: list[str],
        actions: list[str],
        details: str | None = None,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.consequences = consequences
        self.actions = actions
        self.details = details

    def as_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.summary,
            "consequences": self.consequences,
            "actions": self.actions,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.per_page < 1:
        raise SystemExit("-n/--per-page must be 1 or greater")
    paths = [Path(p).expanduser() for p in args.paths] or [Path.cwd()]
    cache_path = Path(args.cache).expanduser()
    if not cache_path.is_absolute():
        cache_path = Path.cwd() / cache_path

    documents = discover_documents(paths)
    if not documents:
        print(f"No supported documents found. Kannon currently scans {DOCUMENT_TYPES}.")
        return 1

    print_dependency_warnings(documents)

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
    use_nerd = not args.no_nerd
    if args.no_tui or not (sys.stdin.isatty() and sys.stdout.isatty()):
        render_page(records, mode, start_index=0, per_page=args.per_page, use_nerd=use_nerd)
        return 0

    run_tui(records, mode, args.per_page, use_nerd)
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
        help="Force Kannon's built-in ANSI block renderer.",
    )
    parser.add_argument(
        "--chafa",
        action="store_true",
        help="Force rendering thumbnails through the chafa command.",
    )
    parser.add_argument(
        "--no-chafa",
        action="store_true",
        help="Do not use chafa automatically, even if it is installed.",
    )
    parser.add_argument(
        "--sixel",
        action="store_true",
        help="Force Kannon's built-in sixel output even if terminal detection is unsure.",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Render the first record once and exit instead of opening the arrow-key TUI.",
    )
    parser.add_argument(
        "-n",
        "--per-page",
        type=int,
        default=1,
        help="Number of document previews to fit on one terminal page. Default: 1.",
    )
    parser.add_argument(
        "--no-nerd",
        action="store_true",
        help="Use plain ASCII labels instead of Nerd Font glyphs in the terminal UI.",
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


def print_dependency_warnings(documents: list[Path]) -> None:
    if not any(document.suffix.lower() in PDF_EXTENSIONS for document in documents):
        return

    pymupdf_available = import_available("fitz")
    pdftoppm_path = shutil.which("pdftoppm")
    pdfinfo_path = shutil.which("pdfinfo")

    if not pymupdf_available and pdftoppm_path:
        print(
            format_dependency_notice(
                summary="PyMuPDF is not installed; Kannon will use Poppler as the PDF fallback.",
                consequences=[
                    "PDF thumbnails can still be generated through pdftoppm.",
                    "PDF metadata may differ from the PyMuPDF path.",
                ],
                actions=[
                    "Install the primary Python PDF renderer: python -m pip install PyMuPDF",
                    "Or install all project dependencies: python -m pip install -e .",
                    "Run Kannon with --refresh after installing to regenerate cached PDF records.",
                ],
                details=f"Python import 'fitz' was not found. Using pdftoppm at {pdftoppm_path}.",
            ),
            file=sys.stderr,
        )
    elif pymupdf_available and not pdftoppm_path:
        print(
            format_dependency_notice(
                summary="Poppler's pdftoppm command is not installed; PyMuPDF will render PDFs.",
                consequences=[
                    "PDF thumbnails should still work through PyMuPDF.",
                    "If PyMuPDF later fails or is removed, Kannon will not have a PDF fallback.",
                ],
                actions=[
                    "Install the fallback tools on Debian/Ubuntu: sudo apt install poppler-utils",
                    "Install the fallback tools on Fedora: sudo dnf install poppler-utils",
                    "Install the fallback tools on macOS with Homebrew: brew install poppler",
                ],
                details="Executable 'pdftoppm' was not found on PATH.",
            ),
            file=sys.stderr,
        )
    elif not pymupdf_available and not pdftoppm_path:
        error = missing_pdf_renderer_error(True)
        print(
            format_dependency_notice(
                summary=error.summary,
                consequences=error.consequences,
                actions=error.actions,
                details=error.details,
            ),
            file=sys.stderr,
        )

    if not pdfinfo_path:
        print(
            format_dependency_notice(
                summary="Poppler's pdfinfo command is not installed; fallback PDF metadata is limited.",
                consequences=[
                    "If Kannon uses the Poppler fallback, title, author, date, and page count may be missing.",
                    "PDF thumbnails can still work when either PyMuPDF or pdftoppm is available.",
                ],
                actions=[
                    "Install Poppler on Debian/Ubuntu: sudo apt install poppler-utils",
                    "Install Poppler on Fedora: sudo dnf install poppler-utils",
                    "Install Poppler on macOS with Homebrew: brew install poppler",
                    "Rerun Kannon with --refresh after installing to update cached metadata.",
                ],
                details="Executable 'pdfinfo' was not found on PATH.",
            ),
            file=sys.stderr,
        )


def import_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def format_dependency_notice(
    summary: str,
    consequences: list[str],
    actions: list[str],
    details: str | None = None,
) -> str:
    parts = [
        f"warning: {summary}",
        "  consequence:",
        *[f"    - {item}" for item in consequences],
        "  actions:",
        *[f"    - {item}" for item in actions],
    ]
    if details:
        parts.extend(["  details:", f"    {details}"])
    return "\n".join(parts)


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
        except KannonError as exc:
            if is_missing_pdf_renderer_error(exc):
                print(
                    f"warning: {document}: PDF thumbnail skipped because PDF renderer "
                    "dependencies are missing; see the dependency warning above.",
                    file=sys.stderr,
                )
            else:
                print(format_kannon_error(document, exc), file=sys.stderr)
            records.append(error_record(document, stat, exc))
        except Exception as exc:
            print(format_unexpected_error(document, exc), file=sys.stderr)
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
    elif suffix in RTF_EXTENSIONS:
        metadata, image = render_rtf(document, thumbnail_size)
        kind = "rtf"
    elif suffix in DOCX_EXTENSIONS:
        metadata, image = render_docx(document, thumbnail_size)
        kind = "docx"
    elif suffix in ODT_EXTENSIONS:
        metadata, image = render_odt(document, thumbnail_size)
        kind = "odt"
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
            "created": source_created_iso(stat),
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
            "created": source_created_iso(stat),
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "owner_user": owner,
        },
        "metadata": error_metadata(exc),
    }
    if owner and owner != getpass.getuser():
        record["user_name"] = owner
    return record


def error_metadata(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, KannonError):
        return exc.as_metadata()
    return {
        "error": f"{type(exc).__name__}: {exc}",
        "consequences": ["This document was indexed without a generated thumbnail."],
        "actions": [
            "Run the command again with --refresh after fixing the input file or environment.",
            "If this is a dependency problem, install dependencies with: python -m pip install -e .",
        ],
    }


def format_kannon_error(document: Path, exc: KannonError) -> str:
    parts = [
        f"warning: {document}: {exc.summary}",
        "  consequence:",
        *[f"    - {item}" for item in exc.consequences],
        "  actions:",
        *[f"    - {item}" for item in exc.actions],
    ]
    if exc.details:
        parts.extend(["  details:", f"    {exc.details}"])
    return "\n".join(parts)


def is_missing_pdf_renderer_error(exc: KannonError) -> bool:
    return "Cannot render PDF thumbnails because neither PyMuPDF" in exc.summary


def format_unexpected_error(document: Path, exc: Exception) -> str:
    return "\n".join(
        [
            f"warning: {document}: {type(exc).__name__}: {exc}",
            "  consequence:",
            "    - This document was indexed without a generated thumbnail.",
            "  actions:",
            "    - Run again with --refresh after fixing the document or environment.",
            "    - If this looks like a dependency issue, run: python -m pip install -e .",
        ]
    )


def render_pdf(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    try:
        return render_pdf_with_pymupdf(document, thumbnail_size)
    except ModuleNotFoundError as exc:
        if exc.name != "fitz":
            raise
        return render_pdf_with_poppler(document, thumbnail_size, pymupdf_missing=True)


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


def render_pdf_with_poppler(
    document: Path,
    thumbnail_size: int,
    pymupdf_missing: bool = False,
) -> tuple[dict[str, Any], Image.Image]:
    if not shutil.which("pdftoppm"):
        raise missing_pdf_renderer_error(pymupdf_missing=pymupdf_missing)
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
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            raise missing_pdf_renderer_error(pymupdf_missing=pymupdf_missing) from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise KannonError(
                summary="Poppler could not render the first page of this PDF.",
                consequences=[
                    "This PDF will be listed without a generated thumbnail.",
                    "The TUI will show '(no thumbnail)' for this document.",
                ],
                actions=[
                    "Check whether the file is encrypted, damaged, or not actually a PDF.",
                    "Try opening it with another PDF reader, then run Kannon again with --refresh.",
                    "If Poppler is outdated, upgrade it with your system package manager.",
                ],
                details=stderr.strip() or f"pdftoppm exited with status {exc.returncode}.",
            ) from exc
        image_path = prefix.with_suffix(".png")
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((thumbnail_size, thumbnail_size), Image.Resampling.LANCZOS)
    metadata.setdefault("renderer", "poppler-utils")
    return metadata, image


def missing_pdf_renderer_error(pymupdf_missing: bool) -> KannonError:
    py_status = "Python import 'fitz' failed." if pymupdf_missing else "PyMuPDF was not tried."
    return KannonError(
        summary=(
            "Cannot render PDF thumbnails because neither PyMuPDF ('fitz') nor "
            "Poppler's 'pdftoppm' command is available."
        ),
        consequences=[
            "PDF files will still be recorded in kannon.yaml, but without thumbnails.",
            "PDF metadata may be incomplete because the primary PDF renderer is missing.",
            "The TUI will show '(no thumbnail)' for affected PDFs.",
            "Markdown documents are unaffected.",
        ],
        actions=[
            "From this repository, install Python dependencies: python -m pip install -e .",
            "Or install only the Python PDF renderer: python -m pip install PyMuPDF",
            "Or install Poppler on Debian/Ubuntu: sudo apt install poppler-utils",
            "Or install Poppler on Fedora: sudo dnf install poppler-utils",
            "Or install Poppler on macOS with Homebrew: brew install poppler",
            "After installing, rerun Kannon with --refresh to regenerate cached thumbnails.",
        ],
        details=f"{py_status} Executable 'pdftoppm' was not found on PATH.",
    )


def pdfinfo_metadata(document: Path) -> dict[str, Any]:
    if not shutil.which("pdfinfo"):
        return {
            "metadata_warning": (
                "Poppler command 'pdfinfo' was not found; thumbnail rendering can still work "
                "through 'pdftoppm', but PDF metadata is limited."
            )
        }
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
        "mermaid_blocks": sum(1 for block in markdown_blocks(lines) if block["kind"] == "mermaid"),
        "renderer": "colored-pillow-markdown",
    }
    image = draw_markdown_page(lines[:80], thumbnail_size, title)
    return metadata, image


def render_rtf(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    raw = document.read_text(encoding="utf-8", errors="replace")
    text = rtf_to_text(raw)
    lines = text.splitlines()
    title = first_nonempty_line(lines) or document.stem
    metadata = {
        "title": title,
        "line_count": len(lines),
        "word_count": len(re.findall(r"\S+", text)),
        "renderer": "lightweight-rtf-text-preview",
    }
    return metadata, draw_text_page(lines[:80], thumbnail_size, title, accent="#9f1239", kind="RTF")


def render_docx(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    try:
        with zipfile.ZipFile(document) as archive:
            text = docx_document_text(archive)
            metadata = docx_core_metadata(archive)
    except zipfile.BadZipFile as exc:
        raise KannonError(
            summary="DOCX preview failed because this file is not a valid DOCX ZIP container.",
            consequences=[
                "This document will be listed without a generated thumbnail.",
                "DOCX metadata and first-page text cannot be extracted from this file.",
            ],
            actions=[
                "Check whether the file is damaged or has the wrong extension.",
                "Open and re-save it in a word processor, then run Kannon again with --refresh.",
            ],
        ) from exc

    lines = text.splitlines()
    title = clean_metadata_value(metadata.get("title")) or first_nonempty_line(lines) or document.stem
    metadata.update(
        {
            "title": title,
            "line_count": len(lines),
            "word_count": len(re.findall(r"\S+", text)),
            "renderer": "stdlib-docx-xml-preview",
        }
    )
    return metadata, draw_text_page(lines[:80], thumbnail_size, str(title), accent="#0f766e", kind="DOCX")


def render_odt(document: Path, thumbnail_size: int) -> tuple[dict[str, Any], Image.Image]:
    try:
        with zipfile.ZipFile(document) as archive:
            text = odt_document_text(archive)
            metadata = odt_metadata(archive)
    except zipfile.BadZipFile as exc:
        raise KannonError(
            summary="ODT preview failed because this file is not a valid OpenDocument ZIP container.",
            consequences=[
                "This ODT will be listed without a generated thumbnail.",
                "Kannon cannot extract LibreOffice/OpenDocument text or metadata from this file.",
            ],
            actions=[
                "Check whether the file is damaged or has the wrong extension.",
                "Open and re-save it in LibreOffice, then run Kannon again with --refresh.",
            ],
        ) from exc

    lines = text.splitlines()
    title = clean_metadata_value(metadata.get("title")) or first_nonempty_line(lines) or document.stem
    metadata.update(
        {
            "title": title,
            "line_count": len(lines),
            "word_count": len(re.findall(r"\S+", text)),
            "renderer": "stdlib-odt-xml-preview",
        }
    )
    return metadata, draw_text_page(lines[:80], thumbnail_size, str(title), accent="#6d28d9", kind="ODT")


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
    font_code = load_font(14, monospace=True)

    margin = max(24, width // 18)
    y = margin
    blocks = markdown_blocks(lines)
    for block in blocks:
        if y > height - margin:
            break
        kind = block["kind"]
        text = str(block.get("text", ""))
        if kind == "blank":
            y += 12
            continue
        if kind == "heading":
            level = int(block.get("level", 3))
            y = draw_markdown_heading(draw, margin, y, width - margin, text, level)
            continue
        if kind == "mermaid":
            y = draw_mermaid_block(draw, margin, y, width - margin, text, font_code)
            continue
        if kind == "code":
            language = str(block.get("language", ""))
            y = draw_code_block(draw, margin, y, width - margin, text.splitlines(), font_code, language)
            continue
        line = text
        fill = "#334155"
        prefix = ""
        if re.match(r"^\s*[-*]\s+", line):
            line = re.sub(r"^\s*[-*]\s+", "", line)
            prefix = "- "
            fill = "#047857"
        wrap_width = max(20, (width - margin * 2) // 9)
        for wrapped in textwrap.wrap(line, width=wrap_width) or [""]:
            if y > height - margin:
                break
            draw.text((margin, y), prefix + wrapped, fill=fill, font=font_regular)
            y += 22
            prefix = "  " if prefix else ""
    return image


def markdown_blocks(lines: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        fence = re.match(r"^```\s*([A-Za-z0-9_-]+)?\s*$", line.strip())
        if fence:
            language = (fence.group(1) or "").lower()
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index].rstrip())
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "kind": "mermaid" if language == "mermaid" else "code",
                    "language": language,
                    "text": "\n".join(body),
                }
            )
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if heading:
            blocks.append({"kind": "heading", "level": len(heading.group(1)), "text": heading.group(2).strip()})
        elif line.strip():
            blocks.append({"kind": "paragraph", "text": line})
        else:
            blocks.append({"kind": "blank", "text": ""})
        index += 1
    return blocks


def draw_markdown_heading(
    draw: ImageDraw.ImageDraw,
    left: int,
    y: int,
    right: int,
    text: str,
    level: int,
) -> int:
    line_count = 3 if level == 1 else 2 if level == 2 else 1
    font_size = 34 if level == 1 else 26 if level == 2 else 20
    font = load_font(font_size)
    colors = {1: ("#0f172a", "#bae6fd"), 2: ("#1d4ed8", "#dbeafe")}
    fill, background = colors.get(level, ("#334155", "#e2e8f0"))
    line_height = 28
    total_height = line_height * line_count
    draw.rounded_rectangle((left - 8, y - 4, right, y + total_height), radius=6, fill=background)
    wrap_width = max(8, (right - left) // max(8, font_size // 2))
    wrapped = textwrap.wrap(text, width=wrap_width)[:line_count] or [text[:wrap_width]]
    while len(wrapped) < line_count:
        wrapped.append("")
    for offset, line in enumerate(wrapped):
        draw.text((left, y + offset * line_height), line, fill=fill, font=font)
    return y + total_height + 12


def draw_code_block(
    draw: ImageDraw.ImageDraw,
    left: int,
    y: int,
    right: int,
    lines: list[str],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    language: str,
) -> int:
    if language:
        draw.text((left, y), language[:24], fill="#2563eb", font=font)
        y += 18
    wrap_width = max(20, (right - left) // 9)
    for line in lines:
        for wrapped in textwrap.wrap(line, width=wrap_width, replace_whitespace=False) or [""]:
            draw.rectangle((left - 8, y - 3, right, y + 18), fill="#eef2ff")
            draw_highlighted_code(draw, (left, y), wrapped, font, language)
            y += 20
    return y + 6


def draw_mermaid_block(
    draw: ImageDraw.ImageDraw,
    left: int,
    y: int,
    right: int,
    source: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> int:
    graph = parse_mermaid_flow(source)
    if not graph:
        draw.text((left, y), "mermaid", fill="#9333ea", font=font)
        return draw_code_block(draw, left, y + 18, right, source.splitlines(), font, "mermaid")

    draw.rounded_rectangle((left - 8, y - 4, right, y + 150), radius=8, fill="#faf5ff", outline="#c084fc", width=2)
    draw.text((left, y + 6), "mermaid diagram", fill="#7e22ce", font=font)
    nodes = list(graph["nodes"])
    edges = graph["edges"]
    box_width = max(76, min(140, (right - left - 20) // max(1, min(3, len(nodes)))))
    box_height = 30
    positions: dict[str, tuple[int, int]] = {}
    for index, node in enumerate(nodes[:6]):
        col = index % 3
        row = index // 3
        x = left + col * (box_width + 18)
        yy = y + 34 + row * 54
        positions[node] = (x, yy)
        label = graph["labels"].get(node, node)
        draw.rounded_rectangle((x, yy, x + box_width, yy + box_height), radius=6, fill="#ede9fe", outline="#8b5cf6")
        draw.text((x + 8, yy + 8), truncate_plain(label, max(6, box_width // 8)), fill="#312e81", font=font)
    for source_node, target_node in edges:
        if source_node not in positions or target_node not in positions:
            continue
        x1, y1 = positions[source_node]
        x2, y2 = positions[target_node]
        start = (x1 + box_width, y1 + box_height // 2)
        end = (x2, y2 + box_height // 2)
        draw.line((start, end), fill="#7c3aed", width=2)
        draw.polygon([(end[0], end[1]), (end[0] - 6, end[1] - 4), (end[0] - 6, end[1] + 4)], fill="#7c3aed")
    return y + 162


def parse_mermaid_flow(source: str) -> dict[str, Any] | None:
    nodes: list[str] = []
    labels: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or re.match(r"^(graph|flowchart)\b", line):
            continue
        match = re.match(r"([A-Za-z0-9_]+)(?:\[(.*?)\]|\((.*?)\))?\s*[-=.]+>\s*([A-Za-z0-9_]+)(?:\[(.*?)\]|\((.*?)\))?", line)
        if not match:
            continue
        src = match.group(1)
        dst = match.group(4)
        src_label = match.group(2) or match.group(3) or src
        dst_label = match.group(5) or match.group(6) or dst
        for node in (src, dst):
            if node not in nodes:
                nodes.append(node)
        labels.setdefault(src, src_label)
        labels.setdefault(dst, dst_label)
        edges.append((src, dst))
    if not nodes or not edges:
        return None
    return {"nodes": nodes, "labels": labels, "edges": edges}


def draw_text_page(
    lines: list[str],
    thumbnail_size: int,
    title: str,
    accent: str,
    kind: str,
) -> Image.Image:
    width = thumbnail_size
    height = int(thumbnail_size * 1.33)
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    font_regular = load_font(16)
    font_bold = load_font(22)
    margin = max(24, width // 18)
    y = margin
    draw.rectangle((0, 0, width, 10), fill=accent)
    draw.text((margin, y), title[:70], fill="#111827", font=font_bold)
    y += 34
    draw.text((margin, y), kind, fill=accent, font=font_regular)
    y += 28
    draw.line((margin, y, width - margin, y), fill="#cbd5e1", width=1)
    y += 18
    wrap_width = max(20, (width - margin * 2) // 9)
    for line in lines:
        if y > height - margin:
            break
        if not line.strip():
            y += 12
            continue
        for wrapped in textwrap.wrap(line.strip(), width=wrap_width) or [""]:
            if y > height - margin:
                break
            draw.text((margin, y), wrapped, fill="#334155", font=font_regular)
            y += 22
    return image


def draw_highlighted_code(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    language: str,
) -> None:
    x, y = xy
    for token, color in highlight_tokens(text, language):
        draw.text((x, y), token, fill=color, font=font)
        x += max(1, int(draw.textlength(token, font=font)))


def highlight_tokens(text: str, language: str) -> list[tuple[str, str]]:
    keyword_color = "#7c3aed"
    string_color = "#b45309"
    comment_color = "#64748b"
    number_color = "#0369a1"
    plain_color = "#111827"
    keywords = {
        "and",
        "as",
        "class",
        "const",
        "def",
        "else",
        "except",
        "false",
        "for",
        "from",
        "function",
        "if",
        "import",
        "in",
        "let",
        "none",
        "return",
        "true",
        "try",
        "while",
    }
    comment_match = re.search(r"(#|//).*$", text) if language != "sh" else re.search(r"#.*$", text)
    comment_start = comment_match.start() if comment_match else len(text)
    pieces: list[tuple[str, str]] = []
    pattern = re.compile(r"('.*?'|\".*?\"|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b|\s+|.)")
    for match in pattern.finditer(text[:comment_start]):
        token = match.group(0)
        lower = token.lower()
        if token.startswith(("'", '"')):
            color = string_color
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            color = number_color
        elif lower in keywords:
            color = keyword_color
        else:
            color = plain_color
        pieces.append((token, color))
    if comment_match:
        pieces.append((text[comment_start:], comment_color))
    return pieces


def rtf_to_text(raw: str) -> str:
    text = raw.replace("\\par", "\n").replace("\\line", "\n")
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\\", "")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def docx_document_text(archive: zipfile.ZipFile) -> str:
    try:
        payload = archive.read("word/document.xml")
    except KeyError as exc:
        raise KannonError(
            summary="DOCX preview failed because word/document.xml is missing.",
            consequences=[
                "This DOCX will be listed without a generated thumbnail.",
                "Kannon cannot extract the document body from this file.",
            ],
            actions=[
                "Check whether the file is damaged or actually a DOCX document.",
                "Open and re-save it in a word processor, then run Kannon again with --refresh.",
            ],
        ) from exc
    root = ElementTree.fromstring(payload)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def docx_core_metadata(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        payload = archive.read("docProps/core.xml")
    except KeyError:
        return {}
    root = ElementTree.fromstring(payload)
    metadata: dict[str, Any] = {}
    names = {
        "title": ".//{http://purl.org/dc/elements/1.1/}title",
        "subject": ".//{http://purl.org/dc/elements/1.1/}subject",
        "creator": ".//{http://purl.org/dc/elements/1.1/}creator",
        "keywords": ".//{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}keywords",
        "description": ".//{http://purl.org/dc/elements/1.1/}description",
        "created": ".//{http://purl.org/dc/terms/}created",
        "modified": ".//{http://purl.org/dc/terms/}modified",
    }
    for key, path in names.items():
        node = root.find(path)
        if node is not None and node.text and node.text.strip():
            metadata[key] = node.text.strip()
    return metadata


def odt_document_text(archive: zipfile.ZipFile) -> str:
    try:
        payload = archive.read("content.xml")
    except KeyError as exc:
        raise KannonError(
            summary="ODT preview failed because content.xml is missing.",
            consequences=[
                "This ODT will be listed without a generated thumbnail.",
                "Kannon cannot extract the LibreOffice/OpenDocument body text from this file.",
            ],
            actions=[
                "Check whether the file is damaged or actually an ODT document.",
                "Open and re-save it in LibreOffice, then run Kannon again with --refresh.",
            ],
        ) from exc
    root = ElementTree.fromstring(payload)
    paragraphs: list[str] = []
    text_namespace = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    for node in root.iter():
        if node.tag in {f"{text_namespace}h", f"{text_namespace}p"}:
            text = "".join(node.itertext()).strip()
            if text:
                paragraphs.append(text)
    return "\n".join(paragraphs)


def odt_metadata(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        payload = archive.read("meta.xml")
    except KeyError:
        return {}
    root = ElementTree.fromstring(payload)
    names = {
        "title": ".//{http://purl.org/dc/elements/1.1/}title",
        "subject": ".//{http://purl.org/dc/elements/1.1/}subject",
        "creator": ".//{http://purl.org/dc/elements/1.1/}creator",
        "description": ".//{http://purl.org/dc/elements/1.1/}description",
        "created": ".//{http://purl.org/dc/terms/}created",
        "modified": ".//{http://purl.org/dc/terms/}modified",
        "keyword": ".//{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}keyword",
        "editing_cycles": ".//{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}editing-cycles",
        "generator": ".//{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}generator",
    }
    metadata: dict[str, Any] = {}
    for key, path in names.items():
        node = root.find(path)
        if node is not None and node.text and node.text.strip():
            metadata[key] = node.text.strip()
    return metadata


def first_nonempty_line(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return None


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


def source_created_iso(stat: os.stat_result) -> str:
    birth_time = getattr(stat, "st_birthtime", None)
    if birth_time is not None:
        return datetime.fromtimestamp(birth_time, timezone.utc).isoformat()
    return datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat() + " (ctime)"


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def choose_render_mode(args: argparse.Namespace) -> RenderMode:
    forced = [flag for flag, enabled in {"--ansi": args.ansi, "--sixel": args.sixel, "--chafa": args.chafa}.items() if enabled]
    if len(forced) > 1:
        raise SystemExit("--ansi, --sixel, and --chafa are mutually exclusive")
    if args.chafa and args.no_chafa:
        raise SystemExit("--chafa and --no-chafa are mutually exclusive")
    if args.chafa:
        if not shutil.which("chafa"):
            raise SystemExit(chafa_missing_message())
        return RenderMode("chafa", forced=True)
    if args.ansi:
        return RenderMode("ansi", forced=True)
    if args.sixel:
        return RenderMode("sixel", forced=True)
    if not args.no_chafa and shutil.which("chafa"):
        return RenderMode("chafa")
    if terminal_supports_sixel():
        return RenderMode("sixel")
    return RenderMode("ansi")


def chafa_missing_message() -> str:
    return "\n".join(
        [
            "Kannon cannot use --chafa because the 'chafa' command is not on PATH.",
            "Consequence: Chafa's richer terminal graphics protocols are unavailable.",
            "Use --ansi for the built-in ANSI fallback, or install Chafa:",
            "  Debian/Ubuntu: sudo apt install chafa",
            "  Fedora: sudo dnf install chafa",
            "  Arch: sudo pacman -S chafa",
            "  macOS/Homebrew: brew install chafa",
        ]
    )


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


def run_tui(records: list[dict[str, Any]], mode: RenderMode, per_page: int, use_nerd: bool) -> None:
    index = 0
    with RawTerminal():
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        try:
            while True:
                render_page(records, mode, start_index=index, per_page=per_page, use_nerd=use_nerd)
                key = read_key()
                if key in {"q", "Q", "\x03", "\x04"}:
                    break
                if key in {"right", "down", "j", "n", " "}:
                    index = min(index + per_page, max(0, len(records) - 1))
                elif key in {"left", "up", "k", "p"}:
                    index = max(index - per_page, 0)
                elif key == "home":
                    index = 0
                elif key == "end":
                    index = max(0, len(records) - per_page)
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


def render_page(
    records: list[dict[str, Any]],
    mode: RenderMode,
    start_index: int,
    per_page: int,
    use_nerd: bool,
) -> None:
    terminal = shutil.get_terminal_size((100, 30))
    visible = records[start_index : start_index + per_page]
    if mode.name == "chafa":
        render_chafa_screen(records, visible, terminal, start_index, per_page, use_nerd)
    elif mode.name == "sixel":
        render_sixel_screen(records, visible, terminal, start_index, per_page, use_nerd)
    else:
        render_ansi_screen(records, visible, terminal, start_index, per_page, use_nerd)


def render_chafa_screen(
    records: list[dict[str, Any]],
    visible: list[dict[str, Any]],
    terminal: os.terminal_size,
    start_index: int,
    per_page: int,
    use_nerd: bool,
) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    slot_height = preview_slot_height(terminal.lines, per_page)
    image_cols = max(16, int(terminal.columns * 0.75))
    meta_col = min(terminal.columns, image_cols + 2)
    meta_width = max(16, terminal.columns - meta_col + 1)
    image_rows = max(1, slot_height - 1)
    for offset, record in enumerate(visible):
        top = offset * slot_height + 1
        draw_terminal_header(record, terminal.columns, top, use_nerd)
        body_top = top + 1
        image = record_image(record)
        if image:
            output = image_to_chafa(image, image_cols, image_rows)
            for row, line in enumerate(output.splitlines(), start=body_top):
                if row >= top + slot_height:
                    break
                sys.stdout.write(f"\x1b[{row};1H{line}")
        else:
            sys.stdout.write(f"\x1b[{body_top};1H(no thumbnail)")
        for row, line in enumerate(
            metadata_lines(record, start_index + offset, len(records), meta_width, use_nerd),
            start=body_top,
        ):
            if row >= top + slot_height:
                break
            sys.stdout.write(f"\x1b[{row};{meta_col}H{truncate_plain(line, meta_width)}")
    footer_row = terminal.lines
    sys.stdout.write(
        f"\x1b[{footer_row};1H"
        f"{status_bar(start_index, len(records), per_page, 'chafa', terminal.columns, use_nerd)}"
    )
    sys.stdout.flush()


def render_sixel_screen(
    records: list[dict[str, Any]],
    visible: list[dict[str, Any]],
    terminal: os.terminal_size,
    start_index: int,
    per_page: int,
    use_nerd: bool,
) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    slot_height = preview_slot_height(terminal.lines, per_page)
    image_cols = max(16, int(terminal.columns * 0.75))
    meta_col = min(terminal.columns, image_cols + 2)
    meta_width = max(16, terminal.columns - meta_col + 1)
    for offset, record in enumerate(visible):
        top = offset * slot_height + 1
        draw_terminal_header(record, terminal.columns, top, use_nerd)
        body_top = top + 1
        image = record_image(record)
        if image:
            max_px_height = max(24, (slot_height - 1) * 16)
            target_px_width = max(96, min(1200, image_cols * 8))
            image = fit_image(image, target_px_width, max_px_height)
            sys.stdout.write(f"\x1b[{body_top};1H{image_to_sixel(image)}")
        else:
            sys.stdout.write(f"\x1b[{body_top};1H(no thumbnail)")
        for row, line in enumerate(
            metadata_lines(record, start_index + offset, len(records), meta_width, use_nerd),
            start=body_top,
        ):
            if row >= top + slot_height:
                break
            sys.stdout.write(f"\x1b[{row};{meta_col}H{truncate_plain(line, meta_width)}")
    footer_row = terminal.lines
    sys.stdout.write(
        f"\x1b[{footer_row};1H"
        f"{status_bar(start_index, len(records), per_page, 'sixel', terminal.columns, use_nerd)}"
    )
    sys.stdout.flush()


def render_ansi_screen(
    records: list[dict[str, Any]],
    visible: list[dict[str, Any]],
    terminal: os.terminal_size,
    start_index: int,
    per_page: int,
    use_nerd: bool,
) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    slot_height = preview_slot_height(terminal.lines, per_page)
    image_width = max(16, int(terminal.columns * 0.75))
    meta_width = max(20, terminal.columns - image_width - 3)
    for offset, record in enumerate(visible):
        draw_ansi_header(record, terminal.columns, use_nerd)
        image = record_image(record)
        max_image_rows = max(1, slot_height - 1)
        if image:
            image_lines = image_to_ansi_lines(image, image_width, max_image_rows)
        else:
            image_lines = ["(no thumbnail)"]
        meta = metadata_lines(record, start_index + offset, len(records), meta_width, use_nerd)
        rows = min(max_image_rows, max(len(image_lines), len(meta), 1))
        for row in range(rows):
            left = image_lines[row] if row < len(image_lines) else ""
            padding = " " * image_width if not left else ""
            right = meta[row] if row < len(meta) else ""
            sys.stdout.write(left + "\x1b[0m" + padding + "  " + truncate_plain(right, meta_width) + "\n")
    sys.stdout.write(
        "\x1b[0m" + status_bar(start_index, len(records), per_page, "ansi", terminal.columns, use_nerd) + "\n"
    )
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
    use_nerd: bool,
) -> list[str]:
    width = max(12, width)
    lines: list[str] = []
    if index is not None and total is not None:
        lines.append(f"{index + 1}/{total}")
        lines.append("")
    add_field(lines, ui_label("Title", "󰎏", use_nerd), record.get("title"), width)
    add_field(lines, ui_label("Kind", "󰈙", use_nerd), record.get("kind"), width)
    add_field(lines, ui_label("Path", "󰉋", use_nerd), record.get("path"), width)
    if record.get("user_name"):
        add_field(lines, ui_label("User", "󰀄", use_nerd), record.get("user_name"), width)
    source = record.get("source", {})
    add_field(lines, ui_label("Created", "󰃭", use_nerd), source.get("created"), width)
    add_field(lines, ui_label("Modified", "󰚰", use_nerd), source.get("modified"), width)
    add_field(lines, ui_label("Size", "󰋊", use_nerd), human_size(source.get("size_bytes")), width)

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


def preview_slot_height(terminal_lines: int, per_page: int) -> int:
    usable = max(1, terminal_lines - 1)
    return max(2, usable // max(1, per_page))


def draw_terminal_header(record: dict[str, Any], width: int, row: int, use_nerd: bool) -> None:
    sys.stdout.write(f"\x1b[{row};1H{header_line(record, width, use_nerd)}")


def draw_ansi_header(record: dict[str, Any], width: int, use_nerd: bool) -> None:
    sys.stdout.write(header_line(record, width, use_nerd) + "\n")


def header_line(record: dict[str, Any], width: int, use_nerd: bool) -> str:
    source = record.get("source", {})
    file_icon = "󰈙 " if use_nerd else ""
    created_icon = "󰃭 " if use_nerd else ""
    modified_icon = "󰚰 " if use_nerd else ""
    fields = [
        file_icon + (Path(str(record.get("path") or record.get("path_abs") or "")).name or str(record.get("title") or "")),
        created_icon + short_date(source.get("created")),
        modified_icon + short_date(source.get("modified")),
    ]
    text = "--[ " + " ][ ".join(fields) + " ]"
    text = truncate_plain(text, width)
    padded = text + " " * max(0, width - len(text))
    return f"\x1b[48;2;22;78;99m\x1b[38;2;236;253;245m{padded}\x1b[0m"


def status_bar(start_index: int, total: int, per_page: int, mode: str, width: int, use_nerd: bool) -> str:
    end_index = min(total, start_index + per_page)
    nav_icon = "󰁔 " if use_nerd else ""
    text = (
        f"{nav_icon}[{mode}] showing {start_index + 1}-{end_index}/{total} "
        f"per-page={per_page} arrows/j/k page, Home/End jump, q quits"
    )
    text = truncate_plain(text, width)
    return f"\x1b[7m{text}{' ' * max(0, width - len(text))}\x1b[0m"


def short_date(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value)
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return text[:10] if text else "unknown"


def truncate_plain(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + ">"


def ui_label(label: str, icon: str, use_nerd: bool) -> str:
    return f"{icon} {label}" if use_nerd else label


def resize_for_width(image: Image.Image, width: int) -> Image.Image:
    if image.width == width:
        return image
    height = max(1, int(image.height * (width / image.width)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def fit_image(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    image = image.copy()
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return image


def image_to_chafa(image: Image.Image, width_chars: int, max_rows: int) -> str:
    if not shutil.which("chafa"):
        return "\n".join(image_to_ansi_lines(image, width_chars, max_rows))
    with tempfile.NamedTemporaryFile(suffix=".png") as handle:
        image.save(handle.name, format="PNG")
        command = [
            "chafa",
            "--format",
            "symbols",
            "--colors",
            "full",
            "--dither",
            "none",
            "--animate",
            "off",
            "--relative",
            "off",
            "--polite",
            "on",
            "--size",
            f"{width_chars}x{max_rows}",
            handle.name,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return "\n".join(image_to_ansi_lines(image, width_chars, max_rows))
    return result.stdout.rstrip("\n")


def image_to_ansi_lines(image: Image.Image, width_chars: int, max_rows: int) -> list[str]:
    max_pixel_height = max(2, max_rows * 2)
    image = fit_image(image, width_chars, max_pixel_height)
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
