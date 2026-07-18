"""
Claude API integration for clinical summarization.

Generates structured summaries from FHIR Bundle text content.
"""

import base64
import json
import os

import anthropic

from .cache import SummaryCache

MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are a clinical documentation assistant. Given a patient's medical records,
produce a concise structured clinical summary in JSON format.

Your summary MUST follow this exact JSON structure:
{
  "chief_concern": "Primary reason for recent encounters",
  "key_diagnoses": ["diagnosis1", "diagnosis2"],
  "recent_labs": ["lab1: value units", "lab2: value units"],
  "recent_imaging": ["imaging finding 1"],
  "flagged_anomalies": ["any abnormal values or concerning findings"],
  "summary": "Brief narrative summary of patient's clinical picture"
}

Rules:
- Keep the total summary under 200 words
- Only include clinically relevant information
- Flag any abnormal lab values or concerning patterns
- If no data exists for a section, use an empty array []
- Be factual and precise — do not speculate or diagnose
- Return ONLY valid JSON, no markdown or explanation"""


def extract_text_from_bundle(bundle_json: str) -> str:
    bundle = json.loads(bundle_json)
    parts = []

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType", "")

        if rtype == "Patient":
            name = resource.get("name", [{}])[0]
            given = " ".join(name.get("given", []))
            family = name.get("family", "")
            gender = resource.get("gender", "unknown")
            dob = resource.get("birthDate", "unknown")
            parts.append(f"PATIENT: {given} {family}, Gender: {gender}, DOB: {dob}")

        elif rtype == "DiagnosticReport":
            conclusion = resource.get("conclusion", "")
            effective = resource.get("effectiveDateTime", "")
            code_text = resource.get("code", {}).get("text", "")
            if conclusion:
                parts.append(f"LAB ({effective[:10]}): {code_text}: {conclusion}")

        elif rtype == "DocumentReference":
            type_text = resource.get("type", {}).get("text", "")
            date = resource.get("date", "")[:10]
            for content in resource.get("content", []):
                data = content.get("attachment", {}).get("data", "")
                if data:
                    try:
                        text = base64.b64decode(data).decode("utf-8")
                        parts.append(f"DOCUMENT ({date}, {type_text}):\n{text[:500]}")
                    except Exception:
                        pass

        elif rtype == "Encounter":
            enc_type = ""
            for t in resource.get("type", []):
                enc_type = t.get("text", "")
            period = resource.get("actualPeriod", {})
            start = period.get("start", "")[:10]
            reason_list = resource.get("reason", [])
            reason = ""
            for r in reason_list:
                for use in r.get("use", []):
                    reason = use.get("text", "")
            if enc_type:
                parts.append(f"ENCOUNTER ({start}): {enc_type}" + (f" - Reason: {reason}" if reason else ""))

    return "\n".join(parts)


def summarize_patient(
    patient_mrn: str,
    bundle_json: str,
    cache: SummaryCache | None = None,
) -> dict:
    text = extract_text_from_bundle(bundle_json)

    if not text.strip():
        return {
            "patient_mrn": patient_mrn,
            "chief_concern": "No clinical data available",
            "key_diagnoses": [],
            "recent_labs": [],
            "recent_imaging": [],
            "flagged_anomalies": [],
            "summary": "No clinical records found for this patient.",
            "disclaimer": "AI-generated summary. Not a clinical decision. Must be reviewed by a qualified healthcare provider.",
            "model": MODEL,
            "cached": False,
        }

    content_hash = SummaryCache.compute_hash(text)

    if cache:
        cached = cache.get(patient_mrn, content_hash)
        if cached:
            cached["cached"] = True
            return cached

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Truncate to avoid token limits — keep most recent records
    if len(text) > 12000:
        lines = text.split("\n")
        patient_line = lines[0] if lines[0].startswith("PATIENT:") else ""
        remaining = lines[1:] if patient_line else lines
        remaining = remaining[-(80):]
        text = patient_line + "\n" + "\n".join(remaining)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Summarize this patient's clinical records:\n\n{text}",
        }],
    )

    response_text = response.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

    try:
        summary = json.loads(response_text)
    except json.JSONDecodeError:
        summary = {
            "chief_concern": "Unable to parse",
            "key_diagnoses": [],
            "recent_labs": [],
            "recent_imaging": [],
            "flagged_anomalies": [],
            "summary": response_text[:500],
        }

    summary["patient_mrn"] = patient_mrn
    summary["disclaimer"] = (
        "AI-generated summary. Not a clinical decision. "
        "Must be reviewed by a qualified healthcare provider."
    )
    summary["model"] = MODEL
    summary["cached"] = False

    if cache:
        cache.put(patient_mrn, content_hash, summary, MODEL)

    return summary
