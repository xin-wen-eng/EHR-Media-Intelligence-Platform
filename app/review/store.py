"""
Review queue + audit log storage.

Every AI-generated summary starts as `pending`. A reviewer must explicitly
approve, edit, or reject it before the summary is considered trusted for
clinician use. Every state change and every patient view is persisted in
the audit log with actor, timestamp, and content hash.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "ehr.db"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_EDITED = "edited"
STATUS_REJECTED = "rejected"

VALID_STATUSES = {STATUS_PENDING, STATUS_APPROVED, STATUS_EDITED, STATUS_REJECTED}


def content_hash(summary: dict[str, Any] | str) -> str:
    if isinstance(summary, dict):
        payload = json.dumps(summary, sort_keys=True, ensure_ascii=False)
    else:
        payload = summary
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS summary_reviews (
                patient_mrn TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                original_hash TEXT NOT NULL,
                edited_summary TEXT,
                reviewer TEXT,
                reviewed_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                patient_mrn TEXT,
                content_hash TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_patient
                ON audit_log(patient_mrn);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_log(timestamp DESC);
        """)
        self.conn.commit()

    # -- Review queue -----------------------------------------------------

    def ensure_pending(self, patient_mrn: str, summary: dict[str, Any]) -> None:
        """Insert a `pending` row if this patient has no review record yet."""
        row = self.conn.execute(
            "SELECT patient_mrn FROM summary_reviews WHERE patient_mrn = ?",
            (patient_mrn,),
        ).fetchone()
        if row:
            return
        now = _utcnow()
        self.conn.execute(
            """
            INSERT INTO summary_reviews
                (patient_mrn, status, original_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (patient_mrn, STATUS_PENDING, content_hash(summary), now, now),
        )
        self.conn.commit()

    def get_review(self, patient_mrn: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM summary_reviews WHERE patient_mrn = ?",
            (patient_mrn,),
        ).fetchone()
        return dict(row) if row else None

    def list_reviews(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM summary_reviews WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM summary_reviews ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_status(
        self,
        patient_mrn: str,
        status: str,
        reviewer: str,
        edited_summary: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        now = _utcnow()
        self.conn.execute(
            """
            UPDATE summary_reviews SET
                status = ?,
                edited_summary = ?,
                reviewer = ?,
                reviewed_at = ?,
                notes = ?,
                updated_at = ?
            WHERE patient_mrn = ?
            """,
            (status, edited_summary, reviewer, now, notes, now, patient_mrn),
        )
        self.conn.commit()
        return self.get_review(patient_mrn)  # type: ignore[return-value]

    # -- Audit log --------------------------------------------------------

    def log(
        self,
        actor: str,
        role: str,
        action: str,
        patient_mrn: str | None = None,
        content_hash_value: str | None = None,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log
                (timestamp, actor, role, action, patient_mrn, content_hash, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_utcnow(), actor, role, action, patient_mrn, content_hash_value, notes),
        )
        self.conn.commit()

    def list_audit(
        self,
        limit: int = 200,
        patient_mrn: str | None = None,
    ) -> list[dict[str, Any]]:
        if patient_mrn:
            rows = self.conn.execute(
                "SELECT * FROM audit_log WHERE patient_mrn = ? ORDER BY id DESC LIMIT ?",
                (patient_mrn, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Users ------------------------------------------------------------

    def upsert_user(self, username: str, password_hash: str, role: str) -> None:
        self.conn.execute(
            """
            INSERT INTO users (username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                role = excluded.role
            """,
            (username, password_hash, role, _utcnow()),
        )
        self.conn.commit()

    def get_user(self, username: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT username, role FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Sessions ---------------------------------------------------------

    def create_session(
        self,
        actor: str,
        role: str,
        idle_timeout_seconds: int,
    ) -> dict[str, Any]:
        import secrets
        from datetime import datetime, timedelta, timezone as tz
        token = secrets.token_urlsafe(24)
        now_dt = datetime.now(tz.utc)
        now = now_dt.isoformat(timespec="seconds")
        expires = (now_dt + timedelta(seconds=idle_timeout_seconds)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO sessions (token, actor, role, created_at, last_activity_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, actor, role, now, now, expires),
        )
        self.conn.commit()
        return self.get_session(token)  # type: ignore[return-value]

    def get_session(self, token: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE token = ? AND revoked_at IS NULL",
            (token,),
        ).fetchone()
        return dict(row) if row else None

    def touch_session(
        self,
        token: str,
        idle_timeout_seconds: int,
    ) -> dict[str, Any] | None:
        from datetime import datetime, timedelta, timezone as tz
        session = self.get_session(token)
        if not session:
            return None
        now_dt = datetime.now(tz.utc)
        # Expired? mark revoked and return None
        if session["expires_at"] < now_dt.isoformat(timespec="seconds"):
            self.revoke_session(token, reason="idle_timeout")
            return None
        now = now_dt.isoformat(timespec="seconds")
        expires = (now_dt + timedelta(seconds=idle_timeout_seconds)).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE sessions SET last_activity_at = ?, expires_at = ? WHERE token = ?",
            (now, expires, token),
        )
        self.conn.commit()
        return self.get_session(token)

    def revoke_session(self, token: str, reason: str = "logout") -> None:
        self.conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL",
            (_utcnow(), token),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
