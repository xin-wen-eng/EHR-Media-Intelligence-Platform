"""
SQLite cache for AI-generated clinical summaries.

Keyed by patient_id + content hash to avoid redundant API calls.
"""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "ehr.db"


class SummaryCache:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_table()

    def _init_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS summary_cache (
                patient_mrn TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (patient_mrn, content_hash)
            )
        """)
        self.conn.commit()

    @staticmethod
    def compute_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def get(self, patient_mrn: str, content_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT summary_json FROM summary_cache WHERE patient_mrn = ? AND content_hash = ?",
            (patient_mrn, content_hash),
        ).fetchone()
        if row:
            return json.loads(row["summary_json"])
        return None

    def put(self, patient_mrn: str, content_hash: str, summary: dict, model: str):
        self.conn.execute("""
            INSERT INTO summary_cache (patient_mrn, content_hash, summary_json, model, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(patient_mrn, content_hash) DO UPDATE SET
                summary_json = excluded.summary_json,
                model = excluded.model,
                created_at = excluded.created_at
        """, (patient_mrn, content_hash, json.dumps(summary), model, datetime.now().isoformat()))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM summary_cache").fetchone()[0]

    def close(self):
        self.conn.close()
