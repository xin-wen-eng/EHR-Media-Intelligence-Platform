"""
FastAPI application with search endpoint.
"""

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.search.embedder import get_model, get_chroma_client, search
from app.db.store import FHIRStore
from app.summarizer.cache import SummaryCache


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
    get_chroma_client()
    yield


app = FastAPI(
    title="EHR Media Intelligence Platform",
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
    patient_name: str = ""
    resource_type: str
    match_count: int = 1
    date: str
    relevance_score: float
    summary_snippet: str = ""


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int
    elapsed_ms: float


def _load_summaries() -> dict[str, str]:
    path = Path(__file__).parent.parent.parent / "data" / "summaries.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return {s["patient_mrn"]: s.get("summary", "") for s in json.load(f)}


@app.post("/search", response_model=SearchResponse)
def search_records(request: SearchRequest):
    start = time.time()

    hits = search(
        query=request.query,
        n_results=request.n_results,
        resource_type=request.resource_type,
        date_from=request.date_from,
        date_to=request.date_to,
    )

    summaries = _load_summaries()
    for h in hits:
        h["summary_snippet"] = summaries.get(h["patient_mrn"], "")

    elapsed = (time.time() - start) * 1000

    return SearchResponse(
        query=request.query,
        results=[SearchResult(**h) for h in hits],
        total=len(hits),
        elapsed_ms=round(elapsed, 1),
    )


@app.get("/patients")
def list_patients():
    store = FHIRStore()
    patients = store.list_patients()
    store.close()
    return {"patients": patients, "total": len(patients)}


@app.get("/patients/{mrn}/summary")
def get_patient_summary(mrn: str):
    summaries_path = Path(__file__).parent.parent.parent / "data" / "summaries.json"
    if not summaries_path.exists():
        return {"error": "No summaries found"}

    with open(summaries_path) as f:
        summaries = json.load(f)

    for s in summaries:
        if s["patient_mrn"] == mrn:
            return s

    return {"error": f"No summary found for {mrn}"}


@app.get("/patients/{mrn}/bundle")
def get_patient_bundle(mrn: str):
    store = FHIRStore()
    bundle_json = store.get_bundle_json(mrn)
    store.close()
    if not bundle_json:
        return {"error": f"No bundle found for {mrn}"}
    return json.loads(bundle_json)


@app.get("/patients/{mrn}/records")
def get_patient_records(mrn: str, query: str = Query("")):
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
    return {"records": records}


# Serve frontend
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
