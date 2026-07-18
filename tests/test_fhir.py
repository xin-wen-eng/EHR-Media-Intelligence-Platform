"""
Tests for FHIR R4 mapping, validation, and storage.
"""

import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

from app.db.store import FHIRStore
from app.fhir.mapper import (
    build_patient_bundle,
    map_diagnostic_report,
    map_document_reference,
    map_encounter,
    map_patient,
)
from app.fhir.validator import validate_bundle
from app.ingestion.models import (
    CleanedClinicalNote,
    CleanedEncounter,
    CleanedImagingReport,
    CleanedLabResult,
    CleanedPatient,
    Gender,
)


def make_patient(**kwargs) -> CleanedPatient:
    defaults = {
        "patient_id": "test-001",
        "mrn": "MRN-TEST0001",
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": date(1990, 5, 15),
        "gender": Gender.FEMALE,
        "city": "Boston",
        "state": "MA",
        "zip_code": "02101",
    }
    defaults.update(kwargs)
    return CleanedPatient(**defaults)


def make_encounter(**kwargs) -> CleanedEncounter:
    defaults = {
        "encounter_id": "enc-001",
        "patient_id": "test-001",
        "start": datetime(2026, 7, 1, 10, 0),
        "end": datetime(2026, 7, 1, 11, 0),
        "encounter_class": "ambulatory",
        "encounter_type": "Office visit",
    }
    defaults.update(kwargs)
    return CleanedEncounter(**defaults)


def make_lab(**kwargs) -> CleanedLabResult:
    defaults = {
        "patient_id": "test-001",
        "encounter_id": "enc-001",
        "date": datetime(2026, 7, 1, 10, 30),
        "category": "laboratory",
        "code": "CBC",
        "test_name": "Complete Blood Count",
        "value": "12.3",
        "units": "g/dL",
    }
    defaults.update(kwargs)
    return CleanedLabResult(**defaults)


def make_note(**kwargs) -> CleanedClinicalNote:
    defaults = {
        "patient_id": "test-001",
        "encounter_id": "enc-001",
        "date": datetime(2026, 7, 1),
        "note_type": "progress_note",
        "author": "Dr. Smith",
        "text": "Patient presents with mild cough. Vitals stable.",
    }
    defaults.update(kwargs)
    return CleanedClinicalNote(**defaults)


class TestPatientMapping:
    def test_basic_patient(self):
        patient = make_patient()
        fhir = map_patient(patient)
        assert fhir.__resource_type__ == "Patient"
        assert fhir.id == "MRN-TEST0001"
        assert fhir.gender == "female"
        assert str(fhir.birthDate) == "1990-05-15"
        assert fhir.name[0].family == "Doe"
        assert fhir.name[0].given == ["Jane"]

    def test_patient_with_address(self):
        patient = make_patient(address="123 Main St")
        fhir = map_patient(patient)
        assert fhir.address[0].city == "Boston"
        assert fhir.address[0].line == ["123 Main St"]

    def test_unknown_gender(self):
        patient = make_patient(gender=Gender.UNKNOWN)
        fhir = map_patient(patient)
        assert fhir.gender == "unknown"

    def test_deceased_patient(self):
        patient = make_patient(death_date=date(2026, 1, 1))
        fhir = map_patient(patient)
        assert fhir.deceasedDateTime is not None


class TestEncounterMapping:
    def test_basic_encounter(self):
        enc = make_encounter()
        fhir = map_encounter(enc, "Patient/MRN-TEST0001")
        assert fhir.__resource_type__ == "Encounter"
        assert fhir.status == "finished"
        assert fhir.subject.reference == "Patient/MRN-TEST0001"

    def test_encounter_period(self):
        enc = make_encounter()
        fhir = map_encounter(enc, "Patient/MRN-TEST0001")
        assert fhir.actualPeriod is not None
        assert fhir.actualPeriod.start is not None


class TestDiagnosticReportMapping:
    def test_basic_report(self):
        lab = make_lab()
        fhir = map_diagnostic_report(lab, "Patient/MRN-TEST0001", "Encounter/enc-001")
        assert fhir.__resource_type__ == "DiagnosticReport"
        assert fhir.status == "final"
        assert fhir.subject.reference == "Patient/MRN-TEST0001"
        assert fhir.encounter.reference == "Encounter/enc-001"
        assert "Complete Blood Count" in fhir.conclusion

    def test_report_without_encounter(self):
        lab = make_lab()
        fhir = map_diagnostic_report(lab, "Patient/MRN-TEST0001")
        assert fhir.encounter is None


class TestDocumentReferenceMapping:
    def test_clinical_note(self):
        note = make_note()
        fhir = map_document_reference(note, "Patient/MRN-TEST0001", doc_type="progress_note")
        assert fhir.__resource_type__ == "DocumentReference"
        assert fhir.status == "current"
        assert fhir.subject.reference == "Patient/MRN-TEST0001"
        assert fhir.content[0].attachment.contentType == "text/plain"

    def test_imaging_report(self):
        img = CleanedImagingReport(
            patient_id="test-001",
            encounter_id="enc-001",
            date=datetime(2026, 7, 1),
            modality="Digital Radiography",
            body_site="Chest",
            report_text="No acute findings.",
        )
        fhir = map_document_reference(img, "Patient/MRN-TEST0001", doc_type="imaging")
        assert fhir.type.coding[0].code == "18748-4"


class TestBundleGeneration:
    def test_full_bundle(self):
        patient = make_patient()
        encounters = [make_encounter()]
        labs = [make_lab()]
        notes = [make_note()]
        imaging = []

        bundle = build_patient_bundle(patient, encounters, labs, notes, imaging)
        assert bundle.type == "transaction"
        assert len(bundle.entry) == 4  # Patient + Encounter + DiagnosticReport + DocumentReference

        resource_types = [e.resource.__resource_type__ for e in bundle.entry]
        assert "Patient" in resource_types
        assert "Encounter" in resource_types
        assert "DiagnosticReport" in resource_types
        assert "DocumentReference" in resource_types

    def test_empty_bundle(self):
        patient = make_patient()
        bundle = build_patient_bundle(patient, [], [], [], [])
        assert len(bundle.entry) == 1  # Patient only


class TestBundleValidation:
    def test_valid_bundle_passes(self):
        patient = make_patient()
        bundle = build_patient_bundle(patient, [make_encounter()], [make_lab()], [], [])
        result = validate_bundle(bundle, patient.mrn)
        assert result.is_valid
        assert result.resource_count == 3
        assert len(result.errors) == 0

    def test_resource_type_counts(self):
        patient = make_patient()
        bundle = build_patient_bundle(
            patient, [make_encounter()], [make_lab(), make_lab(code="BMP")], [make_note()], []
        )
        result = validate_bundle(bundle, patient.mrn)
        assert result.resource_types["Patient"] == 1
        assert result.resource_types["DiagnosticReport"] == 2
        assert result.resource_types["DocumentReference"] == 1


class TestFHIRStore:
    def test_save_and_retrieve(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        store = FHIRStore(db_path)
        patient = make_patient()
        bundle = build_patient_bundle(patient, [], [make_lab()], [], [])

        store.save_bundle(patient.mrn, bundle)
        assert store.count() == 1

        retrieved = store.get_bundle(patient.mrn)
        assert retrieved is not None
        assert retrieved.type == "transaction"
        assert len(retrieved.entry) == 2

        store.close()

    def test_list_patients(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        store = FHIRStore(db_path)
        for i in range(3):
            p = make_patient(mrn=f"MRN-TEST000{i}", patient_id=f"test-{i}")
            bundle = build_patient_bundle(p, [], [], [], [])
            store.save_bundle(p.mrn, bundle)

        patients = store.list_patients()
        assert len(patients) == 3
        store.close()
