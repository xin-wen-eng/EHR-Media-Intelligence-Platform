"""
Data cleaning and normalization logic.

Handles:
- Date format normalization → ISO 8601
- Gender code normalization → canonical enum
- MRN format normalization → MRN-XXXXXXXX
- Missing field detection
- Duplicate record detection
"""

from datetime import date, datetime
from .models import Gender, AuditLog

DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%d/%m/%Y",
]

GENDER_MAP: dict[str, Gender] = {
    "m": Gender.MALE,
    "male": Gender.MALE,
    "1": Gender.MALE,
    "f": Gender.FEMALE,
    "female": Gender.FEMALE,
    "2": Gender.FEMALE,
    "other": Gender.OTHER,
    "o": Gender.OTHER,
    "3": Gender.OTHER,
}


def normalize_date(value: str, audit: AuditLog, field: str) -> date | None:
    if not value or not value.strip():
        audit.add_warning(f"Missing {field}")
        return None

    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            result = dt.date()
            iso = result.isoformat()
            if value != iso:
                audit.add_change(field, "date_normalized", value, iso)
            return result
        except ValueError:
            continue

    audit.add_warning(f"Unparseable {field}: {value}")
    return None


def normalize_datetime(value: str, audit: AuditLog, field: str) -> datetime | None:
    if not value or not value.strip():
        audit.add_warning(f"Missing {field}")
        return None

    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            iso = dt.isoformat()
            if value != iso:
                audit.add_change(field, "datetime_normalized", value, iso)
            return dt
        except ValueError:
            continue

    audit.add_warning(f"Unparseable {field}: {value}")
    return None


def normalize_gender(value: str, audit: AuditLog) -> Gender:
    if not value or not value.strip():
        audit.add_warning("Missing gender")
        return Gender.UNKNOWN

    original = value.strip()
    key = original.lower()
    gender = GENDER_MAP.get(key, Gender.UNKNOWN)

    if gender == Gender.UNKNOWN and key:
        audit.add_warning(f"Unrecognized gender code: {original}")
    elif original != gender.value:
        audit.add_change("gender", "gender_normalized", original, gender.value)

    return gender


def normalize_mrn(value: str, audit: AuditLog) -> str:
    if not value or not value.strip():
        audit.add_warning("Missing MRN")
        return ""

    original = value.strip()

    raw = original.upper().replace("MRN-", "").replace("MRN", "")
    raw = raw.replace("-", "").replace(" ", "")
    normalized = f"MRN-{raw[:8].upper()}"

    if original != normalized:
        audit.add_change("mrn", "mrn_normalized", original, normalized)

    return normalized


def normalize_name(value: str) -> str:
    if not value:
        return ""
    return value.strip().title()


class DuplicateDetector:
    def __init__(self):
        self._seen_patients: dict[str, str] = {}
        self._seen_labs: set[tuple] = set()

    def check_patient(self, mrn: str, first: str, last: str, dob: date | None) -> tuple[bool, str | None]:
        key = (first.lower(), last.lower(), dob.isoformat() if dob else "")
        if key in self._seen_patients:
            return True, self._seen_patients[key]
        self._seen_patients[key] = mrn
        return False, None

    def check_lab(self, patient_id: str, dt: datetime | None, code: str) -> bool:
        key = (patient_id, dt.isoformat() if dt else "", code)
        if key in self._seen_labs:
            return True
        self._seen_labs.add(key)
        return False
