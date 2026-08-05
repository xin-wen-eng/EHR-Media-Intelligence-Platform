"""
Unit tests for ReviewStore: reviews, audit log, sessions, users.
Uses a tmp_path-scoped SQLite file per test to stay isolated.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.review.store import (
    STATUS_APPROVED,
    STATUS_EDITED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ReviewStore,
    content_hash,
)


@pytest.fixture
def store(tmp_path):
    s = ReviewStore(db_path=tmp_path / "test.db")
    yield s
    s.close()


class TestContentHash:
    def test_hash_dict_stable(self):
        a = {"foo": 1, "bar": 2}
        b = {"bar": 2, "foo": 1}  # key order differs
        assert content_hash(a) == content_hash(b)

    def test_hash_str(self):
        assert content_hash("hello") == content_hash("hello")
        assert content_hash("hello") != content_hash("world")

    def test_hash_length(self):
        assert len(content_hash("x")) == 16


class TestReviewQueue:
    def test_ensure_pending_creates_row(self, store):
        store.ensure_pending("MRN-TEST", {"summary": "hi"})
        r = store.get_review("MRN-TEST")
        assert r is not None
        assert r["status"] == STATUS_PENDING
        assert r["original_hash"]

    def test_ensure_pending_idempotent(self, store):
        store.ensure_pending("MRN-TEST", {"summary": "hi"})
        first = store.get_review("MRN-TEST")
        store.ensure_pending("MRN-TEST", {"summary": "changed"})
        second = store.get_review("MRN-TEST")
        # Original hash preserved (does not overwrite)
        assert first["original_hash"] == second["original_hash"]
        assert second["status"] == STATUS_PENDING

    def test_set_status_approved(self, store):
        store.ensure_pending("MRN-A", {"summary": "hi"})
        result = store.set_status("MRN-A", STATUS_APPROVED, reviewer="dr.smith")
        assert result["status"] == STATUS_APPROVED
        assert result["reviewer"] == "dr.smith"
        assert result["reviewed_at"] is not None

    def test_set_status_edited_stores_edited_summary(self, store):
        store.ensure_pending("MRN-B", {"summary": "orig"})
        result = store.set_status(
            "MRN-B", STATUS_EDITED, reviewer="dr.smith",
            edited_summary="clinician-edited version",
        )
        assert result["edited_summary"] == "clinician-edited version"

    def test_set_status_rejects_invalid(self, store):
        store.ensure_pending("MRN-C", {"summary": "x"})
        with pytest.raises(ValueError):
            store.set_status("MRN-C", "wobbly", reviewer="x")

    def test_list_reviews_filter_by_status(self, store):
        store.ensure_pending("MRN-1", {"summary": "a"})
        store.ensure_pending("MRN-2", {"summary": "b"})
        store.set_status("MRN-1", STATUS_APPROVED, reviewer="x")

        pending = store.list_reviews(status=STATUS_PENDING)
        approved = store.list_reviews(status=STATUS_APPROVED)
        assert {r["patient_mrn"] for r in pending} == {"MRN-2"}
        assert {r["patient_mrn"] for r in approved} == {"MRN-1"}

    def test_list_reviews_all(self, store):
        store.ensure_pending("MRN-1", {"summary": "a"})
        store.ensure_pending("MRN-2", {"summary": "b"})
        all_r = store.list_reviews()
        assert len(all_r) == 2


class TestAuditLog:
    def test_log_and_list(self, store):
        store.log(actor="dr.wen", role="reviewer", action="login")
        entries = store.list_audit(limit=10)
        assert len(entries) == 1
        assert entries[0]["actor"] == "dr.wen"
        assert entries[0]["action"] == "login"

    def test_audit_ordered_newest_first(self, store):
        store.log(actor="a", role="reviewer", action="login")
        time.sleep(0.01)
        store.log(actor="b", role="clinician", action="view_summary")
        entries = store.list_audit(limit=10)
        # Newest first
        assert entries[0]["actor"] == "b"
        assert entries[1]["actor"] == "a"

    def test_audit_filter_by_patient(self, store):
        store.log(actor="a", role="r", action="view", patient_mrn="MRN-1")
        store.log(actor="a", role="r", action="view", patient_mrn="MRN-2")
        m1 = store.list_audit(patient_mrn="MRN-1")
        assert len(m1) == 1
        assert m1[0]["patient_mrn"] == "MRN-1"

    def test_audit_captures_content_hash_and_notes(self, store):
        h = content_hash({"summary": "s"})
        store.log(
            actor="dr.wen",
            role="reviewer",
            action="review_approved",
            patient_mrn="MRN-X",
            content_hash_value=h,
            notes="clinically consistent",
        )
        e = store.list_audit()[0]
        assert e["content_hash"] == h
        assert e["notes"] == "clinically consistent"
        assert e["role"] == "reviewer"


class TestUsers:
    def test_upsert_and_get_user(self, store):
        store.upsert_user("dr.wen", "pbkdf2_sha256$100000$aa$bb", "reviewer")
        u = store.get_user("dr.wen")
        assert u["username"] == "dr.wen"
        assert u["role"] == "reviewer"
        assert u["password_hash"].startswith("pbkdf2_sha256$")

    def test_get_missing_user(self, store):
        assert store.get_user("ghost") is None

    def test_upsert_updates_existing(self, store):
        store.upsert_user("dr.wen", "old-hash", "reviewer")
        store.upsert_user("dr.wen", "new-hash", "reviewer")
        u = store.get_user("dr.wen")
        assert u["password_hash"] == "new-hash"

    def test_list_users_alphabetical(self, store):
        store.upsert_user("dr.wen", "h", "reviewer")
        store.upsert_user("dr.chen", "h", "reviewer")
        store.upsert_user("nurse.li", "h", "clinician")
        usernames = [u["username"] for u in store.list_users()]
        assert usernames == sorted(usernames)


class TestSessions:
    def test_create_and_get(self, store):
        s = store.create_session("dr.wen", "reviewer", idle_timeout_seconds=60)
        assert s["token"]
        assert s["actor"] == "dr.wen"
        assert s["role"] == "reviewer"
        got = store.get_session(s["token"])
        assert got is not None

    def test_touch_refreshes_expiry(self, store):
        s = store.create_session("dr.wen", "reviewer", idle_timeout_seconds=60)
        first_expiry = s["expires_at"]
        time.sleep(1.1)  # ensure timestamp resolution ticks
        refreshed = store.touch_session(s["token"], idle_timeout_seconds=60)
        assert refreshed is not None
        assert refreshed["expires_at"] > first_expiry

    def test_touch_returns_none_when_expired(self, store):
        # Create a session then sleep past the 1-second timeout window.
        # Sleep for >2s so the second-precision expires_at is definitely in
        # the past regardless of sub-second offset at creation time.
        s = store.create_session("dr.wen", "reviewer", idle_timeout_seconds=1)
        time.sleep(2.5)
        touched = store.touch_session(s["token"], idle_timeout_seconds=1)
        assert touched is None
        # Follow-up get should also return None (revoked)
        assert store.get_session(s["token"]) is None

    def test_revoke_makes_session_invalid(self, store):
        s = store.create_session("dr.wen", "reviewer", idle_timeout_seconds=60)
        store.revoke_session(s["token"])
        assert store.get_session(s["token"]) is None

    def test_revoke_idempotent(self, store):
        s = store.create_session("dr.wen", "reviewer", idle_timeout_seconds=60)
        store.revoke_session(s["token"])
        store.revoke_session(s["token"])  # no-op, no exception
        assert store.get_session(s["token"]) is None
