"""
Tests for the cross_vendor module — content-hash cache + source extraction.
Does NOT call the OpenAI API (no key, no cost).
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from app.review import cross_vendor


class TestContentHash:
    def test_same_inputs_same_hash(self):
        h1 = cross_vendor._content_hash("src", {"a": 1}, "gpt-4o-mini")
        h2 = cross_vendor._content_hash("src", {"a": 1}, "gpt-4o-mini")
        assert h1 == h2

    def test_summary_key_order_stable(self):
        h1 = cross_vendor._content_hash("src", {"a": 1, "b": 2}, "gpt-4o-mini")
        h2 = cross_vendor._content_hash("src", {"b": 2, "a": 1}, "gpt-4o-mini")
        assert h1 == h2

    def test_different_source_changes_hash(self):
        h1 = cross_vendor._content_hash("src1", {"a": 1}, "gpt-4o-mini")
        h2 = cross_vendor._content_hash("src2", {"a": 1}, "gpt-4o-mini")
        assert h1 != h2

    def test_different_model_changes_hash(self):
        h1 = cross_vendor._content_hash("src", {"a": 1}, "gpt-4o-mini")
        h2 = cross_vendor._content_hash("src", {"a": 1}, "gpt-4o")
        assert h1 != h2


class TestExtractSourceText:
    def test_extracts_patient_lab_and_document(self):
        note = "Progress note body"
        bundle_json = json.dumps({
            "resourceType": "Bundle",
            "entry": [
                {"resource": {
                    "resourceType": "Patient",
                    "name": [{"given": ["Janiece99"], "family": "Bogan287"}],
                    "gender": "female", "birthDate": "1969-03-15",
                }},
                {"resource": {
                    "resourceType": "DiagnosticReport",
                    "code": {"text": "HbA1c"},
                    "conclusion": "HbA1c: 6.2 %",
                    "effectiveDateTime": "2025-06-15",
                }},
                {"resource": {
                    "resourceType": "DocumentReference",
                    "type": {"text": "Progress note"},
                    "date": "2025-06-16",
                    "content": [{"attachment": {"data": base64.b64encode(note.encode()).decode()}}],
                }},
            ],
        })
        text = cross_vendor.extract_source_text(bundle_json)
        assert "Janiece99 Bogan287" in text
        assert "HbA1c: 6.2 %" in text
        assert "Progress note body" in text

    def test_handles_empty_bundle(self):
        text = cross_vendor.extract_source_text('{"resourceType": "Bundle"}')
        assert text == ""


class TestCacheHit:
    """verify_one should return the cached verdict without touching the API."""

    def test_cache_hit_skips_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cross_vendor, "CACHE_PATH", tmp_path / "cache.json")

        source = "PATIENT: Test"
        summary = {"summary": "hi"}
        key = cross_vendor._content_hash(source, summary, cross_vendor.DEFAULT_MODEL)
        cached_verdict = {
            "patient_mrn": "MRN-TEST",
            "verdict": "clean",
            "hallucinations": [],
            "value_mismatches": [],
            "grounded_claims_count": 5,
            "summary_narrative_ok": True,
            "reviewer_recommendation": "approve",
        }
        cross_vendor._save_cache({key: cached_verdict})

        # OpenAI import path is inside verify_one; if we hit the cache we
        # never reach the OpenAI() constructor, so we don't need to mock it.
        result = cross_vendor.verify_one(
            "MRN-TEST", source, summary,
            model=cross_vendor.DEFAULT_MODEL,
            force=False,
        )
        assert result == cached_verdict

    def test_force_flag_bypasses_cache(self, tmp_path, monkeypatch):
        """When force=True, we hit the API even with a cached verdict.
        Assert by expecting a RuntimeError because OPENAI_API_KEY is unset."""
        monkeypatch.setattr(cross_vendor, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        source = "PATIENT: Test"
        summary = {"summary": "hi"}
        key = cross_vendor._content_hash(source, summary, cross_vendor.DEFAULT_MODEL)
        cross_vendor._save_cache({key: {"cached": True}})

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            cross_vendor.verify_one(
                "MRN-TEST", source, summary,
                model=cross_vendor.DEFAULT_MODEL,
                force=True,
            )


class TestVerifyOneWithMockedAPI:
    """Mock the OpenAI client to prove the API call + parse pipeline works
    without spending money."""

    def test_parses_json_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cross_vendor, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

        fake_verdict = {
            "patient_mrn": "MRN-X",
            "verdict": "partially_hallucinated",
            "hallucinations": [{"field": "recent_labs", "claim": "BMI 30", "reason": "no BMI"}],
            "value_mismatches": [],
            "grounded_claims_count": 4,
            "summary_narrative_ok": False,
            "reviewer_recommendation": "edit",
        }

        fake_choice = MagicMock()
        fake_choice.message.content = json.dumps(fake_verdict)
        fake_response = MagicMock(choices=[fake_choice])
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch("openai.OpenAI", return_value=fake_client):
            result = cross_vendor.verify_one(
                "MRN-X", "PATIENT: X", {"summary": "s"},
                model="gpt-4o-mini", force=True,
            )

        assert result["verdict"] == "partially_hallucinated"
        assert result["reviewer_recommendation"] == "edit"
        assert result["patient_mrn"] == "MRN-X"
        # Cache was written
        cache = cross_vendor._load_cache()
        assert len(cache) == 1

    def test_handles_malformed_json_from_model(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cross_vendor, "CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

        fake_choice = MagicMock()
        fake_choice.message.content = "not valid json {{"
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(choices=[fake_choice])

        with patch("openai.OpenAI", return_value=fake_client):
            result = cross_vendor.verify_one(
                "MRN-Y", "PATIENT: Y", {"summary": "s"},
                model="gpt-4o-mini", force=True,
            )

        assert result["verdict"] == "parse_error"
        assert result["reviewer_recommendation"] == "reject"
        assert "raw_response" in result
