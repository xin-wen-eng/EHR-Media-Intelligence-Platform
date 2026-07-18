"""
Map cleaned EHR records to FHIR R4 resources.

Produces: Patient, Encounter, DiagnosticReport, DocumentReference.
All resources use correct subject/encounter references.
"""

import base64
import uuid
from datetime import datetime, timezone

from fhir.resources.attachment import Attachment
from fhir.resources.bundle import Bundle, BundleEntry, BundleEntryRequest
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.coding import Coding
from fhir.resources.diagnosticreport import DiagnosticReport
from fhir.resources.documentreference import (
    DocumentReference,
    DocumentReferenceContent,
)
from fhir.resources.encounter import Encounter
from fhir.resources.humanname import HumanName
from fhir.resources.address import Address
from fhir.resources.patient import Patient
from fhir.resources.period import Period
from fhir.resources.reference import Reference

from app.ingestion.models import (
    CleanedClinicalNote,
    CleanedEncounter,
    CleanedImagingReport,
    CleanedLabResult,
    CleanedPatient,
    Gender,
)


ENCOUNTER_CLASS_MAP = {
    "ambulatory": ("AMB", "ambulatory"),
    "emergency": ("EMER", "emergency"),
    "inpatient": ("IMP", "inpatient encounter"),
    "wellness": ("AMB", "ambulatory"),
    "urgentcare": ("AMB", "ambulatory"),
    "outpatient": ("AMB", "ambulatory"),
}


def _make_id() -> str:
    return str(uuid.uuid4())


def _fhir_datetime(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def map_patient(cleaned: CleanedPatient) -> Patient:
    name_parts = {"family": cleaned.last_name}
    given = [cleaned.first_name]
    if cleaned.middle_name:
        given.append(cleaned.middle_name)
    name_parts["given"] = given

    gender_map = {
        Gender.MALE: "male",
        Gender.FEMALE: "female",
        Gender.OTHER: "other",
        Gender.UNKNOWN: "unknown",
    }

    patient_data = {
        "id": cleaned.mrn,
        "identifier": [{
            "system": "urn:oid:2.16.840.1.113883.19.5",
            "value": cleaned.mrn,
        }],
        "name": [HumanName(**name_parts)],
        "gender": gender_map[cleaned.gender],
    }

    if cleaned.date_of_birth:
        patient_data["birthDate"] = cleaned.date_of_birth.isoformat()

    if cleaned.death_date:
        patient_data["deceasedDateTime"] = cleaned.death_date.isoformat()

    if cleaned.address or cleaned.city:
        addr = {}
        if cleaned.address:
            addr["line"] = [cleaned.address]
        if cleaned.city:
            addr["city"] = cleaned.city
        if cleaned.state:
            addr["state"] = cleaned.state
        if cleaned.zip_code:
            addr["postalCode"] = cleaned.zip_code
        patient_data["address"] = [Address(**addr)]

    if cleaned.marital_status:
        patient_data["maritalStatus"] = CodeableConcept(
            text=cleaned.marital_status
        )

    return Patient(**patient_data)


def map_encounter(cleaned: CleanedEncounter, patient_ref: str) -> Encounter:
    class_code, class_display = ENCOUNTER_CLASS_MAP.get(
        cleaned.encounter_class, ("AMB", "ambulatory")
    )

    enc_data: dict = {
        "id": cleaned.encounter_id,
        "status": "finished",
        "class_fhir": [CodeableConcept(
            coding=[Coding(
                system="http://terminology.hl7.org/CodeSystem/v3-ActCode",
                code=class_code,
                display=class_display,
            )]
        )],
        "subject": Reference(reference=patient_ref),
    }

    if cleaned.encounter_type:
        enc_data["type"] = [CodeableConcept(text=cleaned.encounter_type)]

    if cleaned.start or cleaned.end:
        period = {}
        if cleaned.start:
            period["start"] = _fhir_datetime(cleaned.start)
        if cleaned.end:
            period["end"] = _fhir_datetime(cleaned.end)
        enc_data["actualPeriod"] = Period(**period)

    if cleaned.reason:
        enc_data["reason"] = [{"use": [CodeableConcept(text=cleaned.reason)]}]

    return Encounter(**enc_data)


def map_diagnostic_report(
    lab: CleanedLabResult,
    patient_ref: str,
    encounter_ref: str | None = None,
) -> DiagnosticReport:
    report_data: dict = {
        "id": _make_id(),
        "status": "final",
        "code": CodeableConcept(
            coding=[Coding(
                system="http://loinc.org",
                code=lab.code,
                display=lab.test_name,
            )],
            text=lab.test_name,
        ),
        "subject": Reference(reference=patient_ref),
    }

    if encounter_ref:
        report_data["encounter"] = Reference(reference=encounter_ref)

    if lab.date:
        report_data["effectiveDateTime"] = _fhir_datetime(lab.date)

    conclusion_parts = []
    if lab.test_name:
        conclusion_parts.append(lab.test_name)
    if lab.value:
        result_str = lab.value
        if lab.units:
            result_str += f" {lab.units}"
        conclusion_parts.append(result_str)
    if conclusion_parts:
        report_data["conclusion"] = ": ".join(conclusion_parts)

    if lab.category:
        report_data["category"] = [CodeableConcept(
            coding=[Coding(
                system="http://terminology.hl7.org/CodeSystem/v2-0074",
                code=lab.category.upper(),
                display=lab.category,
            )]
        )]

    return DiagnosticReport(**report_data)


def map_document_reference(
    note: CleanedClinicalNote | CleanedImagingReport,
    patient_ref: str,
    encounter_ref: str | None = None,
    doc_type: str = "clinical_note",
) -> DocumentReference:
    if isinstance(note, CleanedClinicalNote):
        text = note.text
        type_display = note.note_type.replace("_", " ").title()
    else:
        text = note.report_text
        type_display = f"{note.modality} - {note.body_site}"

    type_code_map = {
        "discharge_summary": ("18842-5", "Discharge summary"),
        "progress_note": ("11506-3", "Progress note"),
        "imaging": ("18748-4", "Diagnostic imaging study"),
    }
    code, display = type_code_map.get(
        doc_type, ("34109-9", "Note")
    )

    doc_data: dict = {
        "id": _make_id(),
        "status": "current",
        "type": CodeableConcept(
            coding=[Coding(
                system="http://loinc.org",
                code=code,
                display=display,
            )],
            text=type_display,
        ),
        "subject": Reference(reference=patient_ref),
        "content": [DocumentReferenceContent(
            attachment=Attachment(
                contentType="text/plain",
                data=base64.b64encode(text.encode("utf-8")),
            )
        )],
    }

    if encounter_ref:
        doc_data["context"] = [Reference(reference=encounter_ref)]

    if note.date:
        doc_data["date"] = _fhir_datetime(note.date)

    if isinstance(note, CleanedClinicalNote) and note.author:
        doc_data["author"] = [Reference(display=note.author)]

    return DocumentReference(**doc_data)


def build_patient_bundle(
    patient: CleanedPatient,
    encounters: list[CleanedEncounter],
    labs: list[CleanedLabResult],
    notes: list[CleanedClinicalNote],
    imaging: list[CleanedImagingReport],
) -> Bundle:
    entries: list[BundleEntry] = []

    fhir_patient = map_patient(patient)
    patient_ref = f"Patient/{fhir_patient.id}"
    entries.append(BundleEntry(
        fullUrl=f"urn:uuid:{fhir_patient.id}",
        resource=fhir_patient,
        request=BundleEntryRequest(method="PUT", url=patient_ref),
    ))

    encounter_map: dict[str, str] = {}
    for enc in encounters:
        fhir_enc = map_encounter(enc, patient_ref)
        enc_ref = f"Encounter/{fhir_enc.id}"
        encounter_map[enc.encounter_id] = enc_ref
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{fhir_enc.id}",
            resource=fhir_enc,
            request=BundleEntryRequest(method="PUT", url=enc_ref),
        ))

    for lab in labs:
        enc_ref = encounter_map.get(lab.encounter_id)
        fhir_report = map_diagnostic_report(lab, patient_ref, enc_ref)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{fhir_report.id}",
            resource=fhir_report,
            request=BundleEntryRequest(method="PUT", url=f"DiagnosticReport/{fhir_report.id}"),
        ))

    for note in notes:
        enc_ref = encounter_map.get(note.encounter_id)
        fhir_doc = map_document_reference(note, patient_ref, enc_ref, doc_type=note.note_type)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{fhir_doc.id}",
            resource=fhir_doc,
            request=BundleEntryRequest(method="PUT", url=f"DocumentReference/{fhir_doc.id}"),
        ))

    for img in imaging:
        enc_ref = encounter_map.get(img.encounter_id)
        fhir_doc = map_document_reference(img, patient_ref, enc_ref, doc_type="imaging")
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{fhir_doc.id}",
            resource=fhir_doc,
            request=BundleEntryRequest(method="PUT", url=f"DocumentReference/{fhir_doc.id}"),
        ))

    return Bundle(
        id=_make_id(),
        type="transaction",
        entry=entries,
    )
