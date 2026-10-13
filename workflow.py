"""Onboarding workflow steps.

Fetch Ticket -> Validate Ticket -> Create User -> Assign License -> Add to user
groups -> Assign rule bundles -> Assign Intune apps -> Generate report -> Close
ticket.

Each step runs as its own Dagu step (``onboard.py <step> <ticket_id>``).
Results pass between steps through the per-ticket state file. A step refuses
to start unless the previous step completed, and any failure exits non-zero,
so Dagu stops the run there and fires the ``error-report`` handler.
"""

from __future__ import annotations

import html
import json
import logging
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from helpers.directory_map import DirectoryMap, JsonCache, resolve_licenses
from helpers.errors import LifecycleError, StepOrderError, ValidationError, VerificationError
from helpers.report import build_report, render_html_note, render_markdown, verify
from helpers.state import StateStore, utc_now
from helpers.transformation import (
    build_user_payload,
    choose_available_alias,
    derive_values,
    email_candidates,
    extract_source_fields,
    generate_password,
    get_by_path,
    redacted_payload,
)
from helpers.validation import validate_input

LOG = logging.getLogger("onboarding.workflow")


@dataclass
class StepContext:
    ticket_id: str
    config: dict[str, Any]
    mapping: dict[str, Any]
    store: StateStore
    dirs: dict[str, Path]
    graph_factory: Callable[[str], Any]
    sdp_factory: Callable[[], Any]
    dry_run: bool = False
    input_file: Path | None = None
    dag_run_id: str | None = None
    _graph: Any = field(default=None, repr=False)
    _sdp: Any = field(default=None, repr=False)
    _directory: DirectoryMap | None = field(default=None, repr=False)
    correlation_id: str = ""

    def graph(self) -> Any:
        if self._graph is None:
            self._graph = self.graph_factory(self.correlation_id or str(uuid.uuid4()))
        return self._graph

    def sdp(self) -> Any:
        if self._sdp is None:
            self._sdp = self.sdp_factory()
        return self._sdp

    def directory(self) -> DirectoryMap:
        if self._directory is None:
            ttl = self.config.get("directory_cache", {}).get("ttl_minutes", 60)
            self._directory = DirectoryMap(self.graph(), JsonCache(self.dirs["cache"], ttl))
        return self._directory


# ============================================================== helpers


def _fields(state: dict[str, Any]) -> dict[str, Any]:
    fields = state.get("ticket", {}).get("fields")
    if not fields:
        raise StepOrderError("No ticket data in state; run the fetch step first")
    return fields


def _user_id(ctx: StepContext, state: dict[str, Any]) -> str | None:
    """Return the created user's object id (``None`` only in dry-run mode)."""
    user_id = state.get("user", {}).get("id")
    if not user_id and not ctx.dry_run:
        raise StepOrderError("No Entra user recorded in state; the create-user step has not completed")
    return user_id


def _is_pending_user(state: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    """True when ``existing`` is the user a previous create-user attempt POSTed but never recorded.

    That happens when Graph created the user but the response was lost (timeout)
    or the process died before saving state. Such a user is adopted, not
    reported as a duplicate, so the ticket can be re-run.
    """
    pending = (state.get("user", {}).get("pending_upn") or "").lower()
    return bool(existing and pending and (existing.get("userPrincipalName") or "").lower() == pending)


def _ticket_is_local(state: dict[str, Any]) -> bool:
    return state.get("ticket", {}).get("source") == "file"


def _sdp_writes_allowed(ctx: StepContext, state: dict[str, Any]) -> bool:
    return not ctx.dry_run and not _ticket_is_local(state)


# ============================================================== steps


def step_fetch(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    if ctx.input_file:
        with Path(ctx.input_file).open("r", encoding="utf-8") as handle:
            ticket = json.load(handle)
        source = "file"
    else:
        ticket = ctx.sdp().get_ticket(ctx.ticket_id)
        source = "servicedesk"

    fields = extract_source_fields(ticket, ctx.mapping)
    previous_staff_no = state.get("ticket", {}).get("fields", {}).get("staff_no")
    if state.get("user", {}).get("id") and previous_staff_no and previous_staff_no != fields.get("staff_no"):
        raise ValidationError(
            f"Staff number changed from {previous_staff_no} to {fields.get('staff_no')} after user "
            f"{state['user'].get('upn')} was created. Resolve manually."
        )
    state["ticket"] = {"source": source, "fetched_at": utc_now(), "fields": fields}
    populated = sum(1 for v in fields.values() if v not in (None, "", []))
    return {"source": source, "status": fields.get("status"), "fields_populated": populated}


def step_validate(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(state)
    warnings = validate_input(fields, ctx.config)
    state["warnings"] = sorted(set(state.get("warnings", []) + warnings))

    if not ctx.config.get("validation", {}).get("directory_checks", True):
        return {"directory_checks": "disabled"}

    # Directory checks run before anything is created, so a typo in a group
    # name or an exhausted license pool stops the run with no partial user.
    graph = ctx.graph()
    errors: list[str] = []
    plan: dict[str, Any] = {}
    created_id = (state.get("user", {}).get("id") or "").lower()

    existing = graph.get_user_by_employee_id(fields["staff_no"])
    if existing and existing["id"].lower() != created_id and not _is_pending_user(state, existing):
        errors.append(
            f"An Entra user already exists for staff no {fields['staff_no']}: {existing.get('userPrincipalName')}"
        )

    try:
        manager = graph.resolve_manager(fields["reporting_manager"])
        plan["manager"] = {k: manager.get(k) for k in ("id", "userPrincipalName", "displayName")}
    except ValidationError as exc:
        errors.append(str(exc))

    license_cfg = ctx.config.get("license", {})
    try:
        licenses = resolve_licenses(graph, fields.get(license_cfg.get("source_field", "license_sku")), license_cfg)
        assigned = graph.get_assigned_sku_ids(created_id) if created_id else set()
        for item in licenses:
            if item["sku_id"].lower() not in assigned and item["available_units"] < 1:
                errors.append(f"License {item['sku_part_number']} has no available seats")
        plan["licenses"] = licenses
    except ValidationError as exc:
        errors.append(str(exc))

    plan["assignments"] = {}
    for category, settings in (ctx.config.get("assignments") or {}).items():
        try:
            targets = ctx.directory().resolve(category, fields.get(settings.get("source_field", category)) or [], settings)
            plan["assignments"][category] = [t.to_dict() for t in targets]
        except ValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise ValidationError("\n".join(errors))
    state["plan"] = plan
    return {
        "manager": plan["manager"]["userPrincipalName"],
        "licenses": [l["sku_part_number"] for l in plan["licenses"]],
        **{k: len(v) for k, v in plan["assignments"].items()},
    }


def step_create_user(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(state)
    config = ctx.config
    graph = ctx.graph()
    user_state = state.setdefault("user", {})

    manager = graph.resolve_manager(fields["reporting_manager"])

    # Re-run after a later failure: the user already exists, only make sure the manager is set.
    if user_state.get("id"):
        existing = graph.get_user(user_state["id"], "id,userPrincipalName")
        if not existing:
            raise ValidationError(
                f"User {user_state.get('upn')} ({user_state['id']}) recorded for this ticket no longer exists in Entra"
            )
        if (graph.get_manager_id(user_state["id"]) or "").lower() != manager["id"].lower():
            if ctx.dry_run:
                return {"skipped": "user already exists", "would_change_manager_to": manager["id"]}
            graph.assign_manager(user_state["id"], manager["id"])
        user_state["manager_id"] = manager["id"]
        return {"skipped": "user already created by an earlier run", "upn": user_state.get("upn")}

    existing = graph.get_user_by_employee_id(fields["staff_no"])
    if _is_pending_user(state, existing) and not ctx.dry_run:
        user_state.update(
            {
                "id": existing["id"],
                "upn": existing["userPrincipalName"],
                "mail": existing["userPrincipalName"],
                "created_at": utc_now(),
                "used_fallback_address": user_state.get("pending_fallback", False),
            }
        )
        user_state.pop("pending_upn", None)
        user_state.pop("pending_fallback", None)
        ctx.store.save(state)
        graph.assign_manager(existing["id"], manager["id"])
        user_state["manager_id"] = manager["id"]
        state.setdefault("warnings", []).append(
            "User was created by an interrupted earlier attempt and adopted; its initial password is unknown - reset it"
        )
        return {"adopted": True, "upn": user_state["upn"], "object_id": existing["id"]}
    if existing:
        raise ValidationError(
            f"An Entra user already exists for staff no {fields['staff_no']}: {existing.get('userPrincipalName')}"
        )

    # E-mail: first.last@domain; if taken anywhere in the tenant, first.last<employee code>@domain.
    domain = config["defaults"]["domain"]
    email_cfg = config.get("email", {})
    taken = graph.list_used_addresses(include_soft_deleted=bool(email_cfg.get("include_soft_deleted_users", False)))
    candidates = email_candidates(
        fields["first_name"], fields["last_name"], fields["staff_no"], domain, email_cfg.get("employee_code_separator", "")
    )
    alias, _ = choose_available_alias(candidates, domain, taken)
    upn = f"{alias}@{domain}"

    derived = derive_values(fields, config)
    derived["mail_nickname"], derived["upn"] = alias, upn
    password_cfg = config.get("password", {})
    password = generate_password(int(password_cfg.get("length", 20)))
    payload = build_user_payload(fields, derived, config, password)

    if ctx.dry_run:
        state.setdefault("plan", {})["user_preview"] = {
            "upn": upn,
            "used_fallback_address": alias != candidates[0],
            "manager_id": manager["id"],
        }
        LOG.info("DRY RUN - would create user:\n%s", json.dumps(redacted_payload(payload), indent=2, ensure_ascii=False))
        return {"dry_run": True, "upn": upn, "used_fallback_address": alias != candidates[0]}

    # Record the UPN before the POST so an interrupted attempt can be recognised and adopted on re-run.
    user_state["pending_upn"], user_state["pending_fallback"] = upn, alias != candidates[0]
    ctx.store.save(state)

    created = graph.create_user(payload)
    user_state.pop("pending_upn", None)
    user_state.pop("pending_fallback", None)
    user_state.update(
        {
            "id": created["id"],
            "upn": created.get("userPrincipalName", upn),
            "mail": upn,
            "display_name": derived["display_name"],
            "created_at": utc_now(),
            "used_fallback_address": alias != candidates[0],
        }
    )
    ctx.store.save(state)  # persist the object id before anything else can fail
    LOG.info("Created Entra user object_id=%s upn=%s", created["id"], user_state["upn"])

    graph.assign_manager(created["id"], manager["id"])
    user_state["manager_id"] = manager["id"]

    if password_cfg.get("delivery") == "sdp_private_note" and _sdp_writes_allowed(ctx, state):
        try:
            ctx.sdp().add_note(
                ctx.ticket_id,
                f"Temporary password for {upn}: <code>{html.escape(password)}</code><br>The user must change it at first sign-in.",
                show_to_requester=False,
            )
        except LifecycleError as exc:
            # Do not fail the step: the user exists and a re-run would never resend the password.
            state.setdefault("warnings", []).append(f"Password note could not be added to the ticket ({exc}); reset the password manually")
    del password
    return {"upn": user_state["upn"], "object_id": created["id"], "used_fallback_address": user_state["used_fallback_address"]}


def step_assign_license(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(state)
    license_cfg = ctx.config.get("license", {})
    graph = ctx.graph()
    licenses = resolve_licenses(graph, fields.get(license_cfg.get("source_field", "license_sku")), license_cfg)
    user_id = _user_id(ctx, state)
    result: dict[str, Any] = {"expected": licenses, "added": [], "already_assigned": [], "would_add": []}
    state["license"] = result
    if not licenses:
        return {"skipped": "no license requested"}

    assigned = graph.get_assigned_sku_ids(user_id) if user_id else set()
    to_add = [item for item in licenses if item["sku_id"].lower() not in assigned]
    result["already_assigned"] = [item for item in licenses if item["sku_id"].lower() in assigned]
    no_seats = [item["sku_part_number"] for item in to_add if item["available_units"] < 1]
    if no_seats:
        raise ValidationError(f"No available seats for: {', '.join(no_seats)}")

    if ctx.dry_run:
        result["would_add"] = to_add
    elif to_add:
        graph.assign_licenses(user_id, [{"skuId": i["sku_id"], "disabledPlans": i["disabled_plans"]} for i in to_add])
        result["added"] = to_add
    return {
        "added": [i["sku_part_number"] for i in result["added"] or result["would_add"]],
        "already_assigned": [i["sku_part_number"] for i in result["already_assigned"]],
    }


def _assignment_step(category: str) -> Callable[[StepContext, dict[str, Any]], dict[str, Any]]:
    def run(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
        fields = _fields(state)
        settings = (ctx.config.get("assignments") or {}).get(category)
        if settings is None:
            return {"skipped": f"assignments.{category} is not configured"}
        values = fields.get(settings.get("source_field", category)) or []
        targets = ctx.directory().resolve(category, values, settings)  # resolves everything before changing anything

        user_id = _user_id(ctx, state)
        result: dict[str, Any] = {
            "expected": [t.to_dict() for t in targets],
            "added": [],
            "already_member": [],
            "would_add": [],
        }
        state.setdefault("assignments", {})[category] = result
        if not targets:
            return {"skipped": "nothing requested"}

        graph = ctx.graph()
        current = graph.list_member_group_ids(user_id) if user_id else set()
        try:
            for target in targets:
                if target.group_id.lower() in current:
                    result["already_member"].append(target.to_dict())
                elif ctx.dry_run:
                    result["would_add"].append(target.to_dict())
                elif graph.add_group_member(target.group_id, user_id):
                    result["added"].append(target.to_dict())
                    LOG.info("Added %s to %s", user_id, target.group_name)
                else:
                    result["already_member"].append(target.to_dict())
        finally:
            ctx.store.save(state)  # keep partial progress visible in the report if a later add fails
        return {
            "added": [t["group_name"] for t in result["added"] or result["would_add"]],
            "already_member": [t["group_name"] for t in result["already_member"]],
        }

    run.__name__ = f"step_{category}"
    return run


def _write_report(ctx: StepContext, state: dict[str, Any], report: dict[str, Any], suffix: str) -> dict[str, str]:
    directory = ctx.dirs["reports"]
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)  # reports contain employee details
    stamp = report["generated_at"].replace(":", "").replace("-", "")
    base = directory / f"onboarding-{ctx.ticket_id}-{stamp}{suffix}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)  # lands in the Dagu step log for the administrator
    return {"json": str(json_path), "markdown": str(md_path)}


def step_generate_report(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    report_cfg = ctx.config.get("report", {})
    checks = None
    user_id = state.get("user", {}).get("id")
    if user_id and not ctx.dry_run:
        checks = verify(
            ctx.graph(),
            state,
            attempts=int(report_cfg.get("verify_attempts", 3)),
            delay_seconds=float(report_cfg.get("verify_delay_seconds", 15)),
            account_enabled=bool(ctx.config.get("defaults", {}).get("account_enabled", True)),
        )
    mismatches = [c for c in checks or [] if not c["ok"]]
    outcome = "DRY RUN" if ctx.dry_run else ("VERIFICATION FAILED" if mismatches else "SUCCESS")

    report = build_report(state, checks, outcome)
    paths = _write_report(ctx, state, report, "")
    state["report"] = {"paths": paths, "outcome": outcome, "verification": checks or []}

    if report_cfg.get("post_to_ticket", True) and _sdp_writes_allowed(ctx, state):
        ctx.sdp().add_note(ctx.ticket_id, render_html_note(report), show_to_requester=False)

    if mismatches and report_cfg.get("fail_on_mismatch", True):
        raise VerificationError("Entra does not match the expected result: " + ", ".join(c["check"] for c in mismatches))
    return {"outcome": outcome, **paths}


def step_close_ticket(ctx: StepContext, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("ticket_closed"):
        return {"skipped": "ticket already closed by an earlier run"}
    if ctx.dry_run:
        return {"skipped": "dry run"}
    if _ticket_is_local(state):
        return {"skipped": "ticket was loaded from --input-file"}

    close_cfg = ctx.config.get("close", {})
    sdp = ctx.sdp()
    status_path = ctx.mapping["source"]["fields"].get("status", "request.status.name")
    current_status = get_by_path(sdp.get_ticket(ctx.ticket_id), status_path)
    if current_status in close_cfg.get("closed_statuses", ["Closed"]):
        state["ticket_closed"] = True
        return {"skipped": f"ticket is already {current_status}"}

    user = state.get("user", {})
    summary = (
        f"Employee onboarding completed. UPN: {user.get('upn')}. Entra object id: {user.get('id')}. "
        f"Dagu run: {state.get('dag_run_id')}. Correlation id: {state.get('correlation_id')}."
    )
    if close_cfg.get("set_resolution", True):
        sdp.set_resolution(ctx.ticket_id, summary, close_cfg.get("extra_request_fields") or {})
    sdp.close(
        ctx.ticket_id,
        comments=close_cfg.get("closure_comments", "Closed automatically after successful onboarding."),
        closure_code=close_cfg.get("closure_code", "Success"),
        requester_ack=bool(close_cfg.get("requester_ack_resolution", True)),
    )
    state["ticket_closed"] = True
    return {"closed": True}


STEPS: dict[str, Callable[[StepContext, dict[str, Any]], dict[str, Any]]] = {
    "fetch": step_fetch,
    "validate": step_validate,
    "create-user": step_create_user,
    "assign-license": step_assign_license,
    "add-groups": _assignment_step("user_groups"),
    "assign-rule-bundles": _assignment_step("rule_bundles"),
    "assign-intune-apps": _assignment_step("intune_apps"),
    "generate-report": step_generate_report,
    "close-ticket": step_close_ticket,
}
STEP_ORDER = list(STEPS)


# ============================================================== runner


def run_step(name: str, ctx: StepContext) -> dict[str, Any]:
    if name not in STEPS:
        raise ValueError(f"Unknown step {name}")
    state = ctx.store.load(ctx.ticket_id)

    if name == "fetch":
        # A new run (or a re-run after a failure): keep created objects, reset step tracking.
        state["steps"], state["failure"], state["warnings"] = {}, None, []
        state["dry_run"] = ctx.dry_run
        state["correlation_id"] = str(uuid.uuid4())
        state["dag_run_id"] = ctx.dag_run_id
    else:
        previous = STEP_ORDER[STEP_ORDER.index(name) - 1]
        problem = None
        if state.get("steps", {}).get(previous, {}).get("status") != "completed":
            problem = f"Step '{name}' requires '{previous}' to complete first in this run"
        elif bool(state.get("dry_run")) != ctx.dry_run:
            problem = (
                f"This run was started with dry_run={state.get('dry_run')}; "
                f"step '{name}' was called with dry_run={ctx.dry_run}"
            )
        if problem:
            if ctx.store.exists(ctx.ticket_id) and not state.get("failure"):
                state["failure"] = {"step": name, "error_type": "StepOrderError", "message": problem, "at": utc_now()}
                ctx.store.save(state)
            raise StepOrderError(problem)
    ctx.correlation_id = state.get("correlation_id") or str(uuid.uuid4())

    record = {"status": "running", "started_at": utc_now(), "finished_at": None, "details": None}
    state.setdefault("steps", {})[name] = record
    ctx.store.save(state)
    LOG.info("Step %s started for ticket %s (dry_run=%s)", name, ctx.ticket_id, ctx.dry_run)
    try:
        details = STEPS[name](ctx, state)
    except Exception as exc:
        record.update(status="failed", finished_at=utc_now(), details=str(exc)[:2000])
        state["failure"] = {
            "step": name,
            "error_type": exc.__class__.__name__,
            "message": str(exc)[:4000],
            "at": utc_now(),
            "retryable": getattr(exc, "retryable", None),
        }
        if not isinstance(exc, LifecycleError):
            state["failure"]["traceback"] = traceback.format_exc()[-4000:]
        ctx.store.save(state)
        raise
    record.update(status="completed", finished_at=utc_now(), details=details)
    if (state.get("failure") or {}).get("step") == name:
        state["failure"] = None  # the step that failed earlier has now succeeded
    ctx.store.save(state)
    LOG.info("Step %s completed: %s", name, json.dumps(details, default=str))
    return details


def handle_failure(ctx: StepContext) -> dict[str, Any]:
    """Dagu ``handler_on.failure``: write a failure report and note the ticket. Never raises."""
    try:
        state = ctx.store.load(ctx.ticket_id)
    except Exception as exc:  # noqa: BLE001
        LOG.error("Unable to load state for failure report: %s", exc)
        return {"reported": False}
    ctx.dry_run = bool(state.get("dry_run"))
    if not state.get("failure"):
        state["failure"] = {"step": "unknown", "error_type": "Unknown", "message": "Run failed before a step recorded an error; see the Dagu log."}

    report = build_report(state, None, "FAILED")
    result: dict[str, Any] = {"reported": True}
    try:
        result.update(_write_report(ctx, state, report, "-FAILED"))
    except Exception as exc:  # noqa: BLE001
        LOG.error("Unable to write failure report: %s", exc)

    if ctx.config.get("report", {}).get("post_failure_to_ticket", True) and _sdp_writes_allowed(ctx, state):
        try:
            ctx.sdp().add_note(ctx.ticket_id, render_html_note(report), show_to_requester=False)
            result["ticket_note"] = True
        except Exception as exc:  # noqa: BLE001
            LOG.error("Unable to add failure note to ticket %s: %s", ctx.ticket_id, exc)
            result["ticket_note"] = False
    return result
