from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_VERSION = 1


def load_cache(cache_path: Path, yaml_module: Any) -> dict[str, Any]:
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            data = yaml_module.safe_load(handle) or {}
    except OSError as exc:
        print(f"warning: cannot read cache {cache_path}: {exc}; rebuilding cache.", file=sys.stderr)
        return {}
    except yaml_module.YAMLError as exc:
        print(f"warning: cannot parse cache {cache_path}: {exc}; rebuilding cache.", file=sys.stderr)
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        print(f"warning: ignoring unsupported cache format in {cache_path}; rebuilding cache.", file=sys.stderr)
        return {}
    documents = data.get("documents", [])
    if not isinstance(documents, list):
        print(f"warning: ignoring cache with invalid documents list in {cache_path}; rebuilding cache.", file=sys.stderr)
        return {}
    return data


def save_cache(
    cache_path: Path,
    records: list[dict[str, Any]],
    thumbnail_size: int,
    yaml_module: Any,
) -> None:
    payload = {
        "version": CACHE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thumbnail_size": thumbnail_size,
        "documents": records,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(cache_path.parent),
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            yaml_module.safe_dump(payload, handle, sort_keys=False, allow_unicode=False, width=100)
        os.replace(temp_name, cache_path)
    except OSError as exc:
        raise SystemExit(
            "\n".join(
                [
                    f"Kannon could not write the cache file: {cache_path}",
                    f"Reason: {exc}",
                    "Consequence: thumbnails and metadata cannot be saved for reuse.",
                    "Actions:",
                    "  - Choose a writable cache path with --cache /path/to/kannon.yaml",
                    "  - Check directory permissions and available disk space.",
                ]
            )
        ) from exc
    finally:
        if "temp_name" in locals():
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
