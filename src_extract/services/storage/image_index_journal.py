"""Durable per-image catalog commits; the legacy JSON is a compacted snapshot.

Publishing a new, uniquely named asset never needs the full-catalog mutex.
Compaction still uses the caller's existing interprocess catalog lock. Applied
entry IDs are committed WITH the snapshot, so a crash before journal cleanup
cannot replay a deleted image or roll back updated metadata.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


class ImageIndexSnapshot(dict):
    def __init__(self, items=(), *, pending_files=()):
        super().__init__(items)
        self.pending_files = tuple(pending_files)


def atomic_publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ImageIndexJournal:
    def __init__(self, index_file: Path):
        self.directory = index_file.with_suffix(index_file.suffix + ".pending")

    def publish(self, item: dict[str, object]) -> None:
        # Entries are immutable. A compactor may only acknowledge files it read.
        path = self.directory / f"{uuid4().hex}.json"
        atomic_publish(path, json.dumps(item, ensure_ascii=False).encode("utf-8"))

    def overlay(self, raw: dict) -> ImageIndexSnapshot:
        items = raw.get("items")
        result = {str(k): v for k, v in items.items() if isinstance(v, dict)} if isinstance(items, dict) else {}
        applied = set(raw.get("journal_applied") or ())
        files = tuple(self.directory.glob("*.json"))
        for path in files:
            if path.name in applied:
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Unlocked read-only readers may overlap completed compaction.
                continue
            if not isinstance(item, dict) or not isinstance(item.get("rel"), str):
                raise ValueError(f"invalid image journal entry: {path.name}")
            result[item["rel"]] = item
        return ImageIndexSnapshot(result, pending_files=files)

    @staticmethod
    def acknowledge(files) -> None:
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The snapshot already contains journal_applied; retry cleanup
                # on the next compaction without replaying these entries.
                pass
