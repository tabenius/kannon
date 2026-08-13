from __future__ import annotations

import ctypes
import os
import struct
import time
from pathlib import Path
from typing import Any


IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MODIFY = 0x00000002
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_IGNORED = 0x00008000
IN_CLOEXEC = 0x00080000
IN_NONBLOCK = 0x00000800

WATCH_MASK = (
    IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MODIFY
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)

Signature = tuple[str, int, int]


class InotifyBackend:
    def __init__(self) -> None:
        if os.name != "posix" or not sys_platform_is_linux():
            raise OSError("inotify is only available on Linux")
        libc = ctypes.CDLL(None, use_errno=True)
        self._inotify_add_watch = libc.inotify_add_watch
        self._inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._inotify_add_watch.restype = ctypes.c_int
        self._inotify_rm_watch = libc.inotify_rm_watch
        self._inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self._inotify_rm_watch.restype = ctypes.c_int
        init1 = libc.inotify_init1
        init1.argtypes = [ctypes.c_int]
        init1.restype = ctypes.c_int
        fd = init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        self.fd = fd
        self.watches: dict[int, set[int]] = {}

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def add_path(self, path: Path, index: int) -> None:
        wd = self._inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno), str(path))
        self.watches.setdefault(wd, set()).add(index)

    def remove_index(self, index: int) -> None:
        empty: list[int] = []
        for wd, indices in self.watches.items():
            indices.discard(index)
            if not indices:
                empty.append(wd)
        for wd in empty:
            self.watches.pop(wd, None)
            self._inotify_rm_watch(self.fd, wd)

    def changed_indices(self) -> set[int]:
        changed: set[int] = set()
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, _cookie, name_len = struct.unpack_from("iIII", data, offset)
                offset += 16 + name_len
                indices = self.watches.get(wd, set())
                changed.update(indices)
                if mask & IN_IGNORED:
                    self.watches.pop(wd, None)
        return changed


class DocumentWatcher:
    def __init__(self, records: list[dict[str, Any]], mode: str, interval: float) -> None:
        self.requested_mode = mode
        self.interval = max(0.1, interval)
        self.last_poll = 0.0
        self.signatures = [record_signature(record) for record in records]
        self.backend: InotifyBackend | None = None
        self.message: str | None = None
        if mode in {"auto", "inotify"}:
            try:
                self.backend = InotifyBackend()
                for index, record in enumerate(records):
                    path = record_path(record)
                    if path is not None and path.exists():
                        try:
                            self.backend.add_path(path, index)
                        except OSError:
                            pass
            except OSError as exc:
                if mode == "inotify":
                    self.message = f"inotify unavailable ({exc}); using polling instead."
        self.active_mode = "inotify+poll" if self.backend is not None else "poll"

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()

    def changed_indices(self, records: list[dict[str, Any]]) -> list[int]:
        changed: set[int] = set()
        if self.backend is not None:
            changed.update(self.backend.changed_indices())
        now = time.monotonic()
        if now - self.last_poll >= self.interval:
            self.last_poll = now
            changed.update(self._poll_changed(records))
        return sorted(index for index in changed if 0 <= index < len(records))

    def update_record(self, index: int, record: dict[str, Any]) -> None:
        while len(self.signatures) <= index:
            self.signatures.append(("missing", 0, 0))
        self.signatures[index] = record_signature(record)
        if self.backend is not None:
            self.backend.remove_index(index)
            path = record_path(record)
            if path is not None and path.exists():
                try:
                    self.backend.add_path(path, index)
                except OSError:
                    pass

    def mark_seen(self, index: int, record: dict[str, Any]) -> None:
        while len(self.signatures) <= index:
            self.signatures.append(("missing", 0, 0))
        self.signatures[index] = current_signature(record)

    def _poll_changed(self, records: list[dict[str, Any]]) -> set[int]:
        changed: set[int] = set()
        if len(self.signatures) < len(records):
            self.signatures.extend(record_signature(record) for record in records[len(self.signatures) :])
        for index, record in enumerate(records):
            signature = current_signature(record)
            if signature != self.signatures[index]:
                self.signatures[index] = signature
                changed.add(index)
        return changed


def sys_platform_is_linux() -> bool:
    return os.uname().sysname == "Linux" if hasattr(os, "uname") else False


def record_path(record: dict[str, Any]) -> Path | None:
    path_text = str(record.get("path_abs") or record.get("path") or "")
    return Path(path_text).expanduser() if path_text else None


def record_signature(record: dict[str, Any]) -> Signature:
    source = record.get("source", {})
    if not isinstance(source, dict):
        return current_signature(record)
    try:
        return ("ok", int(source.get("mtime_ns")), int(source.get("size_bytes")))
    except (TypeError, ValueError):
        return current_signature(record)


def current_signature(record: dict[str, Any]) -> Signature:
    path = record_path(record)
    if path is None:
        return ("missing", 0, 0)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ("missing", 0, 0)
    except OSError:
        return ("error", 0, 0)
    return ("ok", int(stat.st_mtime_ns), int(stat.st_size))
