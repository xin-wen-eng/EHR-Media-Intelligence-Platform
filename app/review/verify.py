"""
Fact grounding for AI-generated clinical summaries.

For each factual claim in a summary (diagnoses, labs, imaging, anomalies),
attempt to trace it back to a source FHIR resource in the patient's bundle.

Grounded claims get a `verified` flag with pointers to the source resources
(type, id, date, text snippet). Ungrounded claims are marked `unverified`
so a reviewer can decide whether the AI hallucinated or the phrasing simply
diverged from the source.

This is deliberately simple (token overlap + substring match). It is meant
to catch obvious hallucinations, not to certify clinical correctness.
"""

import base64
import re
from typing import Any


STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "was", "were", "be", "been", "no",
    "not", "n/a", "na", "none", "patient", "history", "recent", "chronic",
    "acute", "mg", "kg", "dl", "ml", "l", "mmhg", "hg", "%",
}

# Resources we treat as evidence sources — order matters for display priority
EVIDENCE_TYPES = ("DiagnosticReport", "DocumentReference", "Encounter")


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    parts = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower())
    return {p for p in parts if p not in STOPWORDS}


def _numbers(text: str) -> set[str]:
    """Extract meaningful numeric values (e.g. 6.2, 30.1) for lab matching."""
    return set(re.findall(r"\d+\.\d+|\d{2,}", text or ""))


def _extract_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten bundle entries into evidence-candidates with searchable text."""
    out: list[dict[str, Any]] = []
    for entry in bundle.get("entry", []) or []:
        r = entry.get("resource") or {}
        rtype = r.get("resourceType")
        if rtype not in EVIDENCE_TYPES:
            continue

        parts: list[str] = []
        date = ""

        if rtype == "DiagnosticReport":
            code_text = (r.get("code") or {}).get("text", "")
            conclusion = r.get("conclusion", "") or ""
            date = (r.get("effectiveDateTime") or "")[:10]
            parts.extend([code_text, conclusion])
            for cat in r.get("category", []) or []:
                for c in cat.get("coding", []) or []:
                    parts.append(c.get("display", "") or "")

        elif rtype == "DocumentReference":
            doc_type = (r.get("type") or {}).get("text", "") or ""
            date = (r.get("date") or "")[:10]
            parts.append(doc_type)
            for content in r.get("content", []) or []:
                data = (content.get("attachment") or {}).get("data")
                if data:
                    try:
                        parts.append(base64.b64decode(data).decode("utf-8", errors="ignore"))
                    except Exception:
                        pass

        elif rtype == "Encounter":
            date = ((r.get("actualPeriod") or {}).get("start") or "")[:10]
            for t in r.get("type", []) or []:
                parts.append(t.get("text", "") or "")
            for reason in r.get("reason", []) or []:
                for use in reason.get("use", []) or []:
                    parts.append((use.get("concept") or {}).get("text", "") or use.get("text", "") or "")
            for cls in r.get("class_fhir", []) or []:
                for c in cls.get("coding", []) or []:
                    parts.append(c.get("display", "") or "")

        text = "\n".join(p for p in parts if p).strip()
        if not text:
            continue

        out.append({
            "resource_type": rtype,
            "resource_id": r.get("id", "") or "",
            "date": date,
            "text": text,
            "text_lower": text.lower(),
            "tokens": _tokens(text),
            "numbers": _numbers(text),
        })
    return out


def _snippet(text: str, needle: str, radius: int = 80) -> str:
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return text[:160].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _match_claim(claim: str, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return up to 3 resources that ground this claim, ranked by overlap."""
    if not claim or not claim.strip():
        return []
    claim_tokens = _tokens(claim)
    claim_numbers = _numbers(claim)
    if not claim_tokens and not claim_numbers:
        return []

    scored: list[tuple[float, dict]] = []
    for res in resources:
        # substring hit gets highest score
        substring_hit = claim.lower() in res["text_lower"]

        # token overlap
        token_overlap = len(claim_tokens & res["tokens"])
        # require at least half of the meaningful claim tokens to match
        if claim_tokens:
            required = max(1, len(claim_tokens) // 2)
            token_ok = token_overlap >= required
        else:
            token_ok = False

        # number match (for labs: catches "6.2" appearing in lab code text)
        number_match = bool(claim_numbers & res["numbers"])

        if not (substring_hit or token_ok or number_match):
            continue

        score = 0.0
        if substring_hit:
            score += 10
        if number_match:
            score += 3
        score += token_overlap

        scored.append((score, res))

    scored.sort(key=lambda x: -x[0])
    hits: list[dict[str, Any]] = []
    for _, res in scored[:3]:
        hits.append({
            "resource_type": res["resource_type"],
            "resource_id": res["resource_id"],
            "date": res["date"],
            "snippet": _snippet(res["text"], claim.split(":")[0]),
        })
    return hits


def _verify_field(claim: str, resources: list[dict[str, Any]]) -> dict[str, Any]:
    hits = _match_claim(claim, resources)
    return {
        "claim": claim,
        "grounded": len(hits) > 0,
        "evidence": hits,
    }


def verify_summary(summary: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """Return a verification report for the summary.

    Shape:
      {
        "counts": {"total": N, "grounded": M, "unverified": N-M},
        "insufficient_data": bool,
        "fields": {
          "chief_concern": {claim, grounded, evidence: [...]},
          "key_diagnoses":   [ {claim, grounded, evidence}, ... ],
          "recent_labs":     [ ... ],
          "recent_imaging":  [ ... ],
          "flagged_anomalies": [ ... ],
        }
      }
    """
    resources = _extract_resources(bundle or {})

    # Safe refusal signal: no evidence resources at all
    insufficient = len(resources) == 0

    fields: dict[str, Any] = {}
    total = 0
    grounded = 0

    cc = summary.get("chief_concern") or ""
    if cc:
        fields["chief_concern"] = _verify_field(cc, resources)
        total += 1
        if fields["chief_concern"]["grounded"]:
            grounded += 1
    else:
        fields["chief_concern"] = None

    for key in ("key_diagnoses", "recent_labs", "recent_imaging", "flagged_anomalies"):
        items = summary.get(key) or []
        verified_items = []
        for item in items:
            if not isinstance(item, str):
                continue
            v = _verify_field(item, resources)
            verified_items.append(v)
            total += 1
            if v["grounded"]:
                grounded += 1
        fields[key] = verified_items

    return {
        "counts": {
            "total": total,
            "grounded": grounded,
            "unverified": total - grounded,
        },
        "insufficient_data": insufficient,
        "fields": fields,
    }
