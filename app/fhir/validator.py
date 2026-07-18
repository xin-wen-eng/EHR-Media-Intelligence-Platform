"""
FHIR R4 Bundle validation and error reporting.

Uses fhir.resources' built-in Pydantic validation to check
all Bundles against the FHIR R4 specification.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from fhir.resources.bundle import Bundle


@dataclass
class ValidationResult:
    patient_mrn: str
    is_valid: bool
    resource_count: int
    resource_types: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def validate_bundle(bundle: Bundle, patient_mrn: str) -> ValidationResult:
    result = ValidationResult(
        patient_mrn=patient_mrn,
        is_valid=True,
        resource_count=len(bundle.entry) if bundle.entry else 0,
    )

    if not bundle.entry:
        result.is_valid = False
        result.errors.append("Bundle has no entries")
        return result

    for entry in bundle.entry:
        if not entry.resource:
            result.errors.append(f"Entry missing resource: {entry.fullUrl}")
            result.is_valid = False
            continue

        rtype = entry.resource.__resource_type__
        result.resource_types[rtype] = result.resource_types.get(rtype, 0) + 1

        try:
            entry.resource.__class__.model_validate(
                entry.resource.model_dump()
            )
        except ValidationError as e:
            result.is_valid = False
            for err in e.errors():
                loc = " → ".join(str(l) for l in err["loc"])
                result.errors.append(
                    f"{rtype}: {loc}: {err['msg']}"
                )

    return result


def validate_bundle_from_json(json_str: str, patient_mrn: str) -> ValidationResult:
    try:
        bundle = Bundle.model_validate_json(json_str)
        return validate_bundle(bundle, patient_mrn)
    except ValidationError as e:
        result = ValidationResult(
            patient_mrn=patient_mrn,
            is_valid=False,
            resource_count=0,
        )
        for err in e.errors():
            loc = " → ".join(str(l) for l in err["loc"])
            result.errors.append(f"Bundle: {loc}: {err['msg']}")
        return result


def generate_validation_report(
    results: list[ValidationResult],
    output_path: Path | None = None,
) -> str:
    total = len(results)
    valid = sum(1 for r in results if r.is_valid)
    invalid = total - valid

    lines = [
        "=" * 60,
        "FHIR R4 Validation Report",
        "=" * 60,
        f"Total Bundles:    {total}",
        f"Valid:            {valid}",
        f"Invalid:          {invalid}",
        "",
    ]

    all_types: dict[str, int] = {}
    for r in results:
        for rtype, count in r.resource_types.items():
            all_types[rtype] = all_types.get(rtype, 0) + count

    lines.append("Resource Type Summary:")
    for rtype, count in sorted(all_types.items()):
        lines.append(f"  {rtype}: {count}")
    lines.append("")

    if invalid > 0:
        lines.append("Validation Errors:")
        lines.append("-" * 40)
        for r in results:
            if not r.is_valid:
                lines.append(f"\n  Patient: {r.patient_mrn}")
                for err in r.errors:
                    lines.append(f"    ✗ {err}")
    else:
        lines.append("All Bundles passed FHIR R4 validation.")

    report = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)

        report_data = []
        for r in results:
            report_data.append({
                "patient_mrn": r.patient_mrn,
                "is_valid": r.is_valid,
                "resource_count": r.resource_count,
                "resource_types": r.resource_types,
                "errors": r.errors,
            })
        json_path = output_path.with_suffix(".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

    return report
