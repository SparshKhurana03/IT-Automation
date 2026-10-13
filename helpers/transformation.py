"""Field extraction from ServiceDesk Plus and derivation of Entra attributes."""

from __future__ import annotations

import json
import re
import secrets
import string
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Iterable

from helpers.errors import ConfigurationError, ValidationError

# --------------------------------------------------------------------------
# Generic value helpers
# --------------------------------------------------------------------------


def get_by_path(data: Any, path: str, default: Any = None) -> Any:
    """Resolve dotted paths, including numeric list indexes."""
    current = data
    for token in path.split("."):
        if isinstance(current, dict):
            if token not in current:
                return default
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return default
            current = current[index]
        else:
            return default
    return current


def compact_text(value: Any) -> str | None:
    """Collapse an SDP value (string, number or ``{"name": ...}`` object) to text."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "name", "display_value", "email_id", "email"):
            if key in value:
                return compact_text(value[key])
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def parse_multi_value(value: Any) -> list[str]:
    """Normalise an SDP multi-select / multi-line value into a de-duplicated list.

    SDP v3 returns multi-select UDFs as JSON arrays; free-text fields are split
    on commas, semicolons and new lines.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            items.extend(parse_multi_value(item))
    elif isinstance(value, dict):
        text = compact_text(value)
        items = [text] if text else []
    else:
        items = [compact_text(part) or "" for part in re.split(r"[,;\r\n]+", str(value))]

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return result


def extract_source_fields(ticket: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    source = mapping.get("source", {})
    field_paths = source.get("fields", {})
    if not isinstance(field_paths, dict) or not field_paths:
        raise ConfigurationError("source.fields is missing from mapping YAML")
    list_fields = set(source.get("list_fields", [])) | {"other_mails"}
    raw_text_fields = set(source.get("raw_text_fields", ["description"]))

    extracted: dict[str, Any] = {}
    for logical_name, source_path in field_paths.items():
        value = get_by_path(ticket, str(source_path))
        if logical_name in list_fields:
            value = parse_multi_value(value)
        elif logical_name not in raw_text_fields:
            value = compact_text(value)
        extracted[logical_name] = value
    return extracted


def join_non_empty(values: Iterable[Any], separator: str) -> str:
    return separator.join(str(value).strip() for value in values if value is not None and str(value).strip())


def remove_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {k: remove_nulls(v) for k, v in value.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v != {}}
    if isinstance(value, list):
        return [remove_nulls(item) for item in value if item is not None]
    return value


def normalize_iso_datetime(value: str) -> str:
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Employee Hire Date must be ISO 8601, for example 2026-10-01") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Alias / e-mail generation
# --------------------------------------------------------------------------


def ascii_text(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def sanitize_alias_part(value: str) -> str:
    part = re.sub(r"[^a-z0-9._-]", "", ascii_text(value or "").lower())
    part = re.sub(r"[._-]{2,}", ".", part)
    return part.strip("._-")


def sanitize_alias(first_name: str, last_name: str) -> str:
    alias = sanitize_alias_part(f"{first_name}.{last_name}")
    if not alias:
        raise ValidationError("Unable to derive mail nickname from employee name")
    return alias[:64]


def email_candidates(first_name: str, last_name: str, employee_code: str, domain: str, separator: str = "") -> list[str]:
    """Return the ordered aliases to try: ``first.last`` then ``first.last<employee code>``."""
    base = sanitize_alias(first_name, last_name)
    code = sanitize_alias_part(employee_code or "")
    if not code:
        raise ValidationError("Employee code (staff_no) is required to build the fallback e-mail address")
    with_code = f"{base}{separator}{code}"
    if len(with_code) > 64:
        # Trim the name part, never the employee code, so the fallback stays unique.
        with_code = f"{base[: 64 - len(separator) - len(code)].rstrip('._-')}{separator}{code}"
    return [base, with_code]


def choose_available_alias(candidates: list[str], domain: str, taken_addresses: set[str]) -> tuple[str, str]:
    """Pick the first candidate whose ``alias@domain`` is not already used in Entra."""
    for alias in candidates:
        address = f"{alias}@{domain}".lower()
        if address not in taken_addresses:
            return alias, address
    tried = ", ".join(f"{alias}@{domain}" for alias in candidates)
    raise ValidationError(
        f"All generated e-mail addresses are already in use in Entra ({tried}). "
        "Resolve the conflict manually or correct the ticket."
    )


# --------------------------------------------------------------------------
# Password
# --------------------------------------------------------------------------

PASSWORD_SPECIALS = "!@#$%^&*()-_=+[]{}?"


def generate_password(length: int = 24) -> str:
    if length < 16:
        raise ConfigurationError("Generated password length must be at least 16")
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits + PASSWORD_SPECIALS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in PASSWORD_SPECIALS for c in password)
        ):
            return password


# --------------------------------------------------------------------------
# Graph payload
# --------------------------------------------------------------------------


def derive_values(fields: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    first = fields["first_name"].strip()
    last = fields["last_name"].strip()
    alias = sanitize_alias(first, last)
    domain = defaults["domain"]
    business_phone = fields.get("phone_number")
    hire_date = fields.get("employee_hire_date")
    return {
        "display_name": join_non_empty([first, fields.get("middle_name"), last], " "),
        "mail_nickname": alias,
        "upn": f"{alias}@{domain}",
        "job_title": fields.get("designation"),
        "employee_hire_date": normalize_iso_datetime(hire_date) if hire_date else None,
        "usage_location": str(fields.get("usage_location") or defaults.get("usage_location", "IN")).upper(),
        "country": fields.get("country_or_region") or defaults.get("country_or_region"),
        "office_location": fields.get("location") or fields.get("site"),
        "street_address": fields.get("company_address"),
        "business_phones": [business_phone] if business_phone else [],
        "preferred_language": fields.get("preferred_language") or defaults.get("preferred_language"),
    }


def build_user_payload(
    fields: dict[str, Any], derived: dict[str, Any], config: dict[str, Any], password: str
) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    extension_map = config.get("extension_attributes", {}) or {}
    extensions = {target: fields.get(source) for target, source in extension_map.items()}

    payload = {
        "accountEnabled": bool(defaults.get("account_enabled", True)),
        "displayName": derived["display_name"],
        "givenName": fields["first_name"],
        "surname": fields["last_name"],
        "mailNickname": derived["mail_nickname"],
        "userPrincipalName": derived["upn"],
        "mail": derived["upn"],
        "passwordProfile": {
            "password": password,
            "forceChangePasswordNextSignIn": bool(
                config.get("password", {}).get("force_change_password_next_sign_in", True)
            ),
        },
        "userType": defaults.get("user_type", "Member"),
        "jobTitle": derived["job_title"],
        "companyName": fields.get("company_name"),
        "department": fields.get("department_name"),
        "employeeId": fields.get("staff_no"),
        "employeeType": fields.get("employee_type"),
        "employeeHireDate": derived["employee_hire_date"],
        "officeLocation": derived["office_location"],
        "streetAddress": derived["street_address"],
        "city": fields.get("city"),
        "state": fields.get("state_or_province"),
        "postalCode": fields.get("postal_code"),
        "country": derived["country"],
        "businessPhones": derived["business_phones"],
        "mobilePhone": fields.get("mobile_phone"),
        "otherMails": fields.get("other_mails") or [],
        "faxNumber": fields.get("fax_number"),
        "ageGroup": fields.get("age_group"),
        "consentProvidedForMinor": fields.get("consent_provided_for_minor"),
        "usageLocation": derived["usage_location"],
        "preferredLanguage": derived["preferred_language"],
        "onPremisesExtensionAttributes": extensions or None,
    }
    return remove_nulls(payload)


def redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(payload))
    if "passwordProfile" in copy:
        copy["passwordProfile"]["password"] = "***REDACTED***"
    return copy
