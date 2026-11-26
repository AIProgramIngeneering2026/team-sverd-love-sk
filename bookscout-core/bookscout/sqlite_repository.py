"""SQLite persistence adapter. No SQL/database dependency leaks into core."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bookscout.core.errors import IdempotencyConflict
from bookscout.core.job import Job, JobState


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"], request_id=row["request_id"], state=JobState(row["state"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error_code=row["error_code"], error_message=row["error_message"],
        created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
    )


class SQLiteJobRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def add(self, job: Job) -> None:
        self.create_or_get(job, None, None)

    def create_or_get(self, job: Job, idempotency_key: str | None,
                      request_hash: str | None) -> tuple[Job, bool]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key уже использован с другим телом запроса",
                            field="Idempotency-Key",
                            details={"idempotency_key": idempotency_key},
                        )
                    conn.commit()
                    return _job_from_row(existing), False
            now = job.created_at.isoformat()
            conn.execute(
                """INSERT INTO jobs
                   (id, request_id, state, result_json, error_code, error_message,
                    created_at, updated_at, idempotency_key, request_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.id, job.request_id, job.state.value, None, None, None, now, now,
                 idempotency_key, request_hash),
            )
            conn.execute(
                "INSERT INTO job_events(job_id, from_state, to_state, occurred_at) VALUES (?, NULL, ?, ?)",
                (job.id, job.state.value, now),
            )
            conn.commit()
            return job, True
        except sqlite3.IntegrityError:
            conn.rollback()
            # A concurrent creator may have won the unique-key race.
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key уже использован с другим телом запроса",
                            field="Idempotency-Key",
                            details={"idempotency_key": idempotency_key},
                        )
                    return _job_from_row(existing), False
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save(self, job: Job) -> None:
        """Persist state, result and event history atomically."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT state, result_json, error_code FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if previous is None:
                raise KeyError(job.id)
            result_json = _json(job.result)
            changed = (previous["state"] != job.state.value
                       or previous["result_json"] != result_json
                       or previous["error_code"] != job.error_code)
            conn.execute(
                """UPDATE jobs SET state=?, result_json=?, error_code=?, error_message=?, updated_at=?
                   WHERE id=?""",
                (job.state.value, result_json, job.error_code, job.error_message,
                 job.updated_at.isoformat(), job.id),
            )
            if changed:
                conn.execute(
                    """INSERT INTO job_events
                       (job_id, from_state, to_state, occurred_at, error_code, result_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job.id, previous["state"], job.state.value, job.updated_at.isoformat(),
                     job.error_code, result_json),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_by_idempotency_key(self, key: str, request_hash: str) -> Job | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "Idempotency-Key уже использован с другим телом запроса",
                    field="Idempotency-Key", details={"idempotency_key": key},
                )
            return _job_from_row(row)
        finally:
            conn.close()

    def get(self, job_id: str) -> Job | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _job_from_row(row) if row else None
        finally:
            conn.close()

    def list_all(self) -> list[Job]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at, id").fetchall()
            return [_job_from_row(row) for row in rows]
        finally:
            conn.close()

    def history(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def migrate(database_path: str | Path, migrations_dir: str | Path | None = None) -> list[str]:
    """Apply numbered SQL migrations once; no schema is created by app startup."""
    root = Path(migrations_dir) if migrations_dir else Path(__file__).resolve().parent / "migrations"
    conn = sqlite3.connect(str(database_path), timeout=10, isolation_level=None)
    applied: list[str] = []
    try:
        for path in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if exists:
                done = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (path.name,)
                ).fetchone()
                if done:
                    continue
            sql = path.read_text(encoding="utf-8")
            stamp = datetime.now(timezone.utc).isoformat()
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + sql +
                f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ('{path.name}', '{stamp}');\nCOMMIT;"
            )
            applied.append(path.name)
    finally:
        conn.close()
    return applied
