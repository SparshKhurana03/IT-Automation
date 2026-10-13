"""Offline validation of the fields extracted from a ServiceDesk Plus ticket.

These checks need no network access. Directory checks (duplicate employee,
manager, license seats, group names) run afterwards in the validate step.
"""

from __future__ import annotations

import re
from typing import Any

from helpers.errors import ValidationError
from helpers.transformation import normalize_iso_datetime, sanitize_alias

ENTRA_FIELD_LIMITS = {
    "first_name": ("First Name", 64),
    "middle_name": ("Middle Name", 64),
    "last_name": ("Last Name", 64),
    "designation": ("Designation", 128),
    "department_name": ("Department Name", 64),
    "company_name": ("Company Name", 64),
    "staff_no": ("Staff Number", 64),
    "phone_number": ("Business Phone", 64),
    "mobile_phone": ("Mobile Phone", 64),
    "fax_number": ("Fax Number", 64),
    "location": ("Office Location", 128),
    "city": ("City", 128),
    "state_or_province": ("State / Province", 128),
    "postal_code": ("Postal Code", 40),
    "country_or_region": ("Country / Region", 128),
    "company_address": ("Street Address", 1024),
}


def is_email(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip()))


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)


def validate_input(fields: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Raise ``ValidationError`` listing every problem; return non-fatal warnings."""
    validation = config.get("validation", {})
    defaults = config.get("defaults", {})
    servicedesk = config.get("servicedesk", {})
    errors: list[str] = []
    warnings: list[str] = []

    # Required fields -------------------------------------------------------
    for field_name in validation.get("required_sdp_fields", []):
        if _is_blank(fields.get(field_name)):
            errors.append(f"Missing required SDP field: {field_name}")

    # Ticket status / request type -------------------------------------------
    approved_statuses = servicedesk.get("approved_statuses", [])
    status = fields.get("status")
    if approved_statuses and status not in approved_statuses:
        errors.append(f"Ticket status '{status}' is not approved. Allowed values: {', '.join(approved_statuses)}")

    accepted_request_types = servicedesk.get("accepted_request_for", [])
    request_for = fields.get("request_for")
    if accepted_request_types and request_for not in accepted_request_types:
        errors.append(
            f"Unsupported Request For value '{request_for}'. Allowed values: {', '.join(accepted_request_types)}"
        )

    # Manager ----------------------------------------------------------------
    reporting_manager = (fields.get("reporting_manager") or "").strip()
    if reporting_manager and not is_email(reporting_manager):
        errors.append("Reporting Manager must contain a valid email address or UPN")

    # Employee type ----------------------------------------------------------
    employee_type = (fields.get("employee_type") or "").strip()
    allowed_employee_types = validation.get("allowed_employee_types", [])
    if employee_type and allowed_employee_types and employee_type not in allowed_employee_types:
        errors.append(
            f"Unsupported employee type '{employee_type}'. Allowed values: {', '.join(allowed_employee_types)}"
        )

    # Usage location / country ---------------------------------------------
    usage_location = fields.get("usage_location") or defaults.get("usage_location")
    if usage_location and not re.fullmatch(r"[A-Z]{2}", str(usage_location).strip().upper()):
        errors.append("Usage Location must be a valid ISO country code (example: IN, US, GB)")

    country = fields.get("country_or_region") or defaults.get("country_or_region")
    if country is not None and not str(country).strip():
        errors.append("Country / Region cannot be blank")

    # Hire date --------------------------------------------------------------
    # New joiners are normally onboarded *before* their start date, so a future
    # hire date is accepted. Only the format is enforced.
    hire_date = fields.get("employee_hire_date")
    if hire_date:
        try:
            normalize_iso_datetime(hire_date)
        except ValidationError as exc:
            errors.append(str(exc))

    # Other e-mails ----------------------------------------------------------
    other_mails = fields.get("other_mails") or []
    if not isinstance(other_mails, list):
        errors.append("Other Emails must be a list")
    else:
        errors.extend(f"Invalid email address in Other Emails: {mail}" for mail in other_mails if not is_email(mail))

    # Alias / UPN / self-manager ---------------------------------------------
    if fields.get("first_name") and fields.get("last_name"):
        try:
            alias = sanitize_alias(fields["first_name"], fields["last_name"])
            domain = defaults.get("domain")
            if domain:
                upn = f"{alias}@{domain}"
                if len(upn) > 113:
                    errors.append("Generated User Principal Name exceeds 113 characters")
                if reporting_manager and reporting_manager.lower() == upn.lower():
                    errors.append("Employee cannot be assigned as their own manager")
        except ValidationError as exc:
            errors.append(str(exc))

    # Entra attribute lengths -----------------------------------------------
    for field_name, (label, max_length) in ENTRA_FIELD_LIMITS.items():
        value = fields.get(field_name)
        if isinstance(value, str) and len(value.strip()) > max_length:
            errors.append(f"{label} exceeds Entra maximum length ({max_length} characters)")

    # License ----------------------------------------------------------------
    license_cfg = config.get("license", {})
    license_field = license_cfg.get("source_field", "license_sku")
    if license_cfg.get("required", True) and not fields.get(license_field) and not license_cfg.get("default_skus"):
        errors.append(f"No license requested on the ticket ({license_field}) and no license.default_skus configured")

    # Group / bundle / app selections ---------------------------------------
    for category, settings in (config.get("assignments") or {}).items():
        values = fields.get(settings.get("source_field", category)) or []
        if settings.get("required", False) and not values and not settings.get("default_values"):
            errors.append(f"Ticket does not request any {settings.get('label', category)} ({category})")
        elif not values and not settings.get("default_values"):
            warnings.append(f"No {settings.get('label', category)} requested on the ticket")

    if errors:
        raise ValidationError("\n".join(errors))
    return warnings
