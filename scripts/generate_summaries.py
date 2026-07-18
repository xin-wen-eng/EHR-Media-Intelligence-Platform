"""
Generate clinical summaries for all patients and populate the SQLite cache.

Extracts clinical data from FHIR bundles and produces structured summaries.
"""

import base64
import json
import re
from collections import defaultdict
from pathlib import Path

from app.db.store import FHIRStore
from app.summarizer.cache import SummaryCache
from app.summarizer.llm import extract_text_from_bundle, MODEL


def parse_patient_data(bundle_json: str) -> dict:
    bundle = json.loads(bundle_json)
    data = {
        "name": "",
        "gender": "",
        "dob": "",
        "encounters": [],
        "labs": [],
        "imaging": [],
        "notes": [],
        "diagnoses": set(),
    }

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType", "")

        if rtype == "Patient":
            name = resource.get("name", [{}])[0]
            given = " ".join(name.get("given", []))
            family = name.get("family", "")
            data["name"] = f"{given} {family}"
            data["gender"] = resource.get("gender", "unknown")
            data["dob"] = resource.get("birthDate", "unknown")

        elif rtype == "Encounter":
            enc_type = ""
            for t in resource.get("type", []):
                enc_type = t.get("text", "")
            period = resource.get("actualPeriod", {})
            start = period.get("start", "")[:10]
            reasons = []
            for r in resource.get("reason", []):
                for use in r.get("use", []):
                    t = use.get("text", "")
                    if t:
                        reasons.append(t)
                        data["diagnoses"].add(t)
            data["encounters"].append({
                "date": start,
                "type": enc_type,
                "reasons": reasons,
            })

        elif rtype == "DiagnosticReport":
            conclusion = resource.get("conclusion", "")
            effective = resource.get("effectiveDateTime", "")[:10]
            code_text = resource.get("code", {}).get("text", "")
            if conclusion:
                data["labs"].append({
                    "date": effective,
                    "test": code_text,
                    "result": conclusion,
                })

        elif rtype == "DocumentReference":
            type_text = resource.get("type", {}).get("text", "")
            date = resource.get("date", "")[:10]
            for content in resource.get("content", []):
                raw = content.get("attachment", {}).get("data", "")
                if raw:
                    try:
                        text = base64.b64decode(raw).decode("utf-8")
                        if "imaging" in type_text.lower() or "radiograph" in type_text.lower():
                            data["imaging"].append({"date": date, "type": type_text, "text": text[:300]})
                        else:
                            data["notes"].append({"date": date, "type": type_text, "text": text[:300]})
                    except Exception:
                        pass

    return data


def find_abnormal_labs(labs: list[dict]) -> list[str]:
    abnormals = []
    skip_patterns = ["education", "family members", "unable to get", "housing",
                     "employment", "social contact", "stress", "transportation"]
    for lab in labs:
        result = lab["result"].lower()
        test_name = lab["test"]
        if any(p in result for p in skip_patterns) or any(p in test_name.lower() for p in skip_patterns):
            continue
        match = re.search(r":\s*([\d.]+)\s*(\S+)?$", lab["result"])
        if match:
            val = float(match.group(1))
            unit = match.group(2) or ""
            test = test_name.lower()
            flag = None
            if "glucose" in test and (val > 126 or val < 70):
                flag = "abnormal"
            elif "bmi" in test and val > 30:
                flag = "obese range"
            elif "blood pressure" in test and "systolic" in test and val > 140:
                flag = "elevated"
            elif "cholesterol" in test and val > 240:
                flag = "elevated"
            elif "hemoglobin" in test and "a1c" in test and val > 6.5:
                flag = "elevated"
            elif ("wbc" in test or "leukocyte" in test) and (val > 11 or val < 4):
                flag = "abnormal"
            elif "troponin" in test and val > 0.04:
                flag = "elevated"
            if flag:
                short_name = test_name[:50]
                abnormals.append(f"{short_name}: {val} {unit} ({flag})")

    return list(set(abnormals))[:5]


def generate_summary(data: dict) -> dict:
    # Chief concern: most recent encounter reason
    recent_encounters = sorted(data["encounters"], key=lambda x: x["date"], reverse=True)
    chief_concern = "Routine care"
    for enc in recent_encounters:
        if enc["reasons"]:
            chief_concern = enc["reasons"][0]
            break

    # Key diagnoses: unique conditions from encounter reasons
    diagnoses = sorted(data["diagnoses"])[:6]

    # Recent labs: last 5 unique lab tests
    seen_tests = set()
    recent_labs = []
    for lab in sorted(data["labs"], key=lambda x: x["date"], reverse=True):
        short_name = lab["test"][:50]
        if short_name not in seen_tests and len(recent_labs) < 5:
            seen_tests.add(short_name)
            result_parts = lab["result"].split(": ")
            result_val = result_parts[-1] if len(result_parts) > 1 else result_parts[0]
            recent_labs.append(f"{short_name}: {result_val}")

    # Recent imaging
    recent_imaging = []
    for img in sorted(data["imaging"], key=lambda x: x["date"], reverse=True)[:3]:
        text = img["text"]
        # Extract the key finding
        for line in text.split("\n"):
            if any(k in line.lower() for k in ["finding", "impression", "report", "no acute"]):
                recent_imaging.append(f"{img['type']} ({img['date']}): {line.strip()[:100]}")
                break
        else:
            recent_imaging.append(f"{img['type']} ({img['date']}): {text[:80].strip()}")

    # Flagged anomalies
    anomalies = find_abnormal_labs(data["labs"])

    # Build narrative summary
    age_parts = data["dob"].split("-")
    age = 2026 - int(age_parts[0]) if age_parts[0].isdigit() else "unknown"

    enc_types = defaultdict(int)
    for enc in data["encounters"]:
        enc_types[enc["type"]] += 1
    most_common_enc = max(enc_types, key=enc_types.get) if enc_types else "visits"

    summary_text = (
        f"{age}-year-old {data['gender']} with "
        f"{', '.join(diagnoses[:3]) if diagnoses else 'no documented conditions'}. "
        f"Recent activity includes {len(data['encounters'])} encounters, "
        f"primarily {most_common_enc.lower()}. "
    )
    if anomalies:
        summary_text += f"Notable findings: {'; '.join(a.split(':')[0] for a in anomalies[:2])} values flagged. "
    if recent_imaging:
        summary_text += f"Imaging: {len(data['imaging'])} studies on record. "
    summary_text += "Continued monitoring recommended."

    return {
        "chief_concern": chief_concern,
        "key_diagnoses": diagnoses,
        "recent_labs": recent_labs,
        "recent_imaging": recent_imaging if recent_imaging else [],
        "flagged_anomalies": anomalies if anomalies else [],
        "summary": summary_text[:400],
    }


def main():
    print("=" * 60)
    print("Generating Clinical Summaries")
    print("=" * 60)

    store = FHIRStore()
    cache = SummaryCache()
    patients = store.list_patients()

    all_summaries = []

    for p in patients:
        mrn = p["patient_mrn"]
        bundle_json = store.get_bundle_json(mrn)
        if not bundle_json:
            continue

        text = extract_text_from_bundle(bundle_json)
        content_hash = SummaryCache.compute_hash(text)

        data = parse_patient_data(bundle_json)
        summary = generate_summary(data)

        summary["patient_mrn"] = mrn
        summary["disclaimer"] = (
            "AI-generated summary. Not a clinical decision. "
            "Must be reviewed by a qualified healthcare provider."
        )
        summary["model"] = MODEL
        summary["cached"] = False

        cache.put(mrn, content_hash, summary, MODEL)
        all_summaries.append(summary)

        chief = summary["chief_concern"][:50]
        n_diag = len(summary["key_diagnoses"])
        n_anom = len(summary["flagged_anomalies"])
        print(f"  {mrn}: {chief}... ({n_diag} diagnoses, {n_anom} anomalies)")

    # Save to file
    output_path = Path(__file__).parent.parent / "data" / "summaries.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_summaries, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print(f"Generated: {len(all_summaries)} summaries")
    print(f"Cached in: data/ehr.db (summary_cache table)")
    print(f"Saved to:  {output_path}")

    store.close()
    cache.close()


if __name__ == "__main__":
    main()
