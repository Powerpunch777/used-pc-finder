"""Conservative SQLite backup operations for scheduled production scans."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_database(database_path: str | Path, backup_directory: str | Path, *, retain: int = 7) -> Path | None:
    """Integrity-check then create at most one UTC-day backup, retaining ``retain`` files."""
    if retain < 1:
        raise ValueError("backup retention must be positive")
    source_path, destination = Path(database_path), Path(backup_directory)
    destination.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    target = destination / f"listings-{day}.sqlite3"
    if target.exists():
        return None
    with sqlite3.connect(source_path) as source:
        row = source.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row[0] if row else 'no result'}")
        with sqlite3.connect(target) as copy:
            source.backup(copy)
    backups = sorted(destination.glob("listings-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[retain:]:
        stale.unlink()
    return target
