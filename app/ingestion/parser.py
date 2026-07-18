"""
Parsers for raw EHR input formats.

Supports:
- JSON: simulated EHR system export (patients_ehr_export.json)
- CSV:  lab results, imaging reports, clinical notes
"""

import csv
import json
from pathlib import Path

from .models import (
    AuditLog,
    CleanedClinicalNote,
    CleanedEncounter,
    CleanedImagingReport,
    CleanedLabResult,
    CleanedPatient,
)
from .cleaner import (
    DuplicateDetector,
    normalize_date,
    normalize_datetime,
    normalize_gender,
    normalize_mrn,
    normalize_name,
)


def ingest_patients_json(filepath: Path) -> tuple[list[CleanedPatient], list[AuditLog]]:
    with open(filepath, encoding="utf-8") as f:
        raw_records = json.load(f)

    patients: list[CleanedPatient] = []
    audits: list[AuditLog] = []
    detector = DuplicateDetector()

    for raw in raw_records:
        pid = raw.get("patient_id", "")
        audit = AuditLog(record_id=pid, record_type="patient")

        mrn = normalize_mrn(raw.get("mrn", ""), audit)
        first = normalize_name(raw.get("first_name", ""))
        last = normalize_name(raw.get("last_name", ""))
        dob = normalize_date(raw.get("date_of_birth", ""), audit, "date_of_birth")
        gender = normalize_gender(raw.get("gender", ""), audit)

        is_dup, dup_of = detector.check_patient(mrn, first, last, dob)
        if is_dup:
            audit.is_duplicate = True
            audit.duplicate_of = dup_of
            audit.add_warning(f"Duplicate patient detected (same as MRN {dup_of})")
            audits.append(audit)
            continue

        if not raw.get("address", "").strip():
            audit.add_warning("Missing address")

        death_date = normalize_date(raw.get("death_date", ""), audit, "death_date") if raw.get("death_date") else None

        patient = CleanedPatient(
            patient_id=pid,
            mrn=mrn,
            first_name=first,
            last_name=last,
            middle_name=normalize_name(raw.get("middle_name", "")),
            date_of_birth=dob,
            gender=gender,
            race=raw.get("race", ""),
            ethnicity=raw.get("ethnicity", ""),
            address=raw.get("address", "").strip(),
            city=raw.get("city", ""),
            state=raw.get("state", ""),
            zip_code=raw.get("zip", ""),
            marital_status=raw.get("marital_status", ""),
            death_date=death_date,
            audit=audit,
        )
        patients.append(patient)
        audits.append(audit)

    return patients, audits


def ingest_encounters_from_json(filepath: Path) -> list[CleanedEncounter]:
    with open(filepath, encoding="utf-8") as f:
        raw_records = json.load(f)

    encounters = []
    for raw in raw_records:
        pid = raw.get("patient_id", "")
        for enc in raw.get("encounters", []):
            audit = AuditLog(record_id=enc.get("encounter_id", ""), record_type="encounter")
            encounters.append(CleanedEncounter(
                encounter_id=enc.get("encounter_id", ""),
                patient_id=pid,
                start=normalize_datetime(enc.get("start", ""), audit, "start"),
                end=normalize_datetime(enc.get("end", ""), audit, "end"),
                encounter_class=enc.get("class", ""),
                encounter_type=enc.get("type", ""),
                reason=enc.get("reason", ""),
            ))
    return encounters


def _read_csv(filepath: Path) -> list[dict]:
    with open(filepath, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ingest_lab_results(filepath: Path) -> tuple[list[CleanedLabResult], list[AuditLog]]:
    rows = _read_csv(filepath)
    results: list[CleanedLabResult] = []
    audits: list[AuditLog] = []
    detector = DuplicateDetector()

    for row in rows:
        rid = f"{row.get('patient_id', '')}_{row.get('code', '')}_{row.get('date', '')}"
        audit = AuditLog(record_id=rid, record_type="lab_result")

        dt = normalize_datetime(row.get("date", ""), audit, "date")

        is_dup = detector.check_lab(row.get("patient_id", ""), dt, row.get("code", ""))
        if is_dup:
            audit.is_duplicate = True
            audit.add_warning("Duplicate lab result")
            audits.append(audit)
            continue

        if not row.get("value", "").strip():
            audit.add_warning("Missing lab value")

        results.append(CleanedLabResult(
            patient_id=row.get("patient_id", ""),
            encounter_id=row.get("encounter_id", ""),
            date=dt,
            category=row.get("category", ""),
            code=row.get("code", ""),
            test_name=row.get("test_name", ""),
            value=row.get("value", "").strip(),
            units=row.get("units", ""),
            value_type=row.get("type", ""),
            audit=audit,
        ))
        audits.append(audit)

    return results, audits


def ingest_imaging_reports(filepath: Path) -> tuple[list[CleanedImagingReport], list[AuditLog]]:
    rows = _read_csv(filepath)
    results: list[CleanedImagingReport] = []
    audits: list[AuditLog] = []

    for row in rows:
        rid = f"{row.get('patient_id', '')}_{row.get('date', '')}"
        audit = AuditLog(record_id=rid, record_type="imaging_report")

        results.append(CleanedImagingReport(
            patient_id=row.get("patient_id", ""),
            encounter_id=row.get("encounter_id", ""),
            date=normalize_datetime(row.get("date", ""), audit, "date"),
            modality=row.get("modality", ""),
            body_site=row.get("body_site", ""),
            report_text=row.get("report_text", ""),
            audit=audit,
        ))
        audits.append(audit)

    return results, audits


def ingest_clinical_notes(filepath: Path) -> tuple[list[CleanedClinicalNote], list[AuditLog]]:
    rows = _read_csv(filepath)
    results: list[CleanedClinicalNote] = []
    audits: list[AuditLog] = []

    for row in rows:
        rid = f"{row.get('patient_id', '')}_{row.get('date', '')}_{row.get('note_type', '')}"
        audit = AuditLog(record_id=rid, record_type="clinical_note")

        results.append(CleanedClinicalNote(
            patient_id=row.get("patient_id", ""),
            encounter_id=row.get("encounter_id", ""),
            date=normalize_datetime(row.get("date", ""), audit, "date"),
            note_type=row.get("note_type", ""),
            author=row.get("author", ""),
            text=row.get("text", ""),
            audit=audit,
        ))
        audits.append(audit)

    return results, audits
