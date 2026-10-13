#!/usr/bin/env python3
"""ServiceDesk Plus to Microsoft Entra employee onboarding automation.

Usage:
    python employee_lifecycle.py --ticket-id 12345 --config onboarding.yaml
    python employee_lifecycle.py --ticket-id 12345 --config onboarding.yaml --dry-run
    python employee_lifecycle.py --input-file sample-ticket.json --config onboarding.yaml --dry-run

23EEEA1E-C69C-4F07-843D-1E0BAA627CAC SDP API TOKEN
Required environment variables:
    ENTRA_TENANT_ID
    ENTRA_CLIENT_ID
    ENTRA_CLIENT_SECRET
    SDP_USERNAME / SDP_PASSWORD when ServiceDesk auth is basic

The script:
  1. Loads onboarding.yaml and its external mapping YAML.
  2. Retrieves a ServiceDesk Plus ticket or reads a saved JSON payload.
  3. Extracts and validates the configured fields.
  4. Derives the Entra user attributes.
  5. Checks employee ID and UPN uniqueness.
  6. Creates the user through Microsoft Graph.
  7. Resolves and assigns the reporting manager.
  8. Updates the ServiceDesk ticket with the outcome when configured.

Passwords and access tokens are never written to normal logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import string
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import msal
import requests
import yaml


LOG = logging.getLogger("employee_lifecycle")
MISSING = object()


class LifecycleError(Exception):
    """Base lifecycle error."""


class ConfigurationError(LifecycleError):
    """Invalid or incomplete configuration."""


class ValidationError(LifecycleError):
    """ServiceDesk request failed business validation."""


class ApiError(LifecycleError):
    """Remote system call failed."""

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass
class RuntimeContext:
    ticket_id: str
    correlation_id: str
    idempotency_key: str
    dry_run: bool
    created_user_id: str | None = None
    created_upn: str | None = None
    manager_id: str | None = None


def load_yaml(path: Path) -> dict[str, Any\]:
    """
    Load and validate a YAML configuration file.
    """

    if not path.exists():
        raise ConfigurationError(
            f"Employee mapping configuration file not found: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle)

    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML syntax in file: {path}"
        ) from exc

    except OSError as exc:
        raise ConfigurationError(
            f"Unable to read configuration file: {path}"
        ) from exc

    if value is None:
        raise ConfigurationError(
            f"Configuration file is empty: {path}"
        )

    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Expected a YAML object at the root of {path}"
        )

    return value


def load_configuration(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_yaml(config_path)
    mapping_ref = config.get("configuration", {}).get("field_mapping_file")
    if not mapping_ref:
        raise ConfigurationError("configuration.field_mapping_file is required")
    mapping_path = (config_path.parent / mapping_ref).resolve()
    mapping = load_yaml(mapping_path)
    return config, mapping


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigurationError(f"Required environment variable is not set: {name}")
    return value


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
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "name", "display_value", "email_id", "email"):
            if key in value:
                return compact_text(value[key])
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def extract_source_fields(ticket: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    field_paths = mapping.get("source", {}).get("fields", {})
    if not isinstance(field_paths, dict) or not field_paths:
        raise ConfigurationError("source.fields is missing from mapping YAML")
    extracted: dict[str, Any] = {}
    for logical_name, source_path in field_paths.items():
        value = get_by_path(ticket, str(source_path))
        if logical_name == "other_mails":
            if isinstance(value, str):
                value = [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
            elif value is None:
                value = []
        elif logical_name not in {"description"}:
            value = compact_text(value)
        extracted[logical_name] = value
    return extracted


def is_email(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip()))


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


def validate_input(fields: dict[str, Any], config: dict[str, Any]) -> None:
    validation = config.get("validation", {})
    defaults = config.get("defaults", {})
    servicedesk = config.get("servicedesk", {})

    errors: list[str] = []

    # ---------------------------------------------------------
    # Required field validation
    # ---------------------------------------------------------
    for field_name in validation.get("required_sdp_fields", []):
        value = fields.get(field_name)

        if value is None:
            errors.append(f"Missing required SDP field: {field_name}")
            continue

        if isinstance(value, str) and not value.strip():
            errors.append(f"Missing required SDP field: {field_name}")
            continue

        if isinstance(value, list) and not value:
            errors.append(f"Missing required SDP field: {field_name}")

    # ---------------------------------------------------------
    # SDP Status Validation
    # ---------------------------------------------------------
    approved_statuses = servicedesk.get("approved_statuses", [])

    status = fields.get("status")
    if approved_statuses and status not in approved_statuses:
        errors.append(
            f"Ticket status '{status}' is not approved. "
            f"Allowed values: {', '.join(approved_statuses)}"
        )

    # ---------------------------------------------------------
    # Request Type Validation
    # ---------------------------------------------------------
    accepted_request_types = servicedesk.get(
        "accepted_request_for",
        []
    )

    request_for = fields.get("request_for")

    if accepted_request_types and request_for not in accepted_request_types:
        errors.append(
            f"Unsupported Request For value '{request_for}'. "
            f"Allowed values: {', '.join(accepted_request_types)}"
        )

    # ---------------------------------------------------------
    # Manager Validation
    # ---------------------------------------------------------
    reporting_manager = fields.get("reporting_manager")

    if reporting_manager:
        reporting_manager = reporting_manager.strip()

        if not is_email(reporting_manager):
            errors.append(
                "Reporting Manager must contain a valid email address or UPN"
            )

    # ---------------------------------------------------------
    # Employee Type Validation
    # ---------------------------------------------------------
    employee_type = fields.get("employee_type")

    allowed_employee_types = validation.get(
        "allowed_employee_types",
        []
    )

    if employee_type:
        employee_type = employee_type.strip()

        if (
            allowed_employee_types
            and employee_type not in allowed_employee_types
        ):
            errors.append(
                f"Unsupported employee type '{employee_type}'. "
                f"Allowed values: {', '.join(allowed_employee_types)}"
            )

    # ---------------------------------------------------------
    # Usage Location Validation
    # ---------------------------------------------------------
    usage_location = (
        fields.get("usage_location")
        or defaults.get("usage_location")
    )

    if usage_location:
        usage_location = str(usage_location).strip().upper()

        if not re.fullmatch(r"[A-Z]{2}", usage_location):
            errors.append(
                "Usage Location must be a valid ISO country code "
                "(example: IN, US, GB)"
            )

    # ---------------------------------------------------------
    # Country Validation
    # ---------------------------------------------------------
    country = (
        fields.get("country_or_region")
        or defaults.get("country_or_region")
    )

    if country is not None and not str(country).strip():
        errors.append("Country / Region cannot be blank")

    # ---------------------------------------------------------
    # Employee Hire Date Validation
    # ---------------------------------------------------------
    hire_date = fields.get("employee_hire_date")

    if hire_date:
        try:
            normalized = normalize_iso_datetime(hire_date)

            parsed = datetime.fromisoformat(
                normalized.replace("Z", "+00:00")
            )

            if parsed.date() > datetime.now(timezone.utc).date():
                errors.append(
                    "Employee Hire Date cannot be in the future"
                )

        except ValidationError as exc:
            errors.append(str(exc))

    # ---------------------------------------------------------
    # Other Emails Validation
    # ---------------------------------------------------------
    other_mails = fields.get("other_mails") or []

    if not isinstance(other_mails, list):
        errors.append("Other Emails must be a list")
    else:
        for email in other_mails:
            if not is_email(email):
                errors.append(
                    f"Invalid email address in Other Emails: {email}"
                )

    # ---------------------------------------------------------
    # UPN / Alias Validation
    # ---------------------------------------------------------
    try:
        alias = sanitize_alias(
            fields["first_name"],
            fields["last_name"]
        )

        if len(alias) > 64:
            errors.append(
                "Generated mailNickname exceeds 64 characters"
            )

        domain = defaults.get("domain")

        if domain:
            upn = f"{alias}@{domain}"

            if len(upn) > 113:
                errors.append(
                    "Generated User Principal Name exceeds 113 characters"
                )

    except Exception as exc:
        errors.append(
            f"Unable to generate mailNickname/UserPrincipalName: {exc}"
        )

    # ---------------------------------------------------------
    # Manager Self Reference Validation
    # ---------------------------------------------------------
    if reporting_manager and defaults.get("domain"):
        try:
            alias = sanitize_alias(
                fields["first_name"],
                fields["last_name"]
            )

            expected_upn = (
                f"{alias}@{defaults['domain']}"
            ).lower()

            if reporting_manager.lower() == expected_upn:
                errors.append(
                    "Employee cannot be assigned as their own manager"
                )
        except Exception:
            pass

    # ---------------------------------------------------------
    # Microsoft Entra Attribute Length Validation
    # ---------------------------------------------------------

    field_limits = {
        "first_name": 64,
        "middle_name": 64,
        "last_name": 64,
        "designation": 128,
        "department_name": 64,
        "company_name": 64,
        "staff_no": 64,
        "phone_number": 64,
        "mobile_phone": 64,
        "fax_number": 64,
        "location": 128,
        "city": 128,
        "state_or_province": 128,
        "postal_code": 40,
        "country_or_region": 128,
        "company_address": 1024,
    }

    display_names = {
        "first_name": "First Name",
        "middle_name": "Middle Name",
        "last_name": "Last Name",
        "designation": "Designation",
        "department_name": "Department Name",
        "company_name": "Company Name",
        "staff_no": "Staff Number",
        "phone_number": "Business Phone",
        "mobile_phone": "Mobile Phone",
        "fax_number": "Fax Number",
        "location": "Office Location",
        "city": "City",
        "state_or_province": "State / Province",
        "postal_code": "Postal Code",
        "country_or_region": "Country / Region",
        "company_address": "Street Address",
    }

    for field_name, max_length in field_limits.items():
        value = fields.get(field_name)

        if (
            value is not None
            and isinstance(value, str)
            and len(value.strip()) > max_length
        ):
            errors.append(
                f"{display_names[field_name]} exceeds "
                f"Entra maximum length ({max_length} characters)"
            )

    # ---------------------------------------------------------
    # Final Validation Result
    # ---------------------------------------------------------
    if errors:
        raise ValidationError("\n".join(errors))

def ascii_text(value: str) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def sanitize_alias(first_name: str, last_name: str) -> str:
    first = ascii_text(first_name or "")
    last = ascii_text(last_name or "")

    raw = f"{first}.{last}".lower()

    alias = re.sub(r"[^a-z0-9._-]", "", raw)
    alias = re.sub(r"[._-]{2,}", ".", alias)
    alias = alias.strip("._-")

    if not alias:
        raise ValidationError(
            "Unable to derive mail nickname from employee name"
        )

    return alias[:64]


def join_non_empty(values: Iterable[Any], separator: str) -> str:
    return separator.join(str(value).strip()
        for value in values
        if value is not None
        and str(value).strip()
    )


def generate_password(length: int = 24) -> str:
    if length < 16:
        raise ConfigurationError(
            "Generated password length must be at least 16"
        )

    specials = "!@#$%^&*()-_=+[]{}?"

    alphabet = (
        string.ascii_lowercase
        + string.ascii_uppercase        + string.digits
        + specials
    )

    while True:
   *    password*= "".join(
            secrets.cho*ce(alphabet)
            for*_ in range(length)
        )

    *   if (
            any(c.islower(* for c in password)
            an* any(c.isupper() for c in password*
            and any(c.isdigit() f*r c in password)
            and a*y(c in specials for c in password)*        ):
            return pass*ord


def derive_values(fields: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    first = fields["first_name"].strip()
    middle = fields.get("middle_name")
    last = fields["last_name"].strip()
    alias = sanitize_alias(first, last)
    domain = defaults.get("domain")
    if not domain:
        raise ConfigurationError("defaults.domain is required")

    business_phone = fields.get("phone_number")
    return {
        "display_name": join_non_empty([first, middle, last], " "),
        "mail_nickname": alias,
        "upn": f"{alias}@{domain}",
        "job_title": join_non_empty([fields.get("grade"), fields.get("designation")], " - "),
        "employee_hire_date": normalize_iso_datetime(fields["employee_hire_date"]),
        "usage_location": (fields.get("usage_location") or defaults.get("usage_location", "IN")).upper(),
        "country": (fields.get("country_or_region") or defaults.get("country_or_region", "IN")).upper(),
        "office_location": fields.get("location") or fields.get("site"),
        "street_address": fields.get("company_address"),
        "business_phones": [business_phone] if business_phone else [],
    }


def remove_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: remove_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [remove_nulls(item) for item in value if item is not None]
    return value


def build_user_payload(fields: dict[str, Any], derived: dict[str, Any], config: dict[str, Any], password: str) -> dict[str, Any]:
    defaults = config.get("defaults", {})
    payload = {
        "accountEnabled": bool(defaults.get("account_enabled", True)),
        "displayName": derived["display_name"],
        "givenName": fields["first_name"],
        "surname": fields["last_name"],
        "mailNickname": derived["mail_nickname"],
        "userPrincipalName": derived["upn"],
        "passwordProfile": {
            "password": password,
            "forceChangePasswordNextSignIn": True,
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
    }
    return remove_nulls(payload)


class RetryHttpClient:
    def __init__(self, retry_config: dict[str, Any]):
        self.session = requests.Session()
        self.maximum_attempts = int(retry_config.get("maximum_attempts", 4))
        self.retryable = set(retry_config.get("retryable_http_status_codes", [429, 500, 502, 503, 504]))
        self.initial_delay = float(retry_config.get("initial_delay_seconds", 10))
        self.multiplier = float(retry_config.get("backoff_multiplier", 3))
        self.maximum_delay = float(retry_config.get("maximum_delay_seconds", 300))
        self.honor_retry_after = bool(retry_config.get("honor_retry_after_header", True))

    def request(self, method: str, url: str, *, expected: set[int], **kwargs: Any) -> requests.Response:
        delay = self.initial_delay
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                response = self.session.request(method, url, timeout=(10, 60), **kwargs)
            except requests.RequestException as exc:
                if attempt == self.maximum_attempts:
                    raise ApiError(f"Network error calling {url}: {exc}", retryable=True) from exc
                time.sleep(delay)
                delay = min(delay * self.multiplier, self.maximum_delay)
                continue

            if response.status_code in expected:
                return response

            if response.status_code not in self.retryable or attempt == self.maximum_attempts:
                request_id = response.headers.get("request-id") or response.headers.get("client-request-id")
                body = response.text[:1000]
                raise ApiError(
                    f"{method} {url} failed with HTTP {response.status_code}; request-id={request_id}; response={body}",
                    status_code=response.status_code,
                    retryable=response.status_code in self.retryable,
                )

            retry_after = response.headers.get("Retry-After")
            if self.honor_retry_after and retry_after and retry_after.isdigit():
                sleep_for = min(float(retry_after), self.maximum_delay)
            else:
                sleep_for = delay
            LOG.warning("Transient HTTP %s. Retrying in %.0f seconds", response.status_code, sleep_for)
            time.sleep(sleep_for)
            delay = min(delay * self.multiplier, self.maximum_delay)

        raise ApiError("HTTP retry loop exited unexpectedly")


class ServiceDeskClient:
    def __init__(self, config: dict[str, Any], http: RetryHttpClient):
        self.config = config["servicedesk"]
        self.http = http
        self.base_url = self.config["base_url"].rstrip("/")
        self.headers = {"Accept": "application/vnd.manageengine.sdp.v3+json"}
        self.auth: tuple[str, str] | None = None

        auth_type = self.config.get("auth", "basic").lower()
        credentials = self.config.get("credentials", {})
        if auth_type == "basic":
            self.auth = (env_required(credentials.get("username_env", "SDP_USERNAME")),
                         env_required(credentials.get("password_env", "SDP_PASSWORD")))
        elif auth_type == "bearer":
            token = env_required(credentials.get("token_env", "SDP_TOKEN"))
            self.headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "apikey":
            token = env_required(credentials.get("token_env", "SDP_API_KEY"))
            self.headers[credentials.get("header_name", "TECHNICIAN_KEY")] = token
        else:
            raise ConfigurationError(f"Unsupported ServiceDesk auth type: {auth_type}")

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        path = self.config["ticket_path"].format(ticket_id=quote(ticket_id, safe=""))
        response = self.http.request("GET", self.base_url + path, expected={200}, headers=self.headers, auth=self.auth)
        body = response.json()
        return body

    def add_note(self, ticket_id: str, message: str) -> None:
        path = self.config.get("comment_path")
        if not path:
            return
        url = self.base_url + path.format(ticket_id=quote(ticket_id, safe=""))
        # ServiceDesk Plus deployments differ in note payload shape. Override this
        # method or configure an integration adapter if the local API expects a
        # form-encoded input_data payload instead of JSON.
        payload = {"request_note": {"description": message, "show_to_requester": False}}
        self.http.request("POST", url, expected={200, 201}, headers={**self.headers, "Content-Type": "application/json"}, auth=self.auth, json=payload)


class GraphClient:
    def __init__(self, config: dict[str, Any], http: RetryHttpClient, correlation_id: str):
        self.config = config
        self.http = http
        self.base_url = config.get("graph", {}).get("base_url", "https://graph.microsoft.com/v1.0").rstrip("/")
        self.correlation_id = correlation_id
        tenant_id = env_required("ENTRA_TENANT_ID")
        client_id = env_required("ENTRA_CLIENT_ID")
        client_secret = env_required("ENTRA_CLIENT_SECRET")
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        token = result.get("access_token")
        if not token:
            safe_error = result.get("error_description") or result.get("error") or "unknown token error"
            raise ApiError(f"Unable to acquire Microsoft Graph application token: {safe_error}")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "client-request-id": correlation_id,
            "return-client-request-id": "true",
        }

    def get_user_by_employee_id(self, employee_id: str) -> dict[str, Any] | None:
        escaped = employee_id.replace("'", "''")
        url = self.base_url + "/users"
        params = {"$filter": f"employeeId eq '{escaped}'", "$select": "id,userPrincipalName,employeeId,accountEnabled"}
        response = self.http.request("GET", url, expected={200}, headers=self.headers, params=params)
        values = response.json().get("value", [])
        if len(values) > 1:
            raise ValidationError(f"Multiple Entra users have employeeId {employee_id}")
        return values[0] if values else None

    def user_exists(self, upn: str) -> bool:
        url = self.base_url + "/users/" + quote(upn, safe="")
        response = self.http.session.get(url, headers=self.headers, params={"$select": "id"}, timeout=(10, 60))
        if response.status_code == 404:
            return False
        if response.status_code == 200:
            return True
        raise ApiError(f"Unable to check UPN availability: HTTP {response.status_code}: {response.text[:500]}", response.status_code)

    def reserve_available_upn(self, base_alias: str, domain: str, maximum: int = 100) -> tuple[str, str]:
        for sequence in range(1, maximum + 1):
            alias = base_alias if sequence == 1 else f"{base_alias}{sequence}"
            upn = f"{alias}@{domain}"
            if not self.user_exists(upn):
                return alias, upn
        raise ValidationError(f"Unable to find an available UPN after {maximum} attempts")

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.request("POST", self.base_url + "/users", expected={201}, headers=self.headers, json=payload)
        return response.json()

    def resolve_manager(self, email_or_upn: str) -> dict[str, Any]:
        direct_url = self.base_url + "/users/" + quote(email_or_upn, safe="")
        response = self.http.session.get(direct_url, headers=self.headers, params={"$select": "id,mail,userPrincipalName,accountEnabled"}, timeout=(10, 60))
        if response.status_code == 200:
            manager = response.json()
        elif response.status_code == 404:
            escaped = email_or_upn.replace("'", "''")
            response = self.http.request(
                "GET", self.base_url + "/users", expected={200}, headers=self.headers,
                params={"$filter": f"mail eq '{escaped}'", "$select": "id,mail,userPrincipalName,accountEnabled"},
            )
            matches = response.json().get("value", [])
            if len(matches) != 1:
                raise ValidationError(f"Reporting manager could not be uniquely resolved: {email_or_upn}")
            manager = matches[0]
        else:
            raise ApiError(f"Manager lookup failed with HTTP {response.status_code}: {response.text[:500]}", response.status_code)
        if manager.get("accountEnabled") is False:
            raise ValidationError(f"Reporting manager account is disabled: {email_or_upn}")
        return manager

    def assign_manager(self, user_id: str, manager_id: str) -> None:
        url = self.base_url + f"/users/{quote(user_id, safe='')}/manager/$ref"
        payload = {"@odata.id": f"{self.base_url}/users/{manager_id}"}
        self.http.request("PUT", url, expected={204}, headers=self.headers, json=payload)


def redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(payload))
    if "passwordProfile" in copy:
        copy["passwordProfile"]["password"] = "***REDACTED***"
    return copy


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config, mapping = load_configuration(config_path)
    retry_config = config.get("processing", {}).get("retry_policy", {})
    http = RetryHttpClient(retry_config)

    ticket_id = str(args.ticket_id or "LOCAL-INPUT")
    correlation_id = str(uuid.uuid4())
    LOG.info("Starting onboarding ticket=%s correlation_id=%s dry_run=%s", ticket_id, correlation_id, args.dry_run)

    service_desk: ServiceDeskClient | None = None
    if args.input_file:
        with Path(args.input_file).open("r", encoding="utf-8") as handle:
            ticket_payload = json.load(handle)
    else:
        service_desk = ServiceDeskClient(config, http)
        ticket_payload = service_desk.get_ticket(ticket_id)

    fields = extract_source_fields(ticket_payload, mapping)
    validate_input(fields, config)

    idempotency_pattern = config.get("processing", {}).get("idempotency_key", "EMPLOYEE_ONBOARDING:{ticket_id}:{staff_no}")
    idempotency_key = idempotency_pattern.format(ticket_id=ticket_id, staff_no=fields["staff_no"])
    context = RuntimeContext(ticket_id, correlation_id, idempotency_key, args.dry_run)

    derived = derive_values(fields, config)
    password_length = int(config.get("transformations", {}).get("password", {}).get("minimum_length", 20))
    password = generate_password(password_length)
    initial_payload = build_user_payload(fields, derived, config, password)

    if args.dry_run:
        print(json.dumps({
            "ticket_id": ticket_id,
            "correlation_id": correlation_id,
            "idempotency_key": idempotency_key,
            "validated_fields": fields,
            "graph_create_user_payload": redacted_payload(initial_payload),
            "manager_lookup": fields["reporting_manager"],
        }, indent=2, ensure_ascii=False))
        LOG.info("Dry run completed; no Entra or ServiceDesk changes were made")
        return 0

    graph = GraphClient(config, http, correlation_id)

    existing = graph.get_user_by_employee_id(fields["staff_no"])
    if existing:
        raise ValidationError(
            f"An Entra user already exists for Staff No {fields['staff_no']}: {existing.get('userPrincipalName')}"
        )

    domain = config["defaults"]["domain"]
    alias, upn = graph.reserve_available_upn(derived["mail_nickname"], domain)
    derived["mail_nickname"] = alias
    derived["upn"] = upn
    payload = build_user_payload(fields, derived, config, password)

    manager = graph.resolve_manager(fields["reporting_manager"])
    context.manager_id = manager["id"]

    created = graph.create_user(payload)
    context.created_user_id = created["id"]
    context.created_upn = created["userPrincipalName"]
    LOG.info("Created Entra user object_id=%s upn=%s", context.created_user_id, context.created_upn)

    graph.assign_manager(context.created_user_id, context.manager_id)
    LOG.info("Assigned manager manager_id=%s user_id=%s", context.manager_id, context.created_user_id)

    if service_desk:
        try:
            service_desk.add_note(
                ticket_id,
                f"Employee onboarding completed successfully. UPN: {context.created_upn}. "
                f"Entra object ID: {context.created_user_id}. Correlation ID: {correlation_id}.",
            )
        except LifecycleError as exc:
            LOG.error("User was created, but ServiceDesk note update failed: %s", exc)

    print(json.dumps({
        "status": "COMPLETED",
        "ticket_id": ticket_id,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
        "entra_object_id": context.created_user_id,
        "user_principal_name": context.created_upn,
        "manager_object_id": context.manager_id,
    }, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision an approved ServiceDesk employee request into Microsoft Entra ID")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ticket-id", help="ServiceDesk ticket/request ID")
    source.add_argument("--input-file", help="Saved ServiceDesk JSON response, useful for testing")
    parser.add_argument("--config", default="onboarding.yaml", help="Path to onboarding.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print a redacted Graph payload without making changes")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return run(args)
    except ValidationError as exc:
        LOG.error("Validation failed: %s", exc)
        return 2
    except ConfigurationError as exc:
        LOG.error("Configuration failed: %s", exc)
        return 3
    except ApiError as exc:
        LOG.error("API operation failed: %s", exc)
        return 4
    except (OSError, ValueError, KeyError, json.JSONDecodeError, yaml.YAMLError) as exc:
        LOG.exception("Unhandled input/configuration error: %s", exc)
        return 5


if __name__ == "__main__":
    sys.exit(main())
