"""
Integration tests for FastAPI endpoints and search quality.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app


client = TestClient(app)


class TestSearchEndpoint:
    def test_search_returns_results(self):
        resp = client.post("/search", json={"query": "heart failure", "n_results": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "heart failure"
        assert len(data["results"]) > 0
        assert data["total"] > 0
        assert data["elapsed_ms"] > 0

    def test_search_result_fields(self):
        resp = client.post("/search", json={"query": "blood pressure", "n_results": 3})
        data = resp.json()
        result = data["results"][0]
        assert "id" in result
        assert "text" in result
        assert "patient_mrn" in result
        assert "patient_name" in result
        assert "resource_type" in result
        assert "date" in result
        assert "relevance_score" in result
        assert "summary_snippet" in result
        assert 0 <= result["relevance_score"] <= 1

    def test_search_respects_n_results(self):
        resp = client.post("/search", json={"query": "glucose", "n_results": 3})
        data = resp.json()
        assert len(data["results"]) <= 3

    def test_search_filter_by_resource_type(self):
        resp = client.post("/search", json={
            "query": "patient",
            "n_results": 5,
            "resource_type": "AISummary",
        })
        data = resp.json()
        for result in data["results"]:
            assert result["resource_type"] == "AISummary"

    def test_search_filter_by_date_range(self):
        resp = client.post("/search", json={
            "query": "lab results",
            "n_results": 10,
            "date_from": "2025-01-01",
            "date_to": "2026-12-31",
        })
        data = resp.json()
        for result in data["results"]:
            if result["date"]:
                assert result["date"] >= "2025-01-01"
                assert result["date"] <= "2026-12-31"

    def test_search_empty_query(self):
        resp = client.post("/search", json={"query": "", "n_results": 5})
        assert resp.status_code == 200

    def test_search_under_two_seconds(self):
        resp = client.post("/search", json={"query": "diabetes medication", "n_results": 5})
        data = resp.json()
        assert data["elapsed_ms"] < 2000

    def test_search_per_patient_dedup(self):
        resp = client.post("/search", json={"query": "glucose", "n_results": 10})
        data = resp.json()
        mrns = [r["patient_mrn"] for r in data["results"]]
        assert len(mrns) == len(set(mrns)), "Duplicate patients in results"


class TestPatientEndpoints:
    def test_list_patients(self):
        resp = client.get("/patients")
        assert resp.status_code == 200
        data = resp.json()
        assert "patients" in data
        assert data["total"] > 0

    def test_get_patient_summary(self):
        patients = client.get("/patients").json()["patients"]
        mrn = patients[0]["patient_mrn"]
        resp = client.get(f"/patients/{mrn}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "chief_concern" in data
        assert "key_diagnoses" in data
        assert "recent_labs" in data
        assert "flagged_anomalies" in data
        assert "summary" in data
        assert "disclaimer" in data

    def test_get_patient_bundle(self):
        patients = client.get("/patients").json()["patients"]
        mrn = patients[0]["patient_mrn"]
        resp = client.get(f"/patients/{mrn}/bundle")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resourceType"] == "Bundle"
        assert len(data["entry"]) > 0

    def test_get_patient_records(self):
        patients = client.get("/patients").json()["patients"]
        mrn = patients[0]["patient_mrn"]
        resp = client.get(f"/patients/{mrn}/records?query=lab")
        assert resp.status_code == 200
        data = resp.json()
        assert "records" in data

    def test_summary_not_found(self):
        resp = client.get("/patients/FAKE-MRN/summary")
        data = resp.json()
        assert "error" in data

    def test_bundle_not_found(self):
        resp = client.get("/patients/FAKE-MRN/bundle")
        data = resp.json()
        assert "error" in data


class TestSearchQuality:
    def test_heart_failure_finds_cardiac_patients(self):
        resp = client.post("/search", json={"query": "heart failure", "n_results": 5})
        data = resp.json()
        assert len(data["results"]) > 0
        texts = " ".join(r["text"].lower() + " " + r.get("summary_snippet", "").lower()
                         for r in data["results"])
        assert any(w in texts for w in ["heart", "cardiac", "congestive"]), \
            "Heart failure query should return cardiac-related results"

    def test_diabetes_finds_glucose_results(self):
        resp = client.post("/search", json={"query": "diabetes", "n_results": 5})
        data = resp.json()
        assert len(data["results"]) > 0
        texts = " ".join(r["text"].lower() for r in data["results"])
        assert any(w in texts for w in ["glucose", "diabetes", "a1c", "blood"]), \
            "Diabetes query should return glucose-related results"

    def test_results_sorted_by_relevance(self):
        resp = client.post("/search", json={"query": "chest pain", "n_results": 5})
        data = resp.json()
        scores = [r["relevance_score"] for r in data["results"]]
        assert scores == sorted(scores, reverse=True), \
            "Results should be sorted by descending relevance score"

    def test_summary_snippet_populated(self):
        resp = client.post("/search", json={"query": "patient care", "n_results": 5})
        data = resp.json()
        snippets = [r["summary_snippet"] for r in data["results"] if r["summary_snippet"]]
        assert len(snippets) > 0, "At least some results should have AI summary snippets"
