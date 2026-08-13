from __future__ import annotations

import os
import time
from pathlib import Path

import yaml

from kannon.cache import load_cache, save_cache
from kannon.cli import compact_record_text, mark_missing_source
from kannon.deps import ImportStatus, doctor_checks, run_doctor_repairs
from kannon.documents import discover_documents, make_error_record, short_date, sort_records
from kannon.watch import DocumentWatcher


def test_sort_missing_values_stay_last_both_directions() -> None:
    records = [
        {"title": "missing.md", "path": "missing.md", "source": {}},
        {"title": "new.md", "path": "new.md", "source": {"modified": "2026-08-13T10:00:00"}},
        {"title": "old.md", "path": "old.md", "source": {"modified": "2026-08-12T10:00:00"}},
    ]
    assert [record["title"] for record in sort_records(records, "modified", descending=True)] == [
        "new.md",
        "old.md",
        "missing.md",
    ]
    assert [record["title"] for record in sort_records(records, "modified", descending=False)] == [
        "old.md",
        "new.md",
        "missing.md",
    ]


def test_discover_documents_skips_unsupported_and_cache_file(tmp_path: Path) -> None:
    markdown = tmp_path / "note.md"
    unsupported = tmp_path / "notes.txt"
    cache = tmp_path / "kannon.yaml"
    nested = tmp_path / "nested"
    nested.mkdir()
    odt = nested / "document.odt"
    markdown.write_text("# Note\n", encoding="utf-8")
    unsupported.write_text("plain text\n", encoding="utf-8")
    cache.write_text("version: 1\n", encoding="utf-8")
    odt.write_bytes(b"not a real odt")

    assert set(discover_documents([tmp_path])) == {markdown.resolve(), odt.resolve()}


def test_make_error_record_preserves_source_identity(tmp_path: Path) -> None:
    document = tmp_path / "broken.pdf"
    document.write_bytes(b"%PDF-broken")
    record = make_error_record(document, document.stat(), {"error": "cannot render"})

    assert record["kind"] == "pdf"
    assert record["title"] == "broken"
    assert record["metadata"]["error"] == "cannot render"
    assert record["source"]["size_bytes"] == len(b"%PDF-broken")


def test_short_date_handles_missing_iso_and_plain_values() -> None:
    assert short_date(None) == "unknown"
    assert short_date("2026-08-13T12:00:00") == "2026-08-13"
    assert short_date("not-a-date-but-long") == "not-a-date"


def test_poll_watcher_detects_file_changes(tmp_path: Path) -> None:
    document = tmp_path / "watch.md"
    document.write_text("# Before\n", encoding="utf-8")
    stat = document.stat()
    record = {
        "path_abs": str(document),
        "source": {"mtime_ns": stat.st_mtime_ns, "size_bytes": stat.st_size},
    }
    watcher = DocumentWatcher([record], "poll", 0.1)
    try:
        assert watcher.changed_indices([record]) == []
        document.write_text("# After\n\nMore text.\n", encoding="utf-8")
        new_time = time.time() + 2
        os.utime(document, (new_time, new_time))
        watcher.last_poll = 0.0
        assert watcher.changed_indices([record]) == [0]
        assert watcher.changed_indices([record]) == []
    finally:
        watcher.close()


def test_poll_watcher_treats_deleted_file_as_changed(tmp_path: Path) -> None:
    document = tmp_path / "gone.md"
    document.write_text("# Gone\n", encoding="utf-8")
    stat = document.stat()
    record = {
        "path_abs": str(document),
        "source": {"mtime_ns": stat.st_mtime_ns, "size_bytes": stat.st_size},
    }
    watcher = DocumentWatcher([record], "poll", 0.1)
    try:
        assert watcher.changed_indices([record]) == []
        document.unlink()
        watcher.last_poll = 0.0
        assert watcher.changed_indices([record]) == [0]
    finally:
        watcher.close()


def test_mark_missing_source_preserves_stale_preview_with_warning(tmp_path: Path) -> None:
    document = tmp_path / "deleted.md"
    record = {
        "path_abs": str(document),
        "metadata": {},
        "source": {},
        "text_preview": "# Deleted",
    }

    assert mark_missing_source(record) is True
    assert mark_missing_source(record) is False
    assert record["metadata"]["source_missing"] is True
    assert "stale" in record["metadata"]["warning"]
    assert record["source"]["missing"] is True


def test_cache_rejects_invalid_documents_list(tmp_path: Path) -> None:
    cache_path = tmp_path / "kannon.yaml"
    cache_path.write_text("version: 1\ndocuments: nope\n", encoding="utf-8")
    assert load_cache(cache_path, yaml) == {}


def test_cache_save_is_readable(tmp_path: Path) -> None:
    cache_path = tmp_path / "kannon.yaml"
    save_cache(cache_path, [{"title": "example"}], 512, yaml)
    assert load_cache(cache_path, yaml)["documents"] == [{"title": "example"}]
    assert not list(tmp_path.glob(".kannon.yaml.*.tmp"))


def test_doctor_deduplicates_python_repair_command(capsys) -> None:
    checks = doctor_checks(
        ImportStatus("yaml", "PyYAML", "cache", ModuleNotFoundError("yaml")),
        ImportStatus("PIL", "Pillow", "preview", ModuleNotFoundError("PIL")),
        fix_system=False,
    )
    missing = [check for check in checks if not check.ok and check.fix is not None]
    assert run_doctor_repairs(missing, prompt=False) is False
    output = capsys.readouterr().out
    assert output.count("Install Kannon Python dependencies into .venv") == 1


def test_text_mode_compacts_metadata_and_preview() -> None:
    record = {
        "title": "Example",
        "kind": "markdown",
        "path": "example.md",
        "source": {"modified": "2026-08-13T12:00:00", "size_bytes": 42},
        "text_preview": "# Example\nSome longer body text that should wrap.",
    }
    lines = compact_record_text(record, max_rows=8, width=24, use_nerd=False)
    assert any("Title: Example" in line for line in lines)
    assert any("# Example" in line for line in lines)
