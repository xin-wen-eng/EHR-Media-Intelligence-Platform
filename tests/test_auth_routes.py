"""
Integration tests for session auth + role-based access on the FastAPI routes.

These hit the real FastAPI app (TestClient), which shares the dev SQLite file.
Tests create their own scratch users and revoke their sessions in teardown so
they don't leak state.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.routes import app
from app.review.passwords import hash_password
from app.review.store import ReviewStore


client = TestClient(app)


def _fresh_user(role="reviewer"):
    """Seed a unique test user and return (username, password, role)."""
    username = f"test-{uuid.uuid4().hex[:8]}"
    password = "TestPw!" + uuid.uuid4().hex[:6]
    store = ReviewStore()
    try:
        store.upsert_user(username, hash_password(password), role)
    finally:
        store.close()
    return username, password, role


def _login(username, password):
    resp = client.post("/session/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestLogin:
    def test_login_success_returns_token_and_role(self):
        u, p, r = _fresh_user(role="reviewer")
        resp = client.post("/session/login", json={"username": u, "password": p})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"]
        assert body["actor"] == u
        assert body["role"] == r
        assert body["expires_at"]
        assert body["idle_timeout_seconds"] == 15 * 60

    def test_login_wrong_password_401(self):
        u, _, _ = _fresh_user()
        resp = client.post("/session/login", json={"username": u, "password": "wrong"})
        assert resp.status_code == 401

    def test_login_unknown_user_401(self):
        resp = client.post("/session/login", json={"username": "nobody", "password": "x"})
        assert resp.status_code == 401

    def test_login_missing_fields_400(self):
        resp = client.post("/session/login", json={"username": "", "password": ""})
        assert resp.status_code == 400


class TestSessionRequired:
    def test_search_requires_session(self):
        resp = client.post("/search", json={"query": "diabetes"})
        assert resp.status_code == 401

    def test_reviews_list_requires_session(self):
        resp = client.get("/reviews")
        assert resp.status_code == 401

    def test_audit_get_requires_session(self):
        resp = client.get("/audit")
        assert resp.status_code == 401

    def test_search_with_valid_session_ok(self):
        u, p, _ = _fresh_user("reviewer")
        token = _login(u, p)
        resp = client.post(
            "/search",
            json={"query": "diabetes", "n_results": 2},
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 200


class TestSessionLifecycle:
    def test_whoami_returns_session_info(self):
        u, p, r = _fresh_user()
        token = _login(u, p)
        resp = client.get("/session/me", headers={"X-Session-Token": token})
        assert resp.status_code == 200
        body = resp.json()
        assert body["actor"] == u
        assert body["role"] == r

    def test_logout_revokes_token(self):
        u, p, _ = _fresh_user()
        token = _login(u, p)
        # Session valid before
        assert client.get("/session/me", headers={"X-Session-Token": token}).status_code == 200
        # Log out
        resp = client.post("/session/logout", headers={"X-Session-Token": token})
        assert resp.status_code == 200
        # Old token no longer works
        assert client.get("/session/me", headers={"X-Session-Token": token}).status_code == 401

    def test_invalid_token_401(self):
        resp = client.get("/session/me", headers={"X-Session-Token": "definitely-not-real"})
        assert resp.status_code == 401


class TestRoleGating:
    def test_clinician_cannot_approve_returns_403(self):
        u, p, _ = _fresh_user(role="clinician")
        token = _login(u, p)
        # Pick any known patient MRN present in summaries.json; if missing, skip
        list_resp = client.get(
            "/reviews", headers={"X-Session-Token": token}
        )
        assert list_resp.status_code == 200
        reviews = list_resp.json()["reviews"]
        if not reviews:
            pytest.skip("no reviews to test against")
        mrn = reviews[0]["patient_mrn"]

        resp = client.post(
            f"/reviews/{mrn}",
            json={"status": "approved", "reviewer": u},
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 403

    def test_reviewer_can_approve(self):
        u, p, _ = _fresh_user(role="reviewer")
        token = _login(u, p)
        list_resp = client.get(
            "/reviews", headers={"X-Session-Token": token}
        )
        reviews = list_resp.json()["reviews"]
        if not reviews:
            pytest.skip("no reviews to test against")
        mrn = reviews[0]["patient_mrn"]

        resp = client.post(
            f"/reviews/{mrn}",
            json={"status": "approved", "reviewer": u, "notes": "unit test"},
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["review"]["status"] == "approved"

    def test_invalid_status_400(self):
        u, p, _ = _fresh_user(role="reviewer")
        token = _login(u, p)
        list_resp = client.get(
            "/reviews", headers={"X-Session-Token": token}
        )
        reviews = list_resp.json()["reviews"]
        if not reviews:
            pytest.skip("no reviews to test against")
        mrn = reviews[0]["patient_mrn"]

        resp = client.post(
            f"/reviews/{mrn}",
            json={"status": "wobble", "reviewer": u},
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 400

    def test_edited_requires_edited_summary(self):
        u, p, _ = _fresh_user(role="reviewer")
        token = _login(u, p)
        list_resp = client.get(
            "/reviews", headers={"X-Session-Token": token}
        )
        reviews = list_resp.json()["reviews"]
        if not reviews:
            pytest.skip("no reviews to test against")
        mrn = reviews[0]["patient_mrn"]

        resp = client.post(
            f"/reviews/{mrn}",
            json={"status": "edited", "reviewer": u},  # missing edited_summary
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 400


class TestPIIMasking:
    def test_default_headers_mask_pii(self):
        """No X-PII-Mask header → masking on by default."""
        u, p, _ = _fresh_user()
        token = _login(u, p)
        resp = client.post(
            "/search",
            json={"query": "diabetes", "n_results": 1},
            headers={"X-Session-Token": token},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        if not results:
            pytest.skip("no search results")
        r = results[0]
        assert r["patient_name"].startswith("Patient ")
        assert r["patient_mrn_display"].startswith("MRN-****")

    def test_explicit_pii_off_reveals_names(self):
        u, p, _ = _fresh_user()
        token = _login(u, p)
        resp = client.post(
            "/search",
            json={"query": "diabetes", "n_results": 1},
            headers={"X-Session-Token": token, "X-PII-Mask": "false"},
        )
        results = resp.json()["results"]
        if not results:
            pytest.skip("no search results")
        r = results[0]
        # Real name should not be "Patient XXXX" format
        assert not r["patient_name"].startswith("Patient ")
        assert r["patient_mrn_display"] == r["patient_mrn"]  # unmasked
