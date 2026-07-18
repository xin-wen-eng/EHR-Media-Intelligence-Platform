"""
Summarization pipeline.

Reads FHIR bundles from SQLite → extracts text → calls Claude → caches results.
"""

import json
from pathlib import Path

from app.db.store import FHIRStore
from .cache import SummaryCache
from .llm import summarize_patient

DATA_DIR = Path(__file__).parent.parent.parent / "data"


def run_summarization_pipeline():
    print("=" * 60)
    print("AI Clinical Summarization Pipeline")
    print("=" * 60)

    store = FHIRStore()
    cache = SummaryCache()

    patients = store.list_patients()
    print(f"\nPatients to summarize: {len(patients)}")
    print(f"Cached summaries: {cache.count()}")

    summaries = []
    new_count = 0
    cached_count = 0

    for p in patients:
        mrn = p["patient_mrn"]
        bundle_json = store.get_bundle_json(mrn)
        if not bundle_json:
            continue

        summary = summarize_patient(mrn, bundle_json, cache)

        if summary.get("cached"):
            cached_count += 1
            status = "cached"
        else:
            new_count += 1
            status = "new"

        summaries.append(summary)
        chief = summary.get("chief_concern", "")[:50]
        print(f"  [{status:6s}] {mrn}: {chief}...")

    # Save all summaries to file
    output_path = DATA_DIR / "summaries.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print("Summarization Complete")
    print(f"{'=' * 60}")
    print(f"  New summaries:    {new_count}")
    print(f"  From cache:       {cached_count}")
    print(f"  Total:            {len(summaries)}")
    print(f"  Output:           {output_path}")

    store.close()
    cache.close()

    return summaries


if __name__ == "__main__":
    run_summarization_pipeline()
