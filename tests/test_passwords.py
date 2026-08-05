"""
Unit tests for password hashing and verification.
"""

import pytest

from app.review.passwords import hash_password, verify_password


class TestPasswordRoundTrip:
    def test_hash_verify_correct(self):
        h = hash_password("hunter2")
        assert verify_password("hunter2", h) is True

    def test_hash_reject_wrong(self):
        h = hash_password("hunter2")
        assert verify_password("hunter3", h) is False

    def test_hash_case_sensitive(self):
        h = hash_password("Reviewer2026")
        assert verify_password("reviewer2026", h) is False
        assert verify_password("Reviewer2026", h) is True

    def test_hash_unicode_password(self):
        h = hash_password("护士李2026")
        assert verify_password("护士李2026", h) is True
        assert verify_password("护士李2025", h) is False


class TestHashFormat:
    def test_hash_uses_pbkdf2_sha256(self):
        h = hash_password("pw")
        assert h.startswith("pbkdf2_sha256$")

    def test_hash_encodes_iterations_salt_and_digest(self):
        h = hash_password("pw")
        parts = h.split("$")
        assert len(parts) == 4
        algo, iters, salt_hex, dk_hex = parts
        assert algo == "pbkdf2_sha256"
        assert int(iters) >= 100_000  # security floor
        assert len(salt_hex) == 32  # 16 bytes hex
        assert len(dk_hex) == 64    # 32 bytes hex (SHA-256)

    def test_hash_is_salted(self):
        """Same password should produce different hashes each call."""
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2
        # But both still verify
        assert verify_password("same-password", h1)
        assert verify_password("same-password", h2)


class TestVerifyRejectsMalformed:
    def test_reject_empty_string(self):
        assert verify_password("pw", "") is False

    def test_reject_garbage(self):
        assert verify_password("pw", "not-a-hash") is False

    def test_reject_wrong_algo(self):
        assert verify_password("pw", "bcrypt$12$deadbeef") is False

    def test_reject_bad_iteration_count(self):
        assert verify_password("pw", "pbkdf2_sha256$abc$deadbeef$cafebabe") is False

    def test_reject_missing_parts(self):
        assert verify_password("pw", "pbkdf2_sha256$100000") is False
