"""
FHIR R4 normalization pipeline.

Reads cleaned data → maps to FHIR resources → validates → stores in SQLite.
"""

import json
from pathlib import Path

from app.db.store import FHIRStore
from app.ingestion.models import (
    CleanedClinicalNote,
    CleanedEncounter,
    CleanedImagingReport,
    CleanedLabResult,
    CleanedPatient,
)
from .mapper import build_patient_bundle
from .validator import ValidationResult, generate_validation_report, validate_bundle

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CLEANED_DIR = DATA_DIR / "cleaned"
FHIR_DIR = DATA_DIR / "fhir"


def _load_cleaned(filename: str, model_class):
    path = CLEANED_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [model_class.model_validate(r) for r in data]


def _group_by_patient(records, key="patient_id") -> dict[str, list]:
    groups: dict[str, list] = {}
    for r in records:
        pid = getattr(r, key)
        groups.setdefault(pid, []).append(r)
    return groups


def run_fhir_pipeline():
    print("=" * 60)
    print("FHIR R4 Normalization Pipeline")
    print("=" * 60)

    print("\nLoading cleaned data...")
    patients = _load_cleaned("patients.json", CleanedPatient)
    encounters = _load_cleaned("encounters.json", CleanedEncounter)
    labs = _load_cleaned("lab_results.json", CleanedLabResult)
    notes = _load_cleaned("clinical_notes.json", CleanedClinicalNote)
    imaging = _load_cleaned("imaging_reports.json", CleanedImagingReport)

    print(f"  Patients:    {len(patients)}")
    print(f"  Encounters:  {len(encounters)}")
    print(f"  Labs:        {len(labs)}")
    print(f"  Notes:       {len(notes)}")
    print(f"  Imaging:     {len(imaging)}")

    enc_by_patient = _group_by_patient(encounters)
    labs_by_patient = _group_by_patient(labs)
    notes_by_patient = _group_by_patient(notes)
    imaging_by_patient = _group_by_patient(imaging)

    FHIR_DIR.mkdir(parents=True, exist_ok=True)
    store = FHIRStore()
    validation_results: list[ValidationResult] = []

    print("\nGenerating FHIR Bundles...")
    for patient in patients:
        pid = patient.patient_id

        bundle = build_patient_bundle(
            patient=patient,
            encounters=enc_by_patient.get(pid, []),
            labs=labs_by_patient.get(pid, []),
            notes=notes_by_patient.get(pid, []),
            imaging=imaging_by_patient.get(pid, []),
        )

        result = validate_bundle(bundle, patient.mrn)
        validation_results.append(result)

        bundle_path = FHIR_DIR / f"{patient.mrn}.json"
        with open(bundle_path, "w", encoding="utf-8") as f:
            f.write(bundle.model_dump_json(indent=2))

        store.save_bundle(patient.mrn, bundle)

        status = "✓" if result.is_valid else "✗"
        types = ", ".join(f"{k}:{v}" for k, v in result.resource_types.items())
        print(f"  {status} {patient.mrn}: {result.resource_count} resources ({types})")

    report = generate_validation_report(
        validation_results,
        output_path=DATA_DIR / "fhir" / "validation_report.txt",
    )
    print(f"\n{report}")

    print(f"\nStored {store.count()} bundles in SQLite: {store.db_path}")
    store.close()

    return validation_results


if __name__ == "__main__":
    run_fhir_pipeline()
