from __future__ import annotations

import getpass
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


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
DOCUMENT_TYPES = "PDF, Markdown, RTF, DOCX, and ODT"
SORT_KEYS = ("modified", "created", "title", "path", "kind", "size", "author")


def discover_documents(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if not safe_exists(path):
            print(f"warning: {path} does not exist", file=sys.stderr)
            continue
        candidates: Iterable[Path] = path.rglob("*") if safe_is_dir(path) else [path]
        try:
            iterator = iter(candidates)
            for candidate in iterator:
                if not safe_is_file(candidate):
                    continue
                if candidate.name == "kannon.yaml":
                    continue
                if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    resolved = candidate.resolve()
                except OSError as exc:
                    print(f"warning: cannot resolve {candidate}: {exc}", file=sys.stderr)
                    continue
                if resolved not in seen:
                    found.append(resolved)
                    seen.add(resolved)
        except OSError as exc:
            print(f"warning: cannot scan {path}: {exc}", file=sys.stderr)
    return sorted(found, key=lambda p: str(p).lower())


def safe_is_file(candidate: Path) -> bool:
    try:
        return candidate.is_file()
    except OSError as exc:
        print(f"warning: cannot inspect {candidate}: {exc}", file=sys.stderr)
        return False


def safe_is_dir(candidate: Path) -> bool:
    try:
        return candidate.is_dir()
    except OSError as exc:
        print(f"warning: cannot inspect {candidate}: {exc}", file=sys.stderr)
        return False


def safe_exists(candidate: Path) -> bool:
    try:
        return candidate.exists()
    except OSError as exc:
        print(f"warning: cannot inspect {candidate}: {exc}", file=sys.stderr)
        return False


def sort_records(records: list[dict[str, Any]], key_name: str, descending: bool) -> list[dict[str, Any]]:
    if key_name not in SORT_KEYS:
        raise SystemExit(f"unsupported sort key: {key_name}")
    present = [record for record in records if sort_value(record, key_name)[0] == 1]
    missing = [record for record in records if sort_value(record, key_name)[0] == 0]
    present.sort(key=lambda record: sort_value(record, key_name)[1], reverse=descending)
    missing.sort(key=lambda record: str(record.get("path") or record.get("path_abs") or "").casefold())
    return present + missing


def sort_value(record: dict[str, Any], key_name: str) -> tuple[int, str | int]:
    source = record.get("source", {})
    metadata = record.get("metadata", {})
    if not isinstance(source, dict):
        source = {}
    if not isinstance(metadata, dict):
        metadata = {}
    if key_name == "modified":
        return sortable_datetime(source.get("modified"))
    if key_name == "created":
        return sortable_datetime(source.get("created"))
    if key_name == "title":
        return sortable_text(record.get("title"))
    if key_name == "path":
        return sortable_text(record.get("path") or record.get("path_abs"))
    if key_name == "kind":
        return sortable_text(record.get("kind"))
    if key_name == "size":
        return sortable_number(source.get("size_bytes"))
    if key_name == "author":
        return sortable_text(metadata.get("author") or metadata.get("Author") or metadata.get("creator"))
    return sortable_text(record.get("path"))


def sortable_datetime(value: Any) -> tuple[int, str]:
    if value is None:
        return (0, "")
    text = str(value)
    if text.endswith(" (ctime)"):
        text = text.removesuffix(" (ctime)")
    return (1, text)


def sortable_text(value: Any) -> tuple[int, str]:
    if value is None:
        return (0, "")
    text = str(value).casefold()
    return (1, text)


def sortable_number(value: Any) -> tuple[int, int]:
    try:
        return (1, int(value))
    except (TypeError, ValueError):
        return (0, 0)


def record_is_current(record: dict[str, Any], stat: os.stat_result, thumbnail_size: int) -> bool:
    source = record.get("source", {})
    thumbnail = record.get("thumbnail", {})
    if not isinstance(source, dict) or not isinstance(thumbnail, dict):
        return False
    return (
        source.get("mtime_ns") == stat.st_mtime_ns
        and source.get("size_bytes") == stat.st_size
        and thumbnail.get("max_edge") == thumbnail_size
    )


def make_record(
    document: Path,
    stat: os.stat_result,
    kind: str,
    title: str,
    metadata: dict[str, Any],
    text_preview: str,
    thumbnail: dict[str, Any],
) -> dict[str, Any]:
    owner = owner_name(stat.st_uid)
    current_user = getpass.getuser()
    record: dict[str, Any] = {
        "path": display_path(document),
        "path_abs": str(document),
        "kind": kind,
        "title": title,
        "source": source_metadata(stat, owner),
        "metadata": metadata,
        "text_preview": text_preview[:6000],
        "thumbnail": thumbnail,
    }
    if owner and owner != current_user:
        record["user_name"] = owner
    return record


def make_error_record(document: Path, stat: os.stat_result, metadata: dict[str, Any]) -> dict[str, Any]:
    owner = owner_name(stat.st_uid)
    record: dict[str, Any] = {
        "path": display_path(document),
        "path_abs": str(document),
        "kind": document.suffix.lower().lstrip(".") or "unknown",
        "title": document.stem,
        "source": source_metadata(stat, owner),
        "metadata": metadata,
        "text_preview": "",
    }
    if owner and owner != getpass.getuser():
        record["user_name"] = owner
    return record


def source_metadata(stat: os.stat_result, owner: str | None) -> dict[str, Any]:
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "created": source_created_iso(stat),
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "owner_user": owner,
    }


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


def short_date(value: Any) -> str:
    if value is None:
        return "unknown"
    text = str(value)
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    return text[:10] if text else "unknown"
