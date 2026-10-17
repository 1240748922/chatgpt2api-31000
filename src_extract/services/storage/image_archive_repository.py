"""Small, cross-replica receipts for best-effort extra-image archival."""
from __future__ import annotations

import time
from sqlalchemy import JSON, Column, Float, String, delete, select
from services.application_database import DatabaseBase


class ImageArchiveResult(DatabaseBase):
    __tablename__ = "image_archive_results"
    id = Column(String(32), primary_key=True)
    completed_at = Column(Float, nullable=False, index=True)
    payload = Column(JSON, nullable=False)


def save_receipt(engine, archive_id: str, payload: dict) -> None:
    with engine.begin() as connection:
        connection.execute(ImageArchiveResult.__table__.insert().values(
            id=archive_id, completed_at=time.time(), payload=payload))


def load_receipts(engine, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(select(ImageArchiveResult.id, ImageArchiveResult.payload)
                                  .where(ImageArchiveResult.id.in_(ids)))
        return {row.id: row.payload for row in rows}


def cleanup_receipts(engine, before: float) -> None:
    with engine.begin() as connection:
        connection.execute(delete(ImageArchiveResult).where(ImageArchiveResult.completed_at < before))
