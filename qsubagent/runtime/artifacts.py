from __future__ import annotations

import codecs
import shutil
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from qsubagent.config import ArtifactSettings


class ArtifactStore:
    def __init__(self, settings: ArtifactSettings) -> None:
        self.settings = settings
        self.root = settings.root.resolve()

    def create_capture(
        self,
        thread_id: str,
        turn_id: str,
        filename: str,
    ) -> "ArtifactCapture":
        directory = self.root / thread_id / turn_id
        directory.mkdir(parents=True, exist_ok=True)
        self.prune(thread_id, keep=directory)
        return ArtifactCapture(
            path=directory / filename,
            file_limit_bytes=self.settings.file_limit_bytes,
            preview_bytes=self.settings.preview_bytes,
        )

    def reference(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root)).replace("\\", "/")

    def resolve_reference(self, reference: str) -> Path:
        candidate = (self.root / reference).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact 路径超出允许目录") from exc
        if not candidate.is_file():
            raise FileNotFoundError(f"artifact 不存在：{reference}")
        return candidate

    def read(
        self,
        reference: str,
        *,
        start_byte: int = 0,
        max_bytes: int = 64 * 1024,
        start_line: int | None = None,
        line_count: int = 200,
    ) -> dict[str, Any]:
        if start_byte < 0 or max_bytes <= 0 or line_count <= 0:
            raise ValueError("读取范围必须为正数")
        max_bytes = min(max_bytes, self.settings.read_max_bytes)
        path = self.resolve_reference(reference)
        file_size = path.stat().st_size

        if start_line is not None:
            if start_line < 1:
                raise ValueError("start_line 必须从 1 开始")
            selected: list[bytes] = []
            selected_size = 0
            with path.open("rb") as stream:
                for number, line in enumerate(stream, start=1):
                    if number < start_line:
                        continue
                    if len(selected) >= line_count:
                        break
                    remaining = max_bytes - selected_size
                    if remaining <= 0:
                        break
                    selected.append(line[:remaining])
                    selected_size += min(len(line), remaining)
            data = b"".join(selected)
            return {
                "artifact": reference,
                "start_line": start_line,
                "lines_returned": len(selected),
                "bytes_returned": len(data),
                "file_size": file_size,
                "content": decode_bytes(data),
            }

        with path.open("rb") as stream:
            stream.seek(start_byte)
            data = stream.read(max_bytes)
        return {
            "artifact": reference,
            "start_byte": start_byte,
            "next_byte": start_byte + len(data),
            "bytes_returned": len(data),
            "file_size": file_size,
            "eof": start_byte + len(data) >= file_size,
            "content": decode_bytes(data),
        }

    def prune(self, thread_id: str, *, keep: Path | None = None) -> None:
        thread_root = self.root / thread_id
        if not thread_root.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.retention_days
        )
        directories = sorted(
            (path for path in thread_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        )
        for directory in list(directories):
            if keep is not None and directory.resolve() == keep.resolve():
                continue
            modified = datetime.fromtimestamp(
                directory.stat().st_mtime,
                tz=timezone.utc,
            )
            if modified < cutoff:
                shutil.rmtree(directory)
                directories.remove(directory)

        total = sum(_directory_size(path) for path in directories)
        for directory in directories:
            if total <= self.settings.thread_limit_bytes:
                break
            if keep is not None and directory.resolve() == keep.resolve():
                continue
            size = _directory_size(directory)
            shutil.rmtree(directory)
            total -= size


class ArtifactCapture:
    def __init__(
        self,
        *,
        path: Path,
        file_limit_bytes: int,
        preview_bytes: int,
    ) -> None:
        self.path = path
        self.file_limit_bytes = file_limit_bytes
        self.preview_bytes = preview_bytes
        self.total_bytes = 0
        self.written_bytes = 0
        self.truncated = False
        self._head = bytearray()
        self._tail: deque[int] = deque(maxlen=preview_bytes)
        self._stream: BinaryIO | None = None

    def __enter__(self) -> "ArtifactCapture":
        self._stream = self.path.open("wb")
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def write(self, data: bytes) -> None:
        self.total_bytes += len(data)
        if len(self._head) < self.preview_bytes:
            remaining = self.preview_bytes - len(self._head)
            self._head.extend(data[:remaining])
        self._tail.extend(data)

        writable = min(
            len(data),
            max(0, self.file_limit_bytes - self.written_bytes),
        )
        if writable and self._stream is not None:
            self._stream.write(data[:writable])
            self.written_bytes += writable
        if writable < len(data):
            self.truncated = True

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def summary(self) -> dict[str, Any]:
        tail = bytes(self._tail)
        if self.total_bytes <= self.preview_bytes:
            tail = b""
        return {
            "total_bytes": self.total_bytes,
            "written_bytes": self.written_bytes,
            "artifact_truncated": self.truncated,
            "head": decode_bytes(bytes(self._head)),
            "tail": decode_bytes(tail),
        }


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=True)


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
