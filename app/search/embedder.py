"""
Embedding pipeline using sentence-transformers + ChromaDB.

Embeds FHIR document text and AI summaries for semantic search.
"""

import base64
import json
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from app.db.store import FHIRStore

MODEL_NAME = "all-MiniLM-L6-v2"
CHROMA_DIR = Path(__file__).parent.parent.parent / "data" / "chroma"

_model_cache: SentenceTransformer | None = None
_chroma_client = None
_collection_cache = None


def get_model() -> SentenceTransformer:
    global _model_cache
    if _model_cache is None:
        _model_cache = SentenceTransformer(MODEL_NAME)
    return _model_cache


def get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        except (ValueError, KeyError):
            chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
            _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _chroma_client


def get_collection(client: chromadb.PersistentClient) -> chromadb.Collection:
    global _collection_cache
    if _collection_cache is None:
        _collection_cache = client.get_or_create_collection(
            name="ehr_documents",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection_cache


def extract_patient_name(bundle: dict) -> str:
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            names = resource.get("name", [])
            if names:
                given = " ".join(names[0].get("given", []))
                family = names[0].get("family", "")
                return f"{given} {family}".strip()
    return ""


def extract_documents_from_bundle(bundle_json: str, patient_mrn: str) -> list[dict]:
    bundle = json.loads(bundle_json)
    patient_name = extract_patient_name(bundle)
    docs = []

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType", "")

        if rtype == "DiagnosticReport":
            conclusion = resource.get("conclusion", "")
            if not conclusion:
                continue
            effective = resource.get("effectiveDateTime", "")[:10]
            code_text = resource.get("code", {}).get("text", "")
            docs.append({
                "id": f"{patient_mrn}_{resource.get('id', '')}",
                "text": f"{code_text}: {conclusion}",
                "patient_mrn": patient_mrn,
                "patient_name": patient_name,
                "resource_type": "DiagnosticReport",
                "date": effective,
            })

        elif rtype == "DocumentReference":
            type_text = resource.get("type", {}).get("text", "")
            date = resource.get("date", "")[:10]
            for content in resource.get("content", []):
                raw = content.get("attachment", {}).get("data", "")
                if raw:
                    try:
                        text = base64.b64decode(raw).decode("utf-8")
                        doc_type = "DocumentReference"
                        if "imaging" in type_text.lower() or "radiograph" in type_text.lower():
                            doc_type = "ImagingReport"
                        elif "discharge" in type_text.lower():
                            doc_type = "DischargeSummary"
                        elif "progress" in type_text.lower():
                            doc_type = "ProgressNote"

                        docs.append({
                            "id": f"{patient_mrn}_{resource.get('id', '')}",
                            "text": text[:1000],
                            "patient_mrn": patient_mrn,
                            "patient_name": patient_name,
                            "resource_type": doc_type,
                            "date": date,
                        })
                    except Exception:
                        pass

    return docs


def build_index():
    print("=" * 60)
    print("Building Semantic Search Index")
    print("=" * 60)

    model = get_model()
    client = get_chroma_client()

    # Delete existing collection to rebuild
    global _collection_cache
    _collection_cache = None
    try:
        client.delete_collection("ehr_documents")
    except Exception:
        pass
    collection = get_collection(client)

    store = FHIRStore()
    patients = store.list_patients()

    # Also load AI summaries
    summaries_path = Path(__file__).parent.parent.parent / "data" / "summaries.json"
    summaries_by_mrn = {}
    if summaries_path.exists():
        with open(summaries_path) as f:
            for s in json.load(f):
                summaries_by_mrn[s["patient_mrn"]] = s

    all_docs = []
    for p in patients:
        mrn = p["patient_mrn"]
        bundle_json = store.get_bundle_json(mrn)
        if not bundle_json:
            continue

        docs = extract_documents_from_bundle(bundle_json, mrn)
        all_docs.extend(docs)

        # Add AI summary as a searchable document
        if mrn in summaries_by_mrn:
            s = summaries_by_mrn[mrn]
            summary_text = (
                f"Clinical Summary for {mrn}: "
                f"Chief concern: {s.get('chief_concern', '')}. "
                f"Diagnoses: {', '.join(s.get('key_diagnoses', []))}. "
                f"{s.get('summary', '')}"
            )
            patient_name = extract_patient_name(json.loads(bundle_json))
            all_docs.append({
                "id": f"{mrn}_summary",
                "text": summary_text,
                "patient_mrn": mrn,
                "patient_name": patient_name,
                "resource_type": "AISummary",
                "date": "",
            })

    print(f"\nTotal documents to embed: {len(all_docs)}")
    print("Generating embeddings (this may take a moment)...")

    # Batch insert into ChromaDB
    batch_size = 500
    for i in range(0, len(all_docs), batch_size):
        batch = all_docs[i:i + batch_size]
        ids = [d["id"] for d in batch]
        texts = [d["text"] for d in batch]
        metadatas = [{
            "patient_mrn": d["patient_mrn"],
            "patient_name": d.get("patient_name", ""),
            "resource_type": d["resource_type"],
            "date": d["date"],
        } for d in batch]

        embeddings = model.encode(texts, show_progress_bar=False).tolist()

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )
        print(f"  Indexed batch {i // batch_size + 1}: {len(batch)} documents")

    print(f"\nIndex built: {collection.count()} documents in ChromaDB")
    store.close()
    return collection


def search(
    query: str,
    n_results: int = 5,
    resource_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    model = get_model()
    client = get_chroma_client()
    collection = get_collection(client)

    query_embedding = model.encode([query]).tolist()

    # ChromaDB only supports string equality for where filters
    where = None
    if resource_type:
        where = {"resource_type": resource_type}

    # Fetch broadly to find enough unique patients after dedup
    fetch_n = min(n_results * 20, collection.count())

    results = collection.query(
        query_embeddings=query_embedding,
        n_results=fetch_n,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    # Collect all candidate hits with date filtering
    candidates = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        doc_date = meta.get("date", "")

        if date_from and doc_date and doc_date < date_from:
            continue
        if date_to and doc_date and doc_date > date_to:
            continue

        distance = results["distances"][0][i]
        score = 1 - distance

        candidates.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i][:300],
            "patient_mrn": meta["patient_mrn"],
            "patient_name": meta.get("patient_name", ""),
            "resource_type": meta["resource_type"],
            "date": doc_date,
            "relevance_score": round(score, 4),
        })

    # Per-patient dedup: keep the most recent document, track history count
    patient_hits: dict[str, dict] = {}
    patient_counts: dict[str, int] = {}
    for hit in candidates:
        mrn = hit["patient_mrn"]
        patient_counts[mrn] = patient_counts.get(mrn, 0) + 1
        if mrn not in patient_hits or hit["date"] > patient_hits[mrn]["date"]:
            patient_hits[mrn] = hit

    for mrn, hit in patient_hits.items():
        hit["match_count"] = patient_counts[mrn]

    # Sort by relevance score descending, return top n
    hits = sorted(patient_hits.values(), key=lambda h: h["relevance_score"], reverse=True)
    return hits[:n_results]


if __name__ == "__main__":
    build_index()

    print("\n" + "=" * 60)
    print("Test Search: 'chest pain'")
    print("=" * 60)
    results = search("chest pain")
    for r in results:
        print(f"  [{r['relevance_score']:.4f}] {r['patient_mrn']} ({r['resource_type']}): {r['text'][:80]}...")
