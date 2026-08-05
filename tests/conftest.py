"""
Shared pytest fixtures.

Because the FastAPI app now enforces a session token on all PHI-touching
endpoints, the pre-existing integration tests (test_api.py, test_fhir.py)
would otherwise all 401. This module logs in as a seeded reviewer once per
test session and attaches the token as a default header on the shared
TestClient, so those tests continue to pass without per-test edits.

Tests that specifically want to exercise the unauthenticated path (see
test_auth_routes.py::TestSessionRequired) use their own TestClient without
this default header, or explicitly override the header.
"""

import pytest

from app.api.routes import app
from app.review.passwords import hash_password
from app.review.store import ReviewStore


@pytest.fixture(scope="session", autouse=True)
def _default_session_header():
    """Seed a reviewer and attach the session token to the shared TestClient
    used by test_api.py and test_fhir.py."""
    # Seed a dedicated bot user with a known password
    username = "pytest-bot"
    password = "PytestBotPassword-2026"
    store = ReviewStore()
    try:
        store.upsert_user(username, hash_password(password), "reviewer")
    finally:
        store.close()

    # Import the module-level client used by test_api.py + friends
    from tests import test_api as api_mod

    # Log in and set default header on the shared client
    login = api_mod.client.post(
        "/session/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    api_mod.client.headers.update({"X-Session-Token": token})

    yield

    # Best-effort logout
    try:
        api_mod.client.post("/session/logout")
    except Exception:
        pass
