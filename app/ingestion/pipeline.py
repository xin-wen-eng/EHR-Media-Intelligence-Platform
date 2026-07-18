"""
Main ingestion pipeline.

Orchestrates: parse → clean → validate → output cleaned data + audit logs.
"""

import json
from pathlib import Path

from .models import AuditLog
from .parser import (
    ingest_clinical_notes,
    ingest_encounters_from_json,
    ingest_imaging_reports,
    ingest_lab_results,
    ingest_patients_json,
)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
CLEANED_DIR = DATA_DIR / "cleaned"


def save_audit_logs(audits: list[AuditLog], filename: str):
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for a in audits:
        records.append({
            "record_id": a.record_id,
            "record_type": a.record_type,
            "timestamp": a.timestamp.isoformat(),
            "is_duplicate": a.is_duplicate,
            "duplicate_of": a.duplicate_of,
            "changes": [c.model_dump() for c in a.changes],
            "warnings": a.warnings,
        })
    path = CLEANED_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    return path


def save_cleaned_records(records, filename: str):
    CLEANED_DIR.mkdir(parents=True, exist_ok=True)
    path = CLEANED_DIR / filename
    data = [r.model_dump(mode="json") for r in records]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    return path


def run_pipeline():
    print("=" * 60)
    print("EHR Data Ingestion Pipeline")
    print("=" * 60)

    all_audits: list[AuditLog] = []

    # 1. Patients (JSON)
    print("\n[1/5] Ingesting patients from JSON...")
    patients, patient_audits = ingest_patients_json(RAW_DIR / "patients_ehr_export.json")
    all_audits.extend(patient_audits)
    dups = sum(1 for a in patient_audits if a.is_duplicate)
    print(f"  Loaded: {len(patients)} patients ({dups} duplicates removed)")

    # 2. Encounters (embedded in JSON)
    print("[2/5] Extracting encounters...")
    encounters = ingest_encounters_from_json(RAW_DIR / "patients_ehr_export.json")
    print(f"  Loaded: {len(encounters)} encounters")

    # 3. Lab results (CSV)
    print("[3/5] Ingesting lab results from CSV...")
    labs, lab_audits = ingest_lab_results(RAW_DIR / "lab_results.csv")
    all_audits.extend(lab_audits)
    lab_dups = sum(1 for a in lab_audits if a.is_duplicate)
    print(f"  Loaded: {len(labs)} lab results ({lab_dups} duplicates removed)")

    # 4. Imaging reports (CSV)
    print("[4/5] Ingesting imaging reports from CSV...")
    imaging, img_audits = ingest_imaging_reports(RAW_DIR / "imaging_reports.csv")
    all_audits.extend(img_audits)
    print(f"  Loaded: {len(imaging)} imaging reports")

    # 5. Clinical notes (CSV)
    print("[5/5] Ingesting clinical notes from CSV...")
    notes, note_audits = ingest_clinical_notes(RAW_DIR / "clinical_notes.csv")
    all_audits.extend(note_audits)
    print(f"  Loaded: {len(notes)} clinical notes")

    # Save cleaned data
    print("\nSaving cleaned data...")
    save_cleaned_records(patients, "patients.json")
    save_cleaned_records(encounters, "encounters.json")
    save_cleaned_records(labs, "lab_results.json")
    save_cleaned_records(imaging, "imaging_reports.json")
    save_cleaned_records(notes, "clinical_notes.json")

    # Save audit log
    audit_path = save_audit_logs(all_audits, "audit_log.json")

    # Summary
    changes = sum(len(a.changes) for a in all_audits)
    warnings = sum(len(a.warnings) for a in all_audits)
    duplicates = sum(1 for a in all_audits if a.is_duplicate)

    print(f"\n{'=' * 60}")
    print("Pipeline Summary")
    print(f"{'=' * 60}")
    print(f"  Total records processed:  {len(all_audits)}")
    print(f"  Duplicates removed:       {duplicates}")
    print(f"  Fields normalized:        {changes}")
    print(f"  Warnings raised:          {warnings}")
    print(f"  Audit log:                {audit_path}")
    print(f"  Cleaned data:             {CLEANED_DIR}/")

    return {
        "patients": patients,
        "encounters": encounters,
        "labs": labs,
        "imaging": imaging,
        "notes": notes,
        "audits": all_audits,
    }


if __name__ == "__main__":
    run_pipeline()
