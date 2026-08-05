"""
Unit tests for PII masking and redaction utilities.
"""

import base64

import pytest

from app.review.pii import (
    mask_bundle,
    mask_dob,
    mask_mrn,
    mask_name,
    mask_record,
    mask_search_hit,
    mask_summary,
    redact_text,
)


class TestFieldMaskers:
    def test_mask_mrn_short(self):
        assert mask_mrn("ABC") == "MRN-****ABC"

    def test_mask_mrn_long_keeps_last4(self):
        assert mask_mrn("MRN-C05BB077") == "MRN-****B077"

    def test_mask_mrn_none_or_empty(self):
        assert mask_mrn(None) == ""
        assert mask_mrn("") == ""

    def test_mask_name_derives_from_mrn(self):
        assert mask_name("MRN-C05BB077") == "Patient B077"

    def test_mask_name_fallback(self):
        assert mask_name(None) == "Patient ????"

    def test_mask_dob_year_only(self):
        assert mask_dob("1969-03-15") == "1969"
        assert mask_dob("2001-12-31T00:00:00") == "2001"

    def test_mask_dob_missing(self):
        assert mask_dob(None) == ""
        assert mask_dob("") == ""


class TestRedactText:
    def test_redacts_full_name(self):
        out = redact_text("PROGRESS NOTE Patient: Janiece99 Bogan287", ["Janiece99 Bogan287"])
        assert "Janiece99" not in out
        assert "Bogan287" not in out
        assert "[PATIENT]" in out

    def test_redacts_partial_variants(self):
        """First name and last name should also match when full name is used."""
        out = redact_text("Follow-up with Janiece next week", ["Janiece99 Bogan287"])
        # 'Janiece' is a variant with trailing digits stripped
        assert "Janiece" not in out
        assert "[PATIENT]" in out

    def test_case_insensitive(self):
        out = redact_text("patient BOGAN287 signed the consent", ["Janiece Bogan287"])
        assert "BOGAN287" not in out

    def test_short_tokens_ignored(self):
        """Redacting single-letter or 2-letter tokens would nuke everything;
        variants shorter than 3 chars are skipped."""
        out = redact_text("The is a note", ["Xi"])
        assert out == "The is a note"

    def test_empty_inputs(self):
        assert redact_text("", ["Name"]) == ""
        assert redact_text("hello", []) == "hello"
        assert redact_text("hello", [""]) == "hello"


class TestMaskSearchHit:
    def _hit(self):
        return {
            "id": "abc",
            "patient_mrn": "MRN-C05BB077",
            "patient_name": "Janiece99 Bogan287",
            "text": "PROGRESS NOTE\nPatient: Janiece99 Bogan287",
            "summary_snippet": "Janiece99 Bogan287 with hyperlipidemia",
            "resource_type": "ProgressNote",
        }

    def test_disabled_preserves_but_adds_display(self):
        out = mask_search_hit(self._hit(), enabled=False)
        assert out["patient_mrn"] == "MRN-C05BB077"
        assert out["patient_name"] == "Janiece99 Bogan287"
        assert out["patient_mrn_display"] == "MRN-C05BB077"

    def test_enabled_masks_all_pii_fields(self):
        out = mask_search_hit(self._hit(), enabled=True)
        # Real MRN preserved as opaque lookup key
        assert out["patient_mrn"] == "MRN-C05BB077"
        # Display is masked
        assert out["patient_mrn_display"] == "MRN-****B077"
        assert out["patient_name"] == "Patient B077"
        # Name mentions redacted in free-text
        assert "Janiece99" not in out["text"]
        assert "Bogan287" not in out["text"]
        assert "[PATIENT]" in out["text"]
        assert "Janiece99" not in out["summary_snippet"]


class TestMaskSummary:
    def _summary(self):
        return {
            "patient_mrn": "MRN-C05BB077",
            "chief_concern": "Hyperlipidemia",
            "key_diagnoses": ["Hyperlipidemia"],
            "recent_labs": ["HbA1c: 6.2 %"],
            "recent_imaging": [],
            "flagged_anomalies": ["elevated HbA1c (6.2 %)"],
            "summary": "57-year-old female Janiece99 Bogan287 with hyperlipidemia",
        }

    def test_disabled_only_adds_display(self):
        out = mask_summary(self._summary(), "Janiece99 Bogan287", enabled=False)
        assert out["patient_mrn"] == "MRN-C05BB077"
        assert out["patient_mrn_display"] == "MRN-C05BB077"
        assert "Janiece99" in out["summary"]  # not redacted

    def test_enabled_redacts_narrative_and_masks_display_mrn(self):
        out = mask_summary(self._summary(), "Janiece99 Bogan287", enabled=True)
        assert out["patient_mrn"] == "MRN-C05BB077"
        assert out["patient_mrn_display"] == "MRN-****B077"
        assert "Janiece99" not in out["summary"]
        assert "[PATIENT]" in out["summary"]


class TestMaskBundle:
    def _bundle(self):
        # Encode a progress note that mentions the patient by name
        note = "PROGRESS NOTE\nPatient: Janiece99 Bogan287\nSubjective: Follow-up"
        note_b64 = base64.b64encode(note.encode()).decode()
        return {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "identifier": [{"value": "MRN-C05BB077"}],
                        "name": [{"given": ["Janiece99"], "family": "Bogan287"}],
                        "birthDate": "1969-03-15",
                        "gender": "female",
                    }
                },
                {
                    "resource": {
                        "resourceType": "DocumentReference",
                        "type": {"text": "Progress note"},
                        "content": [{"attachment": {"data": note_b64}}],
                    }
                },
                {
                    "resource": {
                        "resourceType": "DiagnosticReport",
                        "code": {"text": "HbA1c"},
                        "conclusion": "HbA1c: 6.2 % (Janiece99 Bogan287)",
                    }
                },
            ],
        }

    def test_disabled_returns_unchanged(self):
        b = self._bundle()
        out = mask_bundle(b, enabled=False)
        assert out is b  # same object

    def test_masks_patient_resource(self):
        out = mask_bundle(self._bundle(), enabled=True)
        patient = out["entry"][0]["resource"]
        # Name becomes the masked display form, family empty
        assert patient["name"][0]["given"] == ["Patient B077"]
        assert patient["name"][0]["family"] == ""
        # MRN masked
        assert patient["identifier"][0]["value"] == "MRN-****B077"
        # DOB year only
        assert patient["birthDate"] == "1969"

    def test_masks_document_note_content(self):
        out = mask_bundle(self._bundle(), enabled=True)
        doc = out["entry"][1]["resource"]
        decoded = base64.b64decode(doc["content"][0]["attachment"]["data"]).decode()
        assert "Janiece99" not in decoded
        assert "Bogan287" not in decoded
        assert "[PATIENT]" in decoded

    def test_masks_diagnosticreport_conclusion(self):
        out = mask_bundle(self._bundle(), enabled=True)
        dr = out["entry"][2]["resource"]
        assert "Janiece99" not in dr["conclusion"]
        assert "Bogan287" not in dr["conclusion"]


class TestMaskRecord:
    def test_disabled_passthrough(self):
        r = {"id": "x", "text": "Janiece99 note"}
        assert mask_record(r, "Janiece99 Bogan287", enabled=False) == r

    def test_enabled_redacts_text(self):
        out = mask_record({"id": "x", "text": "Janiece99 note"}, "Janiece99 Bogan287", enabled=True)
        assert "Janiece99" not in out["text"]
