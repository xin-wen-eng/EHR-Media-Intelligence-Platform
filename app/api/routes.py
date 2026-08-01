"""
FastAPI application with search endpoint.
"""

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.search.embedder import get_model, get_chroma_client, search
from app.db.store import FHIRStore
from app.summarizer.cache import SummaryCache
from app.review.store import (
    ReviewStore,
    STATUS_APPROVED,
    STATUS_EDITED,
    STATUS_REJECTED,
    VALID_STATUSES,
    content_hash,
)
from app.review.passwords import hash_password, verify_password
from app.review.pii import (
    mask_bundle,
    mask_record,
    mask_search_hit,
    mask_summary,
)
from app.review.verify import verify_summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
    get_chroma_client()
    _seed_pending_reviews()
    _seed_demo_users()
    yield


app = FastAPI(
    title="Clinical Data Intelligence Platform",
    description="AI-powered clinical record search and summarization",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/session/login")
def login(req: LoginRequest):
    username = (req.username or "").strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="username and password required")
    store = ReviewStore()
    try:
        user = store.get_user(username)
        if not user or not verify_password(req.password, user["password_hash"]):
            store.log(actor=username or "unknown", role="unknown", action="login_failed", notes="invalid credentials")
            raise HTTPException(status_code=401, detail="Invalid username or password")
        session = store.create_session(username, user["role"], IDLE_TIMEOUT_SECONDS)
        store.log(actor=username, role=user["role"], action="login", notes=f"session created; idle_timeout={IDLE_TIMEOUT_SECONDS}s")
    finally:
        store.close()
    return {
        "token": session["token"],
        "actor": session["actor"],
        "role": session["role"],
        "expires_at": session["expires_at"],
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
    }


@app.post("/session/logout")
def logout(x_session_token: str | None = Header(default=None)):
    if not x_session_token:
        return {"ok": True}
    store = ReviewStore()
    try:
        session = store.get_session(x_session_token)
        store.revoke_session(x_session_token, reason="logout")
        if session:
            store.log(actor=session["actor"], role=session["role"], action="logout", notes="manual logout")
    finally:
        store.close()
    return {"ok": True}


@app.get("/session/me")
def whoami(x_session_token: str | None = Header(default=None)):
    """Return current session (refreshes activity). 401 if invalid/expired."""
    session = require_session(x_session_token)
    return {
        "actor": session["actor"],
        "role": session["role"],
        "expires_at": session["expires_at"],
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
    }


class SearchRequest(BaseModel):
    query: str
    n_results: int = 5
    resource_type: str | None = None
    date_from: str | None = None
    date_to: str | None = None


class SearchResult(BaseModel):
    id: str
    text: str
    patient_mrn: str
    patient_mrn_display: str = ""
    patient_name: str = ""
    resource_type: str
    match_count: int = 1
    date: str
    relevance_score: float
    summary_snippet: str = ""
    review_status: str = "pending"
    cross_vendor_verdict: str | None = None
    cross_vendor_hallucination_count: int = 0
    cross_vendor_recommendation: str | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int
    elapsed_ms: float


SUMMARIES_PATH = Path(__file__).parent.parent.parent / "data" / "summaries.json"
VERIFICATIONS_PATH = Path(__file__).parent.parent.parent / "data" / "verifications.json"


def _load_verifications() -> dict:
    if not VERIFICATIONS_PATH.exists():
        return {"meta": {}, "results": []}
    with open(VERIFICATIONS_PATH) as f:
        return json.load(f)


def _verification_for(mrn: str) -> dict | None:
    for entry in _load_verifications().get("results", []):
        if entry.get("patient_mrn") == mrn:
            return entry
    return None


def _load_all_summaries() -> list[dict]:
    if not SUMMARIES_PATH.exists():
        return []
    with open(SUMMARIES_PATH) as f:
        return json.load(f)


def _load_summaries() -> dict[str, str]:
    return {s["patient_mrn"]: s.get("summary", "") for s in _load_all_summaries()}


def _get_summary(mrn: str) -> dict | None:
    for s in _load_all_summaries():
        if s["patient_mrn"] == mrn:
            return s
    return None


def _seed_pending_reviews() -> None:
    """On startup, make sure every cached summary has a review row."""
    store = ReviewStore()
    try:
        for s in _load_all_summaries():
            store.ensure_pending(s["patient_mrn"], s)
    finally:
        store.close()


# Demo credentials — seeded on startup if the users table is empty.
# In production these would come from IdP/SSO. Passwords chosen to be memorable
# for the demo, not for security beyond hackathon scope.
DEMO_USERS = [
    ("dr.wen", "reviewer2026", "reviewer"),
    ("dr.chen", "reviewer2026", "reviewer"),
    ("nurse.li", "clinician2026", "clinician"),
]


def _seed_demo_users() -> None:
    store = ReviewStore()
    try:
        existing = {u["username"] for u in store.list_users()}
        for username, password, role in DEMO_USERS:
            if username not in existing:
                store.upsert_user(username, hash_password(password), role)
    finally:
        store.close()


IDLE_TIMEOUT_SECONDS = 15 * 60  # HIPAA-aligned 15-minute idle timeout


def _actor(x_actor: str | None, x_role: str | None) -> tuple[str, str]:
    """Reviewer identity from headers. Defaults to anonymous clinician."""
    return (x_actor or "anonymous", x_role or "clinician")


def require_session(x_session_token: str | None = Header(default=None)) -> dict:
    """Dependency: validate + refresh session or raise 401."""
    if not x_session_token:
        raise HTTPException(status_code=401, detail="Session required. Please log in.")
    store = ReviewStore()
    try:
        session = store.touch_session(x_session_token, IDLE_TIMEOUT_SECONDS)
        if not session:
            raise HTTPException(status_code=401, detail="Session expired or invalid. Please log in again.")
        return session
    finally:
        store.close()


def _pii_enabled(x_pii_mask: str | None) -> bool:
    """PII masking header. Defaults to True (safer default)."""
    if x_pii_mask is None:
        return True
    return x_pii_mask.lower() not in {"false", "0", "off", "no"}


def _real_name_for(mrn: str) -> str:
    """Look up the patient's real name from the FHIR bundle (for redaction)."""
    store = FHIRStore()
    try:
        bundle_json = store.get_bundle_json(mrn)
    finally:
        store.close()
    if not bundle_json:
        return ""
    try:
        bundle = json.loads(bundle_json)
    except json.JSONDecodeError:
        return ""
    for entry in bundle.get("entry", []) or []:
        r = entry.get("resource") or {}
        if r.get("resourceType") == "Patient":
            for nm in r.get("name", []) or []:
                given = " ".join(nm.get("given", []) or [])
                family = nm.get("family") or ""
                return f"{given} {family}".strip()
    return ""


@app.post("/search", response_model=SearchResponse)
def search_records(
    request: SearchRequest,
    x_pii_mask: str | None = Header(default=None),
    session: dict = Depends(require_session),
):
    start = time.time()

    hits = search(
        query=request.query,
        n_results=request.n_results,
        resource_type=request.resource_type,
        date_from=request.date_from,
        date_to=request.date_to,
    )

    summaries = _load_summaries()
    store = ReviewStore()
    try:
        reviews = {r["patient_mrn"]: r["status"] for r in store.list_reviews()}
    finally:
        store.close()
    verifications = {v["patient_mrn"]: v for v in _load_verifications().get("results", [])}
    for h in hits:
        h["summary_snippet"] = summaries.get(h["patient_mrn"], "")
        h["review_status"] = reviews.get(h["patient_mrn"], "pending")
        v = verifications.get(h["patient_mrn"])
        if v:
            h["cross_vendor_verdict"] = v.get("verdict")
            h["cross_vendor_hallucination_count"] = len(v.get("hallucinations", []))
            h["cross_vendor_recommendation"] = v.get("reviewer_recommendation")

    pii = _pii_enabled(x_pii_mask)
    hits = [mask_search_hit(h, pii) for h in hits]

    elapsed = (time.time() - start) * 1000

    return SearchResponse(
        query=request.query,
        results=[SearchResult(**h) for h in hits],
        total=len(hits),
        elapsed_ms=round(elapsed, 1),
    )


@app.get("/patients")
def list_patients(session: dict = Depends(require_session)):
    store = FHIRStore()
    patients = store.list_patients()
    store.close()
    return {"patients": patients, "total": len(patients)}


@app.get("/patients/{mrn}/summary")
def get_patient_summary(
    mrn: str,
    x_pii_mask: str | None = Header(default=None),
    session: dict = Depends(require_session),
):
    summary = _get_summary(mrn)
    if not summary:
        raise HTTPException(status_code=404, detail=f"No summary found for {mrn}")

    store = ReviewStore()
    try:
        store.ensure_pending(mrn, summary)
        review = store.get_review(mrn)
        actor, role = session["actor"], session["role"]
        store.log(
            actor=actor,
            role=role,
            action="view_summary",
            patient_mrn=mrn,
            content_hash_value=content_hash(summary),
        )
    finally:
        store.close()

    pii = _pii_enabled(x_pii_mask)
    real_name = _real_name_for(mrn) if pii else ""
    masked = mask_summary(summary, real_name, pii)

    # Fact grounding: verify each claim against the raw bundle.
    # Verification uses the UN-masked bundle so redaction never breaks matching.
    fhir_store = FHIRStore()
    try:
        bundle_json = fhir_store.get_bundle_json(mrn)
    finally:
        fhir_store.close()
    bundle = json.loads(bundle_json) if bundle_json else {}
    verification = verify_summary(summary, bundle)

    return {
        **masked,
        "review": review,
        "pii_masked": pii,
        "verification": verification,
        "cross_vendor_verification": _verification_for(mrn),
    }


class ReviewDecision(BaseModel):
    status: str
    reviewer: str = "anonymous"
    edited_summary: str | None = None
    notes: str | None = None


@app.get("/reviews")
def list_reviews(
    status: str | None = Query(default=None),
    session: dict = Depends(require_session),
):
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {status}")
    store = ReviewStore()
    try:
        reviews = store.list_reviews(status=status)
    finally:
        store.close()

    summaries_by_mrn = {s["patient_mrn"]: s for s in _load_all_summaries()}
    enriched = []
    for r in reviews:
        s = summaries_by_mrn.get(r["patient_mrn"])
        enriched.append({
            **r,
            "chief_concern": s.get("chief_concern") if s else None,
            "summary": s.get("summary") if s else None,
        })
    return {"reviews": enriched, "total": len(enriched)}


@app.post("/reviews/{mrn}")
def submit_review(
    mrn: str,
    decision: ReviewDecision,
    session: dict = Depends(require_session),
):
    if decision.status not in {STATUS_APPROVED, STATUS_EDITED, STATUS_REJECTED}:
        raise HTTPException(
            status_code=400,
            detail="status must be one of: approved, edited, rejected",
        )
    if decision.status == STATUS_EDITED and not decision.edited_summary:
        raise HTTPException(
            status_code=400,
            detail="edited_summary required when status is 'edited'",
        )

    summary = _get_summary(mrn)
    if not summary:
        raise HTTPException(status_code=404, detail=f"No summary found for {mrn}")

    actor, role = session["actor"], session["role"]
    if role != "reviewer":
        raise HTTPException(
            status_code=403,
            detail="Only users acting as 'reviewer' can approve, edit, or reject summaries. Switch role in the header.",
        )
    reviewer = decision.reviewer or actor

    store = ReviewStore()
    try:
        store.ensure_pending(mrn, summary)
        review = store.set_status(
            patient_mrn=mrn,
            status=decision.status,
            reviewer=reviewer,
            edited_summary=decision.edited_summary,
            notes=decision.notes,
        )
        store.log(
            actor=reviewer,
            role=role,
            action=f"review_{decision.status}",
            patient_mrn=mrn,
            content_hash_value=content_hash(summary),
            notes=decision.notes,
        )
    finally:
        store.close()

    return {"review": review}


@app.get("/audit")
def list_audit(
    limit: int = Query(default=200, le=1000),
    patient_mrn: str | None = Query(default=None),
    session: dict = Depends(require_session),
):
    store = ReviewStore()
    try:
        entries = store.list_audit(limit=limit, patient_mrn=patient_mrn)
    finally:
        store.close()
    return {"entries": entries, "total": len(entries)}


@app.get("/verifications")
def get_all_verifications(session: dict = Depends(require_session)):
    """Cross-vendor verification results (independently generated by OpenAI GPT
    reviewing Claude-generated summaries against the same source FHIR bundle)."""
    data = _load_verifications()
    results = data.get("results", [])
    # Aggregate stats
    from collections import Counter
    verdicts = Counter(r.get("verdict") for r in results)
    recs = Counter(r.get("reviewer_recommendation") for r in results)
    total_hallucinations = sum(len(r.get("hallucinations", [])) for r in results)
    total_mismatches = sum(len(r.get("value_mismatches", [])) for r in results)
    return {
        "meta": data.get("meta", {}),
        "stats": {
            "total_patients": len(results),
            "verdicts": dict(verdicts),
            "recommendations": dict(recs),
            "total_hallucinated_claims": total_hallucinations,
            "total_value_mismatches": total_mismatches,
        },
        "results": results,
    }


class ClientAuditEvent(BaseModel):
    action: str
    patient_mrn: str | None = None
    notes: str | None = None


@app.post("/audit")
def record_audit_event(
    event: ClientAuditEvent,
    session: dict = Depends(require_session),
):
    actor, role = session["actor"], session["role"]
    store = ReviewStore()
    try:
        store.log(
            actor=actor,
            role=role,
            action=event.action,
            patient_mrn=event.patient_mrn,
            notes=event.notes,
        )
    finally:
        store.close()
    return {"ok": True}


@app.get("/patients/{mrn}/bundle")
def get_patient_bundle(
    mrn: str,
    x_pii_mask: str | None = Header(default=None),
    session: dict = Depends(require_session),
):
    store = FHIRStore()
    bundle_json = store.get_bundle_json(mrn)
    store.close()
    if not bundle_json:
        return {"error": f"No bundle found for {mrn}"}
    bundle = json.loads(bundle_json)
    return mask_bundle(bundle, _pii_enabled(x_pii_mask))


@app.get("/patients/{mrn}/records")
def get_patient_records(
    mrn: str,
    query: str = Query(""),
    x_pii_mask: str | None = Header(default=None),
    session: dict = Depends(require_session),
):
    if not query:
        return {"records": []}
    from app.search.embedder import get_model, get_chroma_client, get_collection
    model = get_model()
    client = get_chroma_client()
    collection = get_collection(client)
    query_embedding = model.encode([query]).tolist()
    total = collection.count()
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(total, 500),
        where={"patient_mrn": mrn},
        include=["documents", "metadatas", "distances"],
    )
    records = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        records.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "resource_type": meta["resource_type"],
            "date": meta.get("date", ""),
            "relevance_score": round(1 - distance, 4),
        })
    records.sort(key=lambda r: r["date"], reverse=True)

    pii = _pii_enabled(x_pii_mask)
    real_name = _real_name_for(mrn) if pii else ""
    records = [mask_record(r, real_name, pii) for r in records]
    return {"records": records}


# Serve frontend
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
