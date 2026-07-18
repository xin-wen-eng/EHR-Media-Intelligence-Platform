"""
Canonical Pydantic models for cleaned EHR data.

These serve as the validated intermediate representation between
raw ingestion and FHIR mapping.
"""

from datetime import date, datetime
from enum import Enum
from pydantic import BaseModel, Field


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class AuditEntry(BaseModel):
    field: str
    action: str
    original_value: str = ""
    cleaned_value: str = ""


class AuditLog(BaseModel):
    record_id: str
    record_type: str
    timestamp: datetime = Field(default_factory=datetime.now)
    changes: list[AuditEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of: str | None = None

    def add_change(self, field: str, action: str, original: str, cleaned: str):
        self.changes.append(AuditEntry(
            field=field, action=action,
            original_value=original, cleaned_value=cleaned,
        ))

    def add_warning(self, msg: str):
        self.warnings.append(msg)


class CleanedPatient(BaseModel):
    patient_id: str
    mrn: str
    first_name: str
    last_name: str
    middle_name: str = ""
    date_of_birth: date | None = None
    gender: Gender = Gender.UNKNOWN
    race: str = ""
    ethnicity: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    marital_status: str = ""
    death_date: date | None = None
    audit: AuditLog | None = None


class CleanedEncounter(BaseModel):
    encounter_id: str
    patient_id: str
    start: datetime | None = None
    end: datetime | None = None
    encounter_class: str = ""
    encounter_type: str = ""
    reason: str = ""


class CleanedLabResult(BaseModel):
    patient_id: str
    encounter_id: str
    date: datetime | None = None
    category: str = ""
    code: str = ""
    test_name: str = ""
    value: str = ""
    units: str = ""
    value_type: str = ""
    audit: AuditLog | None = None


class CleanedImagingReport(BaseModel):
    patient_id: str
    encounter_id: str
    date: datetime | None = None
    modality: str = ""
    body_site: str = ""
    report_text: str = ""
    audit: AuditLog | None = None


class CleanedClinicalNote(BaseModel):
    patient_id: str
    encounter_id: str
    date: datetime | None = None
    note_type: str = ""
    author: str = ""
    text: str = ""
    audit: AuditLog | None = None
