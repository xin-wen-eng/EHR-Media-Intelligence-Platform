from __future__ import annotations

"""
Transform Synthea output into simulated raw EHR data with intentional quality issues.

Produces two input formats:
  - JSON: patients_ehr_export.json (simulated EHR system dump)
  - CSV:  lab_results.csv, imaging_reports.csv, clinical_notes.csv

Injects 6 types of dirty data:
  1. Inconsistent date formats
  2. Inconsistent gender codes
  3. Missing fields (DOB, gender, address)
  4. Duplicate patient records
  5. Conflicting MRN formats
  6. Duplicate documents
"""

import csv
import json
import random
import os
from pathlib import Path
from datetime import datetime

random.seed(42)

SYNTHEA_DIR = Path(__file__).parent.parent / "output" / "csv"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"

# ── Corruption helpers ──────────────────────────────────────────────

DATE_FORMATS = [
    "%Y-%m-%d",       # 2026-07-15
    "%m/%d/%Y",       # 07/15/2026
    "%d-%b-%Y",       # 15-Jul-2026
    "%Y/%m/%d",       # 2026/07/15
    "%m-%d-%Y",       # 07-15-2026
    "%b %d, %Y",      # Jul 15, 2026
]

GENDER_VARIANTS = {
    "M": ["M", "Male", "male", "MALE", "1", "m"],
    "F": ["F", "Female", "female", "FEMALE", "2", "f"],
}

MRN_FORMATS = [
    lambda pid: f"MRN-{pid[:8].upper()}",
    lambda pid: pid[:8],
    lambda pid: f"00{pid[:6]}",
    lambda pid: f"MRN{pid[:8].replace('-', '')}",
    lambda pid: pid[:12],
]


def corrupt_date(date_str: str) -> str:
    """Randomly reformat a date string into an inconsistent format."""
    if not date_str:
        return ""
    try:
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        fmt = random.choice(DATE_FORMATS)
        return dt.strftime(fmt)
    except (ValueError, TypeError):
        return date_str


def corrupt_gender(gender: str) -> str:
    """Randomly pick a variant gender code."""
    variants = GENDER_VARIANTS.get(gender, [gender])
    return random.choice(variants)


def make_mrn(patient_id: str) -> str:
    """Generate an MRN in a random format."""
    fmt = random.choice(MRN_FORMATS)
    return fmt(patient_id)


def maybe_blank(value: str, probability: float = 0.08) -> str:
    """Randomly blank out a field."""
    if random.random() < probability:
        return ""
    return value


# ── Read Synthea CSVs ───────────────────────────────────────────────

def read_csv(filename: str) -> list[dict]:
    filepath = SYNTHEA_DIR / filename
    if not filepath.exists():
        return []
    with open(filepath, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_synthea_data():
    return {
        "patients": read_csv("patients.csv"),
        "encounters": read_csv("encounters.csv"),
        "conditions": read_csv("conditions.csv"),
        "observations": read_csv("observations.csv"),
        "imaging": read_csv("imaging_studies.csv"),
        "medications": read_csv("medications.csv"),
    }


# ── Generate JSON: patients_ehr_export.json ─────────────────────────

def build_patient_json(data: dict) -> list[dict]:
    patients = data["patients"]
    enc_by_patient = {}
    for e in data["encounters"]:
        enc_by_patient.setdefault(e["PATIENT"], []).append(e)
    cond_by_patient = {}
    for c in data["conditions"]:
        cond_by_patient.setdefault(c["PATIENT"], []).append(c)
    med_by_patient = {}
    for m in data["medications"]:
        med_by_patient.setdefault(m["PATIENT"], []).append(m)

    records = []
    duplicated_ids = set()

    for p in patients:
        pid = p["Id"]
        mrn = make_mrn(pid)

        record = {
            "patient_id": pid,
            "mrn": mrn,
            "first_name": p.get("FIRST", ""),
            "last_name": p.get("LAST", ""),
            "middle_name": p.get("MIDDLE", ""),
            "date_of_birth": corrupt_date(maybe_blank(p.get("BIRTHDATE", ""))),
            "gender": corrupt_gender(maybe_blank(p.get("GENDER", ""), 0.06)),
            "race": p.get("RACE", ""),
            "ethnicity": p.get("ETHNICITY", ""),
            "address": maybe_blank(p.get("ADDRESS", ""), 0.05),
            "city": p.get("CITY", ""),
            "state": p.get("STATE", ""),
            "zip": p.get("ZIP", ""),
            "marital_status": p.get("MARITAL", ""),
            "death_date": p.get("DEATHDATE", ""),
            "encounters": [],
            "diagnoses": [],
            "medications": [],
        }

        # Add encounters (keep recent 10 max)
        patient_encs = enc_by_patient.get(pid, [])
        for e in patient_encs[-10:]:
            record["encounters"].append({
                "encounter_id": e["Id"],
                "start": corrupt_date(e.get("START", "")),
                "end": corrupt_date(e.get("STOP", "")),
                "class": e.get("ENCOUNTERCLASS", ""),
                "type": e.get("DESCRIPTION", ""),
                "reason": e.get("REASONDESCRIPTION", ""),
            })

        # Add diagnoses
        patient_conds = cond_by_patient.get(pid, [])
        for c in patient_conds:
            record["diagnoses"].append({
                "date": corrupt_date(c.get("START", "")),
                "end_date": corrupt_date(c.get("STOP", "")),
                "code": c.get("CODE", ""),
                "system": c.get("SYSTEM", ""),
                "description": c.get("DESCRIPTION", ""),
            })

        # Add medications (keep recent 8 max)
        patient_meds = med_by_patient.get(pid, [])
        for m in patient_meds[-8:]:
            record["medications"].append({
                "start": corrupt_date(m.get("START", "")),
                "stop": corrupt_date(m.get("STOP", "")),
                "code": m.get("CODE", ""),
                "description": m.get("DESCRIPTION", ""),
                "reason": m.get("REASONDESCRIPTION", ""),
            })

        records.append(record)

        # Inject duplicate patients (~15% chance)
        if random.random() < 0.15 and pid not in duplicated_ids:
            duplicated_ids.add(pid)
            dup = json.loads(json.dumps(record))
            # Slightly alter the duplicate to simulate conflicting data
            dup["mrn"] = make_mrn(pid + "dup")
            dup["first_name"] = dup["first_name"].upper()
            if dup["date_of_birth"]:
                dup["date_of_birth"] = corrupt_date(p.get("BIRTHDATE", ""))
            records.append(dup)

    random.shuffle(records)
    return records


# ── Generate CSV: lab_results.csv ───────────────────────────────────

LAB_CATEGORIES = {"laboratory", "vital-signs", "survey"}

def build_lab_csv(data: dict) -> list[dict]:
    rows = []
    seen = set()
    for obs in data["observations"]:
        cat = obs.get("CATEGORY", "").lower()
        if cat not in LAB_CATEGORIES:
            continue

        row = {
            "patient_id": obs["PATIENT"],
            "encounter_id": obs["ENCOUNTER"],
            "date": corrupt_date(obs.get("DATE", "")),
            "category": cat,
            "code": obs.get("CODE", ""),
            "test_name": obs.get("DESCRIPTION", ""),
            "value": maybe_blank(obs.get("VALUE", ""), 0.03),
            "units": obs.get("UNITS", ""),
            "type": obs.get("TYPE", ""),
        }
        rows.append(row)

        # Inject ~3% duplicate lab results
        key = (obs["PATIENT"], obs["DATE"], obs["CODE"])
        if random.random() < 0.03 and key not in seen:
            seen.add(key)
            dup = dict(row)
            dup["date"] = corrupt_date(obs.get("DATE", ""))
            rows.append(dup)

    return rows


# ── Generate CSV: imaging_reports.csv ───────────────────────────────

IMAGING_REPORT_TEMPLATES = [
    "Findings: {bodysite}. {modality} performed. No acute abnormality identified.",
    "IMPRESSION: {modality} of {bodysite} shows no significant findings. Clinical correlation recommended.",
    "{modality} examination of {bodysite} completed. Findings within normal limits. No evidence of acute pathology.",
    "REPORT: {modality} - {bodysite}. Mild degenerative changes noted. No acute process. Follow-up as clinically indicated.",
    "Findings: {modality} imaging of {bodysite}. Study is technically adequate. No focal lesion identified.",
]


def build_imaging_csv(data: dict) -> list[dict]:
    rows = []
    for img in data["imaging"]:
        bodysite = img.get("BODYSITE_DESCRIPTION", "unspecified region")
        modality = img.get("MODALITY_DESCRIPTION", "Imaging")

        report = random.choice(IMAGING_REPORT_TEMPLATES).format(
            bodysite=bodysite, modality=modality
        )

        rows.append({
            "patient_id": img["PATIENT"],
            "encounter_id": img["ENCOUNTER"],
            "date": corrupt_date(img.get("DATE", "")),
            "modality": modality,
            "body_site": bodysite,
            "report_text": report,
        })

    return rows


# ── Generate CSV: clinical_notes.csv ────────────────────────────────

NOTE_TEMPLATES = {
    "discharge_summary": (
        "DISCHARGE SUMMARY\n"
        "Patient: {name}\n"
        "Admission Date: {admit}\n"
        "Discharge Date: {discharge}\n"
        "Encounter Type: {enc_type}\n\n"
        "CHIEF COMPLAINT: {reason}\n\n"
        "ACTIVE DIAGNOSES:\n{diagnoses}\n\n"
        "CURRENT MEDICATIONS:\n{medications}\n\n"
        "PLAN: Follow-up in 2-4 weeks. Continue current medications. "
        "Return to ED if symptoms worsen."
    ),
    "progress_note": (
        "PROGRESS NOTE\n"
        "Date: {date}\n"
        "Patient: {name}\n\n"
        "Subjective: Patient presents for {reason}. {enc_type}.\n"
        "Objective: Vitals stable. Physical exam unremarkable.\n"
        "Assessment: {diagnoses}\n"
        "Plan: Continue current management. Follow-up as scheduled."
    ),
}


def build_clinical_notes(data: dict) -> list[dict]:
    patients_by_id = {p["Id"]: p for p in data["patients"]}
    cond_by_patient = {}
    for c in data["conditions"]:
        cond_by_patient.setdefault(c["PATIENT"], []).append(c)
    med_by_patient = {}
    for m in data["medications"]:
        med_by_patient.setdefault(m["PATIENT"], []).append(m)

    rows = []
    for enc in data["encounters"]:
        # Only generate notes for inpatient/emergency/urgentcare encounters
        enc_class = enc.get("ENCOUNTERCLASS", "")
        if enc_class not in ("inpatient", "emergency", "urgentcare", "ambulatory"):
            continue
        # Skip ~40% of ambulatory to keep volume reasonable
        if enc_class == "ambulatory" and random.random() < 0.4:
            continue

        pid = enc["PATIENT"]
        patient = patients_by_id.get(pid, {})
        name = f"{patient.get('FIRST', '')} {patient.get('LAST', '')}"

        diags = cond_by_patient.get(pid, [])
        diag_text = "\n".join(
            f"- {d['DESCRIPTION']}" for d in diags[:5]
        ) or "- None documented"

        meds = med_by_patient.get(pid, [])
        med_text = "\n".join(
            f"- {m['DESCRIPTION']}" for m in meds[:5]
        ) or "- None documented"

        reason = enc.get("REASONDESCRIPTION", "") or enc.get("DESCRIPTION", "routine visit")

        if enc_class in ("inpatient", "emergency"):
            note_type = "discharge_summary"
        else:
            note_type = "progress_note"

        template = NOTE_TEMPLATES[note_type]
        text = template.format(
            name=name,
            admit=corrupt_date(enc.get("START", "")),
            discharge=corrupt_date(enc.get("STOP", "")),
            date=corrupt_date(enc.get("START", "")),
            enc_type=enc.get("DESCRIPTION", ""),
            reason=reason,
            diagnoses=diag_text,
            medications=med_text,
        )

        rows.append({
            "patient_id": pid,
            "encounter_id": enc["Id"],
            "date": corrupt_date(enc.get("START", "")),
            "note_type": note_type,
            "author": f"Dr. {random.choice(['Smith', 'Chen', 'Patel', 'Garcia', 'Johnson', 'Kim', 'Williams', 'Brown'])}",
            "text": text,
        })

    return rows


# ── Write outputs ───────────────────────────────────────────────────

def write_json(data, filename):
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Written: {filepath} ({len(data)} records)")


def write_csv_file(rows, filename):
    if not rows:
        print(f"  Skipped: {filename} (no data)")
        return
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {filepath} ({len(rows)} records)")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading Synthea data...")
    data = load_synthea_data()
    print(f"  Patients: {len(data['patients'])}")
    print(f"  Encounters: {len(data['encounters'])}")
    print(f"  Observations: {len(data['observations'])}")
    print(f"  Imaging: {len(data['imaging'])}")
    print(f"  Conditions: {len(data['conditions'])}")
    print(f"  Medications: {len(data['medications'])}")

    print("\nGenerating dirty EHR data...")

    print("\n[1/4] patients_ehr_export.json")
    patient_records = build_patient_json(data)
    write_json(patient_records, "patients_ehr_export.json")

    print("[2/4] lab_results.csv")
    lab_rows = build_lab_csv(data)
    write_csv_file(lab_rows, "lab_results.csv")

    print("[3/4] imaging_reports.csv")
    imaging_rows = build_imaging_csv(data)
    write_csv_file(imaging_rows, "imaging_reports.csv")

    print("[4/4] clinical_notes.csv")
    note_rows = build_clinical_notes(data)
    write_csv_file(note_rows, "clinical_notes.csv")

    # Print corruption summary
    print("\n── Injected Data Quality Issues ──")
    dup_patients = len(patient_records) - len(data["patients"])
    print(f"  Duplicate patients:         {dup_patients}")
    dup_labs = len(lab_rows) - len([o for o in data["observations"] if o.get("CATEGORY", "").lower() in LAB_CATEGORIES])
    print(f"  Duplicate lab results:      {dup_labs}")
    print(f"  Date format variants:       {len(DATE_FORMATS)}")
    print(f"  Gender code variants:       {sum(len(v) for v in GENDER_VARIANTS.values())}")
    print(f"  MRN format variants:        {len(MRN_FORMATS)}")
    print(f"  Random missing fields:      ~5-8% of DOB, gender, address, lab values")
    print("\nDone!")


if __name__ == "__main__":
    main()
