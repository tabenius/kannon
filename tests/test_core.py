from __future__ import annotations

from pathlib import Path

import yaml

from kannon.cache import load_cache, save_cache
from kannon.cli import compact_record_text, sort_records
from kannon.deps import ImportStatus, doctor_checks, run_doctor_repairs


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
