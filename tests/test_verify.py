"""
Unit tests for fact-grounding logic (app.review.verify).
"""

import base64

import pytest

from app.review.verify import verify_summary


def _bundle_with(entries):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in entries]}


def _doc(text, date="2025-01-01", doc_type="Progress note"):
    return {
        "resourceType": "DocumentReference",
        "id": f"doc-{hash(text) & 0xFFFF:04x}",
        "date": date,
        "type": {"text": doc_type},
        "content": [{"attachment": {"data": base64.b64encode(text.encode()).decode()}}],
    }


def _lab(name, conclusion, date="2025-01-01"):
    return {
        "resourceType": "DiagnosticReport",
        "id": f"lab-{hash(name) & 0xFFFF:04x}",
        "effectiveDateTime": date,
        "code": {"text": name},
        "conclusion": conclusion,
    }


def _encounter(reason, date="2025-01-01"):
    return {
        "resourceType": "Encounter",
        "id": f"enc-{hash(reason) & 0xFFFF:04x}",
        "actualPeriod": {"start": date},
        "reason": [{"use": [{"concept": {"text": reason}}]}],
    }


class TestVerifySummaryShape:
    def test_returns_expected_keys(self):
        summary = {"chief_concern": "", "key_diagnoses": [], "recent_labs": [], "recent_imaging": [], "flagged_anomalies": []}
        bundle = _bundle_with([])
        result = verify_summary(summary, bundle)

        assert "counts" in result
        assert set(result["counts"].keys()) == {"total", "grounded", "unverified"}
        assert "insufficient_data" in result
        assert "fields" in result
        assert set(result["fields"].keys()) >= {
            "chief_concern",
            "key_diagnoses",
            "recent_labs",
            "recent_imaging",
            "flagged_anomalies",
        }

    def test_empty_bundle_marks_insufficient(self):
        summary = {"chief_concern": "Hyperlipidemia", "key_diagnoses": ["Hyperlipidemia"]}
        result = verify_summary(summary, _bundle_with([]))
        assert result["insufficient_data"] is True

    def test_nonempty_bundle_not_insufficient(self):
        result = verify_summary(
            {"chief_concern": "Hyperlipidemia"},
            _bundle_with([_encounter("Hyperlipidemia")]),
        )
        assert result["insufficient_data"] is False


class TestGrounding:
    def test_grounded_when_substring_matches(self):
        summary = {"key_diagnoses": ["Hyperlipidemia"]}
        bundle = _bundle_with([_encounter("Hyperlipidemia follow-up")])
        result = verify_summary(summary, bundle)
        [item] = result["fields"]["key_diagnoses"]
        assert item["grounded"] is True
        assert len(item["evidence"]) >= 1

    def test_grounded_via_lab_code_and_number(self):
        summary = {"recent_labs": ["HbA1c: 6.2 %"]}
        bundle = _bundle_with([_lab("HbA1c", "HbA1c: 6.2 % laboratory")])
        result = verify_summary(summary, bundle)
        [item] = result["fields"]["recent_labs"]
        assert item["grounded"] is True

    def test_ungrounded_when_absent(self):
        summary = {"recent_labs": ["BMI: 30.1 kg/m2"]}
        bundle = _bundle_with([_lab("HbA1c", "HbA1c: 6.2 %")])
        result = verify_summary(summary, bundle)
        [item] = result["fields"]["recent_labs"]
        assert item["grounded"] is False
        assert item["evidence"] == []

    def test_counts_accumulate(self):
        summary = {
            "chief_concern": "Hyperlipidemia",  # grounded
            "key_diagnoses": ["Hyperlipidemia", "Diabetes"],  # 1 grounded, 1 not
            "recent_labs": ["HbA1c: 6.2 %"],  # grounded
            "flagged_anomalies": ["obese BMI (30.1 kg/m2)"],  # not grounded
        }
        bundle = _bundle_with([
            _encounter("Hyperlipidemia"),
            _lab("HbA1c", "HbA1c: 6.2 %"),
        ])
        result = verify_summary(summary, bundle)
        counts = result["counts"]
        # 1 (chief) + 2 (diagnoses) + 1 (lab) + 1 (anomaly) = 5
        assert counts["total"] == 5
        # Grounded: Hyperlipidemia chief_concern, Hyperlipidemia diagnosis, HbA1c lab
        assert counts["grounded"] == 3
        # Unverified: Diabetes diagnosis, obese BMI anomaly
        assert counts["unverified"] == 2

    def test_evidence_contains_resource_metadata(self):
        summary = {"key_diagnoses": ["Hyperlipidemia"]}
        bundle = _bundle_with([_encounter("Hyperlipidemia follow-up", date="2025-06-15")])
        result = verify_summary(summary, bundle)
        [item] = result["fields"]["key_diagnoses"]
        ev = item["evidence"][0]
        assert ev["resource_type"] == "Encounter"
        assert ev["date"] == "2025-06-15"
        assert ev["resource_id"]
        assert "Hyperlipidemia" in ev["snippet"] or ev["snippet"]


class TestFieldShapes:
    def test_chief_concern_is_single_or_none(self):
        result = verify_summary(
            {"chief_concern": "Hyperlipidemia"},
            _bundle_with([_encounter("Hyperlipidemia")]),
        )
        cc = result["fields"]["chief_concern"]
        assert isinstance(cc, dict)
        assert "claim" in cc and "grounded" in cc and "evidence" in cc

    def test_no_chief_concern(self):
        result = verify_summary({}, _bundle_with([_encounter("x")]))
        assert result["fields"]["chief_concern"] is None

    def test_list_fields_return_lists(self):
        result = verify_summary(
            {"key_diagnoses": ["A"], "recent_labs": ["B"]},
            _bundle_with([_encounter("A"), _lab("B", "B: 1 unit")]),
        )
        assert isinstance(result["fields"]["key_diagnoses"], list)
        assert isinstance(result["fields"]["recent_labs"], list)


class TestNonStringItems:
    def test_non_string_list_items_are_skipped(self):
        """Defensive: verify_summary must not crash on malformed input."""
        result = verify_summary(
            {"key_diagnoses": ["Hyperlipidemia", None, 42]},
            _bundle_with([_encounter("Hyperlipidemia")]),
        )
        # Only the valid string is verified
        assert len(result["fields"]["key_diagnoses"]) == 1
