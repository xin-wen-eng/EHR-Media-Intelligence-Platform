"""
PII masking for clinical records.

When masking is enabled:
- Patient names -> "Patient XXXX" (last 4 of MRN)
- MRN -> "MRN-****XXXX" (last 4 shown)
- Date of birth -> year only ("1978")
- Any occurrence of the real patient name inside free-text (progress notes,
  discharge summaries) is redacted to "[PATIENT]".

Masking is applied at the HTTP response boundary — the underlying data in
SQLite / ChromaDB is never modified.
"""

import re
from typing import Any


def mask_mrn(mrn: str | None) -> str:
    if not mrn:
        return ""
    tail = mrn[-4:] if len(mrn) >= 4 else mrn
    return f"MRN-****{tail}"


def mask_name(mrn: str | None) -> str:
    if not mrn:
        return "Patient ????"
    tail = mrn[-4:] if len(mrn) >= 4 else mrn
    return f"Patient {tail}"


def mask_dob(dob: str | None) -> str:
    if not dob:
        return ""
    m = re.match(r"^(\d{4})", dob)
    return m.group(1) if m else ""


def _name_variants(full_name: str) -> list[str]:
    """Return name variants worth redacting: full, first-only, last-only,
    and versions with trailing digits stripped (Synthea appends digits)."""
    parts = [p for p in full_name.split() if p]
    variants: set[str] = set()
    for p in parts + [full_name]:
        variants.add(p)
        stripped = re.sub(r"\d+$", "", p)
        if stripped and stripped != p:
            variants.add(stripped)
    # Longest first so multi-word names redact before their parts
    return sorted((v for v in variants if len(v) >= 3), key=len, reverse=True)


def redact_text(text: str, real_names: list[str]) -> str:
    if not text or not real_names:
        return text
    out = text
    for name in real_names:
        if not name:
            continue
        for variant in _name_variants(name):
            pattern = re.compile(rf"\b{re.escape(variant)}\b", flags=re.IGNORECASE)
            out = pattern.sub("[PATIENT]", out)
    return out


def mask_search_hit(hit: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Mask display fields but keep patient_mrn as the opaque lookup identifier
    the frontend uses to open detail views. Rendered MRN goes in
    patient_mrn_display."""
    mrn = hit.get("patient_mrn", "")
    if not enabled:
        return {**hit, "patient_mrn_display": mrn}
    real_name = hit.get("patient_name", "")
    return {
        **hit,
        "patient_name": mask_name(mrn),
        "patient_mrn_display": mask_mrn(mrn),
        "text": redact_text(hit.get("text", ""), [real_name]),
        "summary_snippet": redact_text(hit.get("summary_snippet", ""), [real_name]),
    }


def mask_summary(summary: dict[str, Any], real_name: str, enabled: bool) -> dict[str, Any]:
    mrn = summary.get("patient_mrn", "")
    if not enabled:
        return {**summary, "patient_mrn_display": mrn}
    out = {**summary, "patient_mrn_display": mask_mrn(mrn)}
    for key in ("summary", "chief_concern"):
        if isinstance(out.get(key), str):
            out[key] = redact_text(out[key], [real_name])
    for key in ("key_diagnoses", "recent_labs", "recent_imaging", "flagged_anomalies"):
        val = out.get(key)
        if isinstance(val, list):
            out[key] = [redact_text(v, [real_name]) if isinstance(v, str) else v for v in val]
    return out


def mask_bundle(bundle: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Mask patient names, MRNs, DOB, and cross-references inside a FHIR bundle
    dict. Bundle is expected to be Bundle.model_dump_json output parsed to dict.
    """
    if not enabled or not isinstance(bundle, dict):
        return bundle

    real_names: list[str] = []
    real_mrn = ""

    entries = bundle.get("entry") or []
    # First find the MRN so we can build a consistent masked display name.
    for entry in entries:
        r = entry.get("resource") or {}
        if r.get("resourceType") == "Patient":
            for ident in r.get("identifier", []) or []:
                v = ident.get("value")
                if v and not real_mrn:
                    real_mrn = v
                    break
            if real_mrn:
                break

    masked_display = mask_name(real_mrn)

    for entry in entries:
        r = entry.get("resource") or {}
        if r.get("resourceType") == "Patient":
            for nm in r.get("name", []) or []:
                given = " ".join(nm.get("given", []) or [])
                family = nm.get("family") or ""
                full = f"{given} {family}".strip()
                if full:
                    real_names.append(full)
                nm["given"] = [masked_display]
                nm["family"] = ""
            for ident in r.get("identifier", []) or []:
                v = ident.get("value")
                if v:
                    ident["value"] = mask_mrn(v)
            if r.get("birthDate"):
                r["birthDate"] = mask_dob(r["birthDate"])
            for addr in r.get("address", []) or []:
                for k in ("line", "city", "district", "postalCode"):
                    if k in addr:
                        addr[k] = "[REDACTED]" if isinstance(addr[k], str) else ["[REDACTED]"]
            for tel in r.get("telecom", []) or []:
                if "value" in tel:
                    tel["value"] = "[REDACTED]"

    # Second pass: redact name mentions in DocumentReference text and DiagnosticReport conclusion
    if real_names:
        for entry in entries:
            r = entry.get("resource") or {}
            rtype = r.get("resourceType")
            if rtype == "DocumentReference":
                for content in r.get("content", []) or []:
                    att = content.get("attachment") or {}
                    if att.get("data"):
                        try:
                            import base64
                            decoded = base64.b64decode(att["data"]).decode("utf-8", errors="ignore")
                            redacted = redact_text(decoded, real_names)
                            att["data"] = base64.b64encode(redacted.encode("utf-8")).decode("ascii")
                        except Exception:
                            pass
            elif rtype == "DiagnosticReport":
                if r.get("conclusion"):
                    r["conclusion"] = redact_text(r["conclusion"], real_names)

    return bundle


def mask_record(record: dict[str, Any], real_name: str, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return record
    return {**record, "text": redact_text(record.get("text", ""), [real_name])}
