"""
Cross-vendor verification via the OpenAI API.

For each Claude-generated summary, ask an OpenAI model to independently
review it against the same source FHIR bundle the summary was written from.

Design:
- The API call is **content-hashed**: (source_text, summary_json, model) →
  hash. If a verdict already exists in the cache under that hash, we skip
  the API call. That makes re-runs free.
- The module is **not** wired into the FastAPI lifespan on purpose. It never
  runs automatically. Trigger it via `python -m app.review.cross_vendor`.
- Falls back gracefully: if OPENAI_API_KEY is unset, prints a clear message
  and exits without cost.

CLI:
    python -m app.review.cross_vendor              # verify every patient
    python -m app.review.cross_vendor MRN-C05BB077 # single patient
    python -m app.review.cross_vendor --force      # ignore cache

Environment:
    OPENAI_API_KEY        required to actually call the API
    OPENAI_MODEL          override the model (default: gpt-4o-mini)

Output:
    Writes/updates data/verifications.json, matching the shape the frontend
    and /verifications endpoint expect. The manual-ChatGPT run's results
    were saved in the same shape; this module can either extend or
    overwrite depending on --force.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).parent.parent.parent / "data"
VERIFICATIONS_PATH = DATA_DIR / "verifications.json"
SUMMARIES_PATH = DATA_DIR / "summaries.json"
CACHE_PATH = DATA_DIR / "cross_vendor_cache.json"

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TEMPERATURE = 0.0  # deterministic-ish for repeatability


SYSTEM_PROMPT = """You are an independent verifier. Another AI system generated the SUMMARY below from the SOURCE clinical records. Identify any claim in the SUMMARY that is not supported by that patient's SOURCE.

Return ONLY a single JSON object with this shape (no prose, no markdown fence):

{
  "patient_mrn": "MRN-XXXX",
  "verdict": "clean" | "partially_hallucinated" | "insufficient_source",
  "hallucinations": [
    {"field": "recent_labs", "claim": "...", "reason": "..."}
  ],
  "value_mismatches": [
    {"field": "recent_labs", "claim": "...", "source_says": "..."}
  ],
  "grounded_claims_count": 0,
  "summary_narrative_ok": true,
  "reviewer_recommendation": "approve" | "edit" | "reject"
}

Rules:
- A claim is *hallucinated* only if it does NOT appear anywhere in SOURCE.
- A claim is a *value_mismatch* if the fact exists but a number/date/spelling differs.
- Ignore trivial rephrasing.
- If SOURCE is empty or contains almost no clinical detail, set verdict to
  "insufficient_source" and reviewer_recommendation to "reject".
"""


def _content_hash(source: str, summary: dict[str, Any], model: str) -> str:
    payload = json.dumps(
        {"source": source, "summary": summary, "model": model},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH) as f:
        return json.load(f)


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def extract_source_text(bundle_json: str) -> str:
    """Same shape as app.summarizer.llm.extract_text_from_bundle, but kept
    inline here so this module has no import-time cost when OPENAI_API_KEY
    is not set."""
    bundle = json.loads(bundle_json)
    parts: list[str] = []
    for entry in bundle.get("entry", []) or []:
        r = entry.get("resource") or {}
        rtype = r.get("resourceType", "")
        if rtype == "Patient":
            name = (r.get("name") or [{}])[0]
            given = " ".join(name.get("given", []) or [])
            family = name.get("family", "") or ""
            parts.append(f"PATIENT: {given} {family}, gender: {r.get('gender', 'unknown')}, DOB: {r.get('birthDate', 'unknown')}")
        elif rtype == "DiagnosticReport":
            code_text = (r.get("code") or {}).get("text", "")
            concl = r.get("conclusion", "") or ""
            when = (r.get("effectiveDateTime") or "")[:10]
            if code_text or concl:
                parts.append(f"LAB ({when}): {code_text}: {concl}")
        elif rtype == "DocumentReference":
            doc_type = (r.get("type") or {}).get("text", "") or ""
            when = (r.get("date") or "")[:10]
            for content in r.get("content", []) or []:
                data = (content.get("attachment") or {}).get("data")
                if data:
                    try:
                        parts.append(f"DOCUMENT ({when}, {doc_type}):\n{base64.b64decode(data).decode('utf-8', errors='ignore')[:600]}")
                    except Exception:
                        pass
        elif rtype == "Encounter":
            when = ((r.get("actualPeriod") or {}).get("start") or "")[:10]
            reasons = []
            for reason in r.get("reason", []) or []:
                for use in reason.get("use", []) or []:
                    reasons.append((use.get("concept") or {}).get("text", "") or "")
            if reasons:
                parts.append(f"ENCOUNTER ({when}): {', '.join(r for r in reasons if r)}")
    return "\n".join(parts)


def verify_one(
    patient_mrn: str,
    source_text: str,
    summary: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    force: bool = False,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a verdict dict. Uses cache unless force=True."""
    cache = cache if cache is not None else _load_cache()
    key = _content_hash(source_text, summary, model)

    if not force and key in cache:
        return cache[key]

    # Lazy import so the module can be inspected without openai installed
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai package not installed. Add `openai>=1.0` to pyproject and reinstall."
        ) from e

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Export it before running:\n"
            "  export OPENAI_API_KEY=sk-..."
        )

    client = OpenAI(api_key=api_key)

    clean_summary = {
        k: summary.get(k)
        for k in ("chief_concern", "key_diagnoses", "recent_labs",
                  "recent_imaging", "flagged_anomalies", "summary")
    }

    user_prompt = (
        f"## Patient: {patient_mrn}\n\n"
        f"### SOURCE\n```\n{source_text}\n```\n\n"
        f"### SUMMARY\n```json\n{json.dumps(clean_summary, indent=2, ensure_ascii=False)}\n```"
    )

    response = client.chat.completions.create(
        model=model,
        temperature=DEFAULT_TEMPERATURE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        verdict = {
            "patient_mrn": patient_mrn,
            "verdict": "parse_error",
            "hallucinations": [],
            "value_mismatches": [],
            "grounded_claims_count": 0,
            "summary_narrative_ok": False,
            "reviewer_recommendation": "reject",
            "raw_response": raw[:1000],
        }
    verdict.setdefault("patient_mrn", patient_mrn)

    cache[key] = verdict
    _save_cache(cache)
    return verdict


def run(
    only: list[str] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    force: bool = False,
) -> dict[str, Any]:
    """Batch entry point. Loads summaries + FHIR bundles from the SQLite
    store and writes results into data/verifications.json."""
    # Local imports to avoid pulling the FHIR/db stack at module import time
    from app.db.store import FHIRStore

    if not SUMMARIES_PATH.exists():
        raise FileNotFoundError(f"summaries missing: {SUMMARIES_PATH}")

    with open(SUMMARIES_PATH) as f:
        summaries = json.load(f)

    if only:
        summaries = [s for s in summaries if s["patient_mrn"] in only]
        if not summaries:
            raise ValueError(f"none of the given MRNs found: {only}")

    store = FHIRStore()
    cache = _load_cache()

    results: list[dict[str, Any]] = []
    for i, s in enumerate(summaries, 1):
        mrn = s["patient_mrn"]
        bundle_json = store.get_bundle_json(mrn)
        if not bundle_json:
            print(f"[{i}/{len(summaries)}] {mrn}  ⨯ no bundle in DB, skipping", file=sys.stderr)
            continue

        source_text = extract_source_text(bundle_json)
        print(f"[{i}/{len(summaries)}] {mrn}  … verifying (source {len(source_text)} chars)")
        verdict = verify_one(mrn, source_text, s, model=model, force=force, cache=cache)
        results.append(verdict)
    store.close()

    out = {
        "meta": {
            "verifier_model": f"OpenAI {model} (automated via API)",
            "primary_model": "Anthropic claude-opus-4-6",
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": "v3-automated",
            "method": "Cross-vendor verification. Each Claude-generated summary is independently verified by an OpenAI model against the same source FHIR bundle, invoked programmatically with content-hash caching.",
        },
        "results": results,
    }

    VERIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(VERIFICATIONS_PATH, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(results)} verdicts to {VERIFICATIONS_PATH}")
    print(f"Cache: {CACHE_PATH}")
    return out


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.review.cross_vendor",
        description="Run OpenAI cross-vendor verification on Claude-generated clinical summaries.",
    )
    parser.add_argument(
        "mrns",
        nargs="*",
        help="Optional list of patient MRNs to verify. Default: all patients.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        help=f"OpenAI model to use (default: {DEFAULT_MODEL} or $OPENAI_MODEL).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cache and re-verify every patient.",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is not set. Export it before running:\n"
            "  export OPENAI_API_KEY=sk-...\n\n"
            "This module never runs automatically at server startup — you must invoke it explicitly.",
            file=sys.stderr,
        )
        return 2

    try:
        run(only=args.mrns or None, model=args.model, force=args.force)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
