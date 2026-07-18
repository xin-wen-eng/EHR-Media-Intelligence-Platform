"""
SQLite storage for FHIR Bundles.

Stores each patient's FHIR Bundle as JSON for downstream retrieval.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from fhir.resources.bundle import Bundle

DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "ehr.db"


class FHIRStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS fhir_bundles (
                patient_mrn TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL,
                bundle_json TEXT NOT NULL,
                resource_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_bundle_id
                ON fhir_bundles(bundle_id);
        """)
        self.conn.commit()

    def save_bundle(self, patient_mrn: str, bundle: Bundle):
        now = datetime.now().isoformat()
        bundle_json = bundle.model_dump_json(indent=2)
        resource_count = len(bundle.entry) if bundle.entry else 0

        self.conn.execute("""
            INSERT INTO fhir_bundles (patient_mrn, bundle_id, bundle_json, resource_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(patient_mrn) DO UPDATE SET
                bundle_id = excluded.bundle_id,
                bundle_json = excluded.bundle_json,
                resource_count = excluded.resource_count,
                updated_at = excluded.updated_at
        """, (patient_mrn, bundle.id, bundle_json, resource_count, now, now))
        self.conn.commit()

    def get_bundle(self, patient_mrn: str) -> Bundle | None:
        row = self.conn.execute(
            "SELECT bundle_json FROM fhir_bundles WHERE patient_mrn = ?",
            (patient_mrn,),
        ).fetchone()
        if not row:
            return None
        return Bundle.model_validate_json(row["bundle_json"])

    def get_bundle_json(self, patient_mrn: str) -> str | None:
        row = self.conn.execute(
            "SELECT bundle_json FROM fhir_bundles WHERE patient_mrn = ?",
            (patient_mrn,),
        ).fetchone()
        return row["bundle_json"] if row else None

    def list_patients(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT patient_mrn, bundle_id, resource_count, created_at FROM fhir_bundles ORDER BY patient_mrn"
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM fhir_bundles").fetchone()
        return row["cnt"]

    def close(self):
        self.conn.close()
