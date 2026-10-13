"""Onboarding report: what was requested, what was done, and what Entra shows now."""

from __future__ import annotations

import html
import logging
import time
from typing import Any

from helpers.state import utc_now

LOG = logging.getLogger("onboarding.report")

ASSIGNMENT_LABELS = {
    "user_groups": "User groups",
    "rule_bundles": "Rule bundles",
    "intune_apps": "Intune apps",
}


# --------------------------------------------------------------- verification


def _expected(state: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    licenses = {item["sku_id"].lower() for item in state.get("license", {}).get("expected", [])}
    groups: dict[str, str] = {}
    for result in state.get("assignments", {}).values():
        for item in result.get("expected", []):
            groups[item["group_id"].lower()] = item["group_name"]
    return licenses, groups


def _run_checks(graph: Any, state: dict[str, Any], account_enabled: bool) -> list[dict[str, Any]]:
    user_state = state.get("user", {})
    user_id = user_state["id"]
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    user = graph.get_user(user_id, "id,userPrincipalName,accountEnabled,assignedLicenses,employeeId")
    if not user:
        add("User exists", False, f"object {user_id} not found")
        return checks
    add("User exists", True, user.get("userPrincipalName", ""))
    add(
        "UPN matches",
        (user.get("userPrincipalName") or "").lower() == (user_state.get("upn") or "").lower(),
        user.get("userPrincipalName", ""),
    )
    add(f"Account enabled = {account_enabled}", bool(user.get("accountEnabled")) == account_enabled, str(user.get("accountEnabled")))

    manager_expected = user_state.get("manager_id")
    if manager_expected:
        manager_actual = graph.get_manager_id(user_id)
        add("Manager assigned", (manager_actual or "").lower() == manager_expected.lower(), manager_actual or "none")

    expected_licenses, expected_groups = _expected(state)
    assigned = {item["skuId"].lower() for item in user.get("assignedLicenses", []) if item.get("skuId")}
    sku_names = {item["sku_id"].lower(): item["sku_part_number"] for item in state.get("license", {}).get("expected", [])}
    for sku_id in sorted(expected_licenses):
        add(f"License {sku_names.get(sku_id, sku_id)}", sku_id in assigned, "assigned" if sku_id in assigned else "missing")

    if expected_groups:
        member_of = graph.list_member_group_ids(user_id)
        for group_id, name in sorted(expected_groups.items(), key=lambda kv: kv[1].lower()):
            add(f"Member of {name}", group_id in member_of, "member" if group_id in member_of else "missing")
    return checks


def verify(
    graph: Any, state: dict[str, Any], attempts: int = 3, delay_seconds: float = 15, account_enabled: bool = True
) -> list[dict[str, Any]]:
    """Re-read the user from Entra; retry briefly because writes replicate asynchronously."""
    checks: list[dict[str, Any]] = []
    for attempt in range(1, max(1, attempts) + 1):
        checks = _run_checks(graph, state, account_enabled)
        if all(c["ok"] for c in checks):
            return checks
        if attempt < attempts:
            LOG.info("Verification mismatch (attempt %d/%d); waiting %.0fs for replication", attempt, attempts, delay_seconds)
            time.sleep(delay_seconds)
    return checks


# ------------------------------------------------------------------- build


def build_report(state: dict[str, Any], checks: list[dict[str, Any]] | None, outcome: str) -> dict[str, Any]:
    fields = state.get("ticket", {}).get("fields", {})
    return {
        "generated_at": utc_now(),
        "outcome": outcome,
        "ticket_id": state.get("ticket_id"),
        "dag_run_id": state.get("dag_run_id"),
        "correlation_id": state.get("correlation_id"),
        "dry_run": state.get("dry_run", False),
        "employee": {
            "name": " ".join(filter(None, [fields.get("first_name"), fields.get("middle_name"), fields.get("last_name")])),
            "staff_no": fields.get("staff_no"),
            "department": fields.get("department_name"),
            "designation": fields.get("designation"),
            "company": fields.get("company_name"),
            "location": fields.get("location"),
            "reporting_manager": fields.get("reporting_manager"),
        },
        "account": state.get("user") or state.get("plan", {}).get("user_preview", {}),
        "license": state.get("license", {}),
        "assignments": state.get("assignments", {}),
        "verification": checks or [],
        "steps": state.get("steps", {}),
        "warnings": state.get("warnings", []),
        "failure": state.get("failure"),
    }


def _assignment_rows(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows = []
    for key, action in (("added", "added"), ("already_member", "already member"), ("would_add", "would add (dry run)")):
        for item in result.get(key, []):
            target = item["group_name"] + (f" (app: {item['app_name']})" if item.get("app_name") else "")
            rows.append((item["requested"], target, action))
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    acc, emp, lic = report["account"], report["employee"], report["license"]
    lines = [
        f"# Onboarding report - ticket {report['ticket_id']}",
        "",
        f"- Outcome: **{report['outcome']}**" + (" (DRY RUN - no changes made)" if report["dry_run"] else ""),
        f"- Generated: {report['generated_at']}",
        f"- Dagu run: {report['dag_run_id'] or '-'} | correlation id: {report['correlation_id'] or '-'}",
        "",
        "## Employee",
        f"- Name: {emp['name']}  |  Staff no: {emp['staff_no']}",
        f"- {emp['designation'] or '-'}, {emp['department'] or '-'}, {emp['company'] or '-'} ({emp['location'] or '-'})",
        f"- Reporting manager: {emp['reporting_manager'] or '-'}",
        "",
        "## Entra account",
        f"- UPN / e-mail: {acc.get('upn', '-')}",
        f"- Object id: {acc.get('id', '-')}",
        f"- Employee-code fallback used for e-mail: {'yes' if acc.get('used_fallback_address') else 'no'}",
        f"- Manager object id: {acc.get('manager_id', '-')}",
        "",
        "## Licenses",
    ]
    if lic.get("expected"):
        for item in lic["expected"]:
            state = "added" if item["sku_id"] in {x["sku_id"] for x in lic.get("added", [])} else (
                "would add (dry run)" if item["sku_id"] in {x["sku_id"] for x in lic.get("would_add", [])} else "already assigned"
            )
            lines.append(f"- {item['sku_part_number']} ({item['requested']}): {state}")
    else:
        lines.append("- none")

    for category, result in report["assignments"].items():
        lines += ["", f"## {ASSIGNMENT_LABELS.get(category, category)}"]
        rows = _assignment_rows(result)
        if rows:
            lines += ["| Requested | Entra group | Action |", "|---|---|---|"]
            lines += [f"| {r} | {g} | {a} |" for r, g, a in rows]
        else:
            lines.append("- none requested")

    if report["verification"]:
        lines += ["", "## Verification (read back from Entra)", "| Check | Result | Detail |", "|---|---|---|"]
        lines += [f"| {c['check']} | {'OK' if c['ok'] else 'MISMATCH'} | {c['detail']} |" for c in report["verification"]]

    lines += ["", "## Steps", "| Step | Status | Finished |", "|---|---|---|"]
    lines += [f"| {name} | {s.get('status')} | {s.get('finished_at') or '-'} |" for name, s in report["steps"].items()]

    if report["warnings"]:
        lines += ["", "## Warnings"] + [f"- {w}" for w in report["warnings"]]
    if report["failure"]:
        f = report["failure"]
        lines += ["", "## Failure", f"- Step: {f.get('step')}", f"- Error: {f.get('error_type')}: {f.get('message')}"]
    return "\n".join(lines) + "\n"


def render_html_note(report: dict[str, Any], max_rows: int = 60) -> str:
    """Compact HTML for a ServiceDesk Plus note (technician-only)."""
    e = html.escape
    acc = report["account"]
    parts = [
        f"<b>Automated onboarding: {e(report['outcome'])}</b>" + (" (dry run)" if report["dry_run"] else ""),
        f"UPN: {e(str(acc.get('upn', '-')))} | Object id: {e(str(acc.get('id', '-')))}",
    ]
    lic = report["license"]
    if lic.get("expected"):
        parts.append("Licenses: " + e(", ".join(i["sku_part_number"] for i in lic["expected"])))
    for category, result in report["assignments"].items():
        rows = _assignment_rows(result)
        if rows:
            parts.append(f"{e(ASSIGNMENT_LABELS.get(category, category))}: " + e(", ".join(f"{g} [{a}]" for _, g, a in rows[:max_rows])))
    if report["verification"]:
        bad = [c for c in report["verification"] if not c["ok"]]
        parts.append(
            "Verification: all checks passed" if not bad else "Verification MISMATCH: " + e(", ".join(c["check"] for c in bad))
        )
    if report["failure"]:
        f = report["failure"]
        parts.append(f"<b>Failed step:</b> {e(str(f.get('step')))} - {e(str(f.get('message'))[:800])}")
    parts.append(f"Dagu run: {e(str(report['dag_run_id'] or '-'))} | Correlation id: {e(str(report['correlation_id'] or '-'))}")
    return "<br>".join(parts)
