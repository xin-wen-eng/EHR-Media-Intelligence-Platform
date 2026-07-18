"""
Unit tests for data cleaning edge cases.

Covers:
1. Date normalization across 6+ inconsistent formats
2. Gender code normalization (M/Male/male/MALE/1/empty → canonical)
3. MRN format normalization (various prefixes → MRN-XXXXXXXX)
4. Duplicate patient detection
5. Missing field handling
6. Duplicate lab result detection
"""

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from app.ingestion.cleaner import (
    DuplicateDetector,
    normalize_date,
    normalize_gender,
    normalize_mrn,
    normalize_name,
)
from app.ingestion.models import AuditLog, Gender
from app.ingestion.parser import ingest_lab_results, ingest_patients_json


def make_audit(record_id: str = "test") -> AuditLog:
    return AuditLog(record_id=record_id, record_type="test")


# ── Edge Case 1: Date Normalization ─────────────────────────────────


class TestDateNormalization:
    def test_iso_format_passthrough(self):
        audit = make_audit()
        result = normalize_date("2026-07-15", audit, "dob")
        assert result == date(2026, 7, 15)

    def test_us_slash_format(self):
        audit = make_audit()
        result = normalize_date("07/15/2026", audit, "dob")
        assert result == date(2026, 7, 15)
        assert any(c.action == "date_normalized" for c in audit.changes)

    def test_day_month_name_format(self):
        audit = make_audit()
        result = normalize_date("15-Jul-2026", audit, "dob")
        assert result == date(2026, 7, 15)

    def test_slash_ymd_format(self):
        audit = make_audit()
        result = normalize_date("2026/07/15", audit, "dob")
        assert result == date(2026, 7, 15)

    def test_us_dash_format(self):
        audit = make_audit()
        result = normalize_date("07-15-2026", audit, "dob")
        assert result == date(2026, 7, 15)

    def test_month_name_comma_format(self):
        audit = make_audit()
        result = normalize_date("Jul 15, 2026", audit, "dob")
        assert result == date(2026, 7, 15)

    def test_empty_string_returns_none(self):
        audit = make_audit()
        result = normalize_date("", audit, "dob")
        assert result is None
        assert "Missing dob" in audit.warnings

    def test_whitespace_only_returns_none(self):
        audit = make_audit()
        result = normalize_date("   ", audit, "dob")
        assert result is None

    def test_unparseable_date_returns_none(self):
        audit = make_audit()
        result = normalize_date("not-a-date", audit, "dob")
        assert result is None
        assert any("Unparseable" in w for w in audit.warnings)

    def test_audit_logs_change(self):
        audit = make_audit()
        normalize_date("07/15/2026", audit, "dob")
        assert len(audit.changes) == 1
        assert audit.changes[0].original_value == "07/15/2026"
        assert audit.changes[0].cleaned_value == "2026-07-15"


# ── Edge Case 2: Gender Code Normalization ──────────────────────────


class TestGenderNormalization:
    @pytest.mark.parametrize("input_val,expected", [
        ("M", Gender.MALE),
        ("Male", Gender.MALE),
        ("male", Gender.MALE),
        ("MALE", Gender.MALE),
        ("m", Gender.MALE),
        ("1", Gender.MALE),
        ("F", Gender.FEMALE),
        ("Female", Gender.FEMALE),
        ("female", Gender.FEMALE),
        ("FEMALE", Gender.FEMALE),
        ("f", Gender.FEMALE),
        ("2", Gender.FEMALE),
    ])
    def test_valid_gender_codes(self, input_val, expected):
        audit = make_audit()
        result = normalize_gender(input_val, audit)
        assert result == expected

    def test_empty_gender_returns_unknown(self):
        audit = make_audit()
        result = normalize_gender("", audit)
        assert result == Gender.UNKNOWN
        assert "Missing gender" in audit.warnings

    def test_unrecognized_gender_returns_unknown(self):
        audit = make_audit()
        result = normalize_gender("X", audit)
        assert result == Gender.UNKNOWN
        assert any("Unrecognized" in w for w in audit.warnings)

    def test_audit_logs_normalization(self):
        audit = make_audit()
        normalize_gender("MALE", audit)
        assert len(audit.changes) == 1
        assert audit.changes[0].original_value == "MALE"
        assert audit.changes[0].cleaned_value == "male"


# ── Edge Case 3: MRN Format Normalization ───────────────────────────


class TestMRNNormalization:
    def test_plain_id(self):
        audit = make_audit()
        result = normalize_mrn("e60dd03a", audit)
        assert result == "MRN-E60DD03A"

    def test_prefixed_mrn(self):
        audit = make_audit()
        result = normalize_mrn("MRN-E60DD03A", audit)
        assert result == "MRN-E60DD03A"

    def test_no_dash_prefix(self):
        audit = make_audit()
        result = normalize_mrn("MRNe60dd03a", audit)
        assert result == "MRN-E60DD03A"

    def test_with_dashes(self):
        audit = make_audit()
        result = normalize_mrn("e60d-d03a-beef", audit)
        assert result == "MRN-E60DD03A"

    def test_zero_padded(self):
        audit = make_audit()
        result = normalize_mrn("00abcdef", audit)
        assert result == "MRN-00ABCDEF"

    def test_long_id_truncated(self):
        audit = make_audit()
        result = normalize_mrn("e60dd03a-7d7f-0ff9", audit)
        assert result == "MRN-E60DD03A"

    def test_empty_mrn(self):
        audit = make_audit()
        result = normalize_mrn("", audit)
        assert result == ""
        assert "Missing MRN" in audit.warnings

    def test_audit_logs_normalization(self):
        audit = make_audit()
        normalize_mrn("e60dd03a", audit)
        assert len(audit.changes) == 1
        assert audit.changes[0].original_value == "e60dd03a"
        assert audit.changes[0].cleaned_value == "MRN-E60DD03A"


# ── Edge Case 4: Duplicate Patient Detection ────────────────────────


class TestDuplicateDetection:
    def test_first_patient_is_not_duplicate(self):
        detector = DuplicateDetector()
        is_dup, _ = detector.check_patient("MRN-001", "John", "Doe", date(1990, 1, 1))
        assert not is_dup

    def test_same_name_dob_is_duplicate(self):
        detector = DuplicateDetector()
        detector.check_patient("MRN-001", "John", "Doe", date(1990, 1, 1))
        is_dup, dup_of = detector.check_patient("MRN-002", "john", "doe", date(1990, 1, 1))
        assert is_dup
        assert dup_of == "MRN-001"

    def test_different_dob_is_not_duplicate(self):
        detector = DuplicateDetector()
        detector.check_patient("MRN-001", "John", "Doe", date(1990, 1, 1))
        is_dup, _ = detector.check_patient("MRN-002", "John", "Doe", date(1991, 1, 1))
        assert not is_dup

    def test_duplicate_lab(self):
        from datetime import datetime
        detector = DuplicateDetector()
        dt = datetime(2026, 7, 15, 10, 0)
        assert not detector.check_lab("P1", dt, "CBC")
        assert detector.check_lab("P1", dt, "CBC")

    def test_different_code_not_duplicate_lab(self):
        from datetime import datetime
        detector = DuplicateDetector()
        dt = datetime(2026, 7, 15, 10, 0)
        assert not detector.check_lab("P1", dt, "CBC")
        assert not detector.check_lab("P1", dt, "BMP")


# ── Edge Case 5: Missing Field Handling ─────────────────────────────


class TestMissingFields:
    def test_patient_with_missing_dob_gender_address(self):
        raw = [{
            "patient_id": "test-001",
            "mrn": "MRN-TEST0001",
            "first_name": "Jane",
            "last_name": "Doe",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "city": "Boston",
            "state": "MA",
            "zip": "02101",
        }]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(raw, f)
            f.flush()
            patients, audits = ingest_patients_json(Path(f.name))

        assert len(patients) == 1
        assert patients[0].date_of_birth is None
        assert patients[0].gender == Gender.UNKNOWN

        warnings = audits[0].warnings
        assert any("Missing date_of_birth" in w for w in warnings)
        assert any("Missing gender" in w for w in warnings)
        assert any("Missing address" in w for w in warnings)


# ── Edge Case 6: End-to-End Pipeline on Dirty CSV ──────────────────


class TestLabCSVIngestion:
    def test_duplicate_labs_removed(self):
        csv_content = (
            "patient_id,encounter_id,date,category,code,test_name,value,units,type\n"
            "P1,E1,07/15/2026,laboratory,CBC,Complete Blood Count,12.3,g/dL,numeric\n"
            "P1,E1,07/15/2026,laboratory,CBC,Complete Blood Count,12.3,g/dL,numeric\n"
            "P1,E1,07/15/2026,laboratory,BMP,Basic Metabolic Panel,140,mEq/L,numeric\n"
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            results, audits = ingest_lab_results(Path(f.name))

        assert len(results) == 2
        dup_audits = [a for a in audits if a.is_duplicate]
        assert len(dup_audits) == 1

    def test_missing_lab_value_logged(self):
        csv_content = (
            "patient_id,encounter_id,date,category,code,test_name,value,units,type\n"
            "P1,E1,2026-07-15,laboratory,WBC,White Blood Cell,,x10^9/L,numeric\n"
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            f.flush()
            results, audits = ingest_lab_results(Path(f.name))

        assert len(results) == 1
        assert results[0].value == ""
        assert any("Missing lab value" in w for w in audits[0].warnings)
