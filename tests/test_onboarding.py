"""End-to-end and unit tests. Run with:  python -m unittest discover -s tests -v   (or pytest)"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import requests  # noqa: E402

from clients.graph_client import GraphClient  # noqa: E402
from clients.servicedesk_client import ServiceDeskClient  # noqa: E402
from fakes import E3, FakeGraph, FakeSDP  # noqa: E402
from helpers.config import load_configuration  # noqa: E402
from helpers.errors import StepOrderError, ValidationError, ApiError  # noqa: E402
from helpers.http_client import RetryHttpClient  # noqa: E402
from helpers.state import StateStore  # noqa: E402
from helpers.transformation import choose_available_alias, email_candidates, extract_source_fields  # noqa: E402
from helpers.validation import validate_input  # noqa: E402
from onboard import parse_args  # noqa: E402
import workflow  # noqa: E402
from workflow import STEP_ORDER, StepContext, handle_failure, run_step  # noqa: E402

workflow.print = lambda *args, **kwargs: None  # keep report markdown out of the test output

SAMPLE = json.loads((ROOT / "tests" / "sample-ticket.json").read_text())
DOMAIN = "sonacomstar.com"


def load_test_config() -> tuple[dict[str, Any], dict[str, Any]]:
    config, mapping = load_configuration(ROOT / "configs" / "onboarding.yaml")
    config["assignments"]["user_groups"]["aliases"] = {"Finance Team": "SG-Finance"}
    config["report"]["verify_delay_seconds"] = 0
    return config, mapping


class Harness:
    def __init__(self, ticket: dict[str, Any] | None = None, dry_run: bool = False, config_patch=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="onboarding-test-"))
        self.config, self.mapping = load_test_config()
        if config_patch:
            config_patch(self.config)
        self.graph = FakeGraph()
        self.sdp = FakeSDP(ticket or SAMPLE)
        self.dirs = {"state": self.tmp / "state", "reports": self.tmp / "reports", "cache": self.tmp / "cache"}
        self.store = StateStore(self.dirs["state"])
        self.dry_run = dry_run

    def ctx(self) -> StepContext:
        # A fresh context per step mirrors Dagu launching a new process per step.
        return StepContext(
            ticket_id="12345",
            config=self.config,
            mapping=self.mapping,
            store=self.store,
            dirs=self.dirs,
            graph_factory=lambda _cid: self.graph,
            sdp_factory=lambda: self.sdp,
            dry_run=self.dry_run,
            dag_run_id="run-1",
        )

    def run(self, steps=STEP_ORDER) -> None:
        for step in steps:
            run_step(step, self.ctx())

    def state(self) -> dict[str, Any]:
        return self.store.load("12345")

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def ticket_with(**udf: Any) -> dict[str, Any]:
    ticket = copy.deepcopy(SAMPLE)
    ticket["request"]["udf_fields"].update(udf)
    return ticket


class EmailGenerationTests(unittest.TestCase):
    def test_first_dot_last_when_free(self):
        alias, address = choose_available_alias(email_candidates("Asha", "Verma", "E10234", DOMAIN), DOMAIN, set())
        self.assertEqual(address, "asha.verma@sonacomstar.com")

    def test_employee_code_appended_when_taken(self):
        taken = {"asha.verma@sonacomstar.com"}
        _, address = choose_available_alias(email_candidates("Asha", "Verma", "E10234", DOMAIN), DOMAIN, taken)
        self.assertEqual(address, "asha.vermae10234@sonacomstar.com")

    def test_separator_and_accents(self):
        cands = email_candidates("José", "D'Souza", "E-77", DOMAIN, separator=".")
        self.assertEqual(cands, ["jose.dsouza", "jose.dsouza.e-77"])

    def test_both_taken_fails(self):
        taken = {"asha.verma@sonacomstar.com", "asha.vermae10234@sonacomstar.com"}
        with self.assertRaises(ValidationError):
            choose_available_alias(email_candidates("Asha", "Verma", "E10234", DOMAIN), DOMAIN, taken)


class ExtractionAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.config, self.mapping = load_test_config()

    def test_multi_value_fields(self):
        fields = extract_source_fields(SAMPLE, self.mapping)
        self.assertEqual(fields["user_groups"], ["Finance Team", "All Staff Gurugram"])
        self.assertEqual(fields["intune_apps"], ["Adobe Acrobat Reader", "Microsoft Teams"])
        self.assertEqual(fields["license_sku"], ["Microsoft 365 E3"])
        self.assertEqual(fields["status"], "Approved")
        self.assertEqual(fields["location"], "Gurugram HQ")

    def test_future_hire_date_is_allowed(self):
        fields = extract_source_fields(ticket_with(employee_hire_date="2099-01-01"), self.mapping)
        validate_input(fields, self.config)

    def test_missing_fields_and_unapproved_status_reported_together(self):
        ticket = ticket_with(first_name="", staff_no=None)
        ticket["request"]["status"]["name"] = "Open"
        with self.assertRaises(ValidationError) as caught:
            validate_input(extract_source_fields(ticket, self.mapping), self.config)
        message = str(caught.exception)
        for expected in ("first_name", "staff_no", "not approved"):
            self.assertIn(expected, message)

    def test_missing_license_rejected(self):
        with self.assertRaises(ValidationError):
            validate_input(extract_source_fields(ticket_with(license_sku=None), self.mapping), self.config)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_happy_path(self):
        h = self.h
        h.run()
        state = h.state()
        user = h.graph.users[state["user"]["id"]]
        self.assertEqual(user["userPrincipalName"], "asha.verma@sonacomstar.com")
        payload = user["created_payload"]
        self.assertEqual(payload["usageLocation"], "IN")
        self.assertEqual(payload["employeeHireDate"], "2026-11-02T00:00:00Z")
        self.assertEqual(payload["onPremisesExtensionAttributes"]["extensionAttribute1"], "M2")
        self.assertEqual(h.graph.managers[user["id"]], h.graph._find("priya.singh@sonacomstar.com")["id"])
        self.assertEqual([l["skuId"] for l in user["assignedLicenses"]], [E3])

        member_names = {g["displayName"] for g in h.graph.groups if user["id"] in h.graph.members[g["id"]]}
        self.assertEqual(
            member_names,
            {"SG-Finance", "All Staff Gurugram", "RB-Standard-Office", "APP-Adobe Acrobat Reader", "APP-Microsoft Teams"},
        )
        self.assertTrue(all(c["ok"] for c in state["report"]["verification"]))
        self.assertTrue(Path(state["report"]["paths"]["markdown"]).exists())
        self.assertTrue(h.sdp.closed)
        self.assertEqual(len(h.sdp.notes), 1)  # report note
        self.assertTrue(all(s["status"] == "completed" for s in state["steps"].values()))
        # group map downloaded once and shared by validate + 3 assignment steps
        self.assertEqual(h.graph.calls.count("list_groups"), 1)
        # no password anywhere on disk
        for path in h.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(payload["passwordProfile"]["password"], path.read_text(errors="ignore"))

    def test_existing_address_gets_employee_code(self):
        self.h.graph.add_user("someone@sonacomstar.com", proxies=["SMTP:someone@sonacomstar.com", "smtp:asha.verma@sonacomstar.com"])
        self.h.run()
        self.assertEqual(self.h.state()["user"]["upn"], "asha.vermae10234@sonacomstar.com")
        self.assertTrue(self.h.state()["user"]["used_fallback_address"])

    def test_soft_deleted_address_counts_as_taken(self):
        self.h.graph.deleted_users.append({"userPrincipalName": "asha.verma@sonacomstar.com", "proxyAddresses": []})
        self.h.run(STEP_ORDER[:3])
        self.assertEqual(self.h.state()["user"]["upn"], "asha.vermae10234@sonacomstar.com")

    def test_unknown_group_stops_before_user_is_created(self):
        h = Harness(ticket=ticket_with(user_groups=["Finance Team", "Does Not Exist"]))
        try:
            run_step("fetch", h.ctx())
            with self.assertRaises(ValidationError) as caught:
                run_step("validate", h.ctx())
            self.assertIn("Does Not Exist", str(caught.exception))
            with self.assertRaises(StepOrderError):
                run_step("create-user", h.ctx())
            self.assertNotIn("create_user", h.graph.calls)
            self.assertEqual(h.graph.calls.count("list_groups"), 1)  # map was just downloaded; no pointless refresh
            state = h.state()
            self.assertEqual(state["failure"]["step"], "validate")

            result = handle_failure(h.ctx())
            self.assertTrue(Path(result["markdown"]).name.endswith("-FAILED.md"))
            self.assertIn("Failed step", h.sdp.notes[-1])
            self.assertFalse(h.sdp.closed)
        finally:
            h.close()

    def test_group_created_after_cache_is_found_by_refresh(self):
        h = self.h
        h.run(STEP_ORDER[:2])  # caches the group map on disk
        h.graph.add_group("SG-Brand-New")
        h.sdp.ticket["request"]["udf_fields"]["user_groups"] = ["SG-Brand-New"]
        h.run(STEP_ORDER[:2])  # cached map misses -> refreshed once -> resolves
        self.assertEqual(h.graph.calls.count("list_groups"), 2)
        self.assertEqual(h.state()["plan"]["assignments"]["user_groups"][0]["group_name"], "SG-Brand-New")

    def test_privileged_dynamic_and_synced_groups_refused(self):
        for groups, bundles, expected in (
            (["Tier0 Admins"], ["Standard-Office"], "role-assignable"),
            (["Dynamic Sales"], ["Standard-Office"], "dynamic"),
            (["Finance Team"], ["Synced"], "on-premises"),
            (["Finance Team"], ["SG-Finance"], "outside the allowed prefix"),
        ):
            h = Harness(ticket=ticket_with(user_groups=groups, rule_bundles=bundles))
            try:
                run_step("fetch", h.ctx())
                with self.assertRaises(ValidationError) as caught:
                    run_step("validate", h.ctx())
                self.assertIn(expected, str(caught.exception))
            finally:
                h.close()

    def test_duplicate_staff_number_rejected(self):
        self.h.graph.add_user("old.account@sonacomstar.com", employee_id="E10234")
        run_step("fetch", self.h.ctx())
        with self.assertRaises(ValidationError):
            run_step("validate", self.h.ctx())

    def test_no_license_seats_rejected_in_validate(self):
        self.h.graph.skus[0]["consumedUnits"] = 10
        run_step("fetch", self.h.ctx())
        with self.assertRaises(ValidationError) as caught:
            run_step("validate", self.h.ctx())
        self.assertIn("no available seats", str(caught.exception))

    def test_failure_stops_workflow_and_rerun_resumes_idempotently(self):
        h = self.h
        h.graph.fail_on["add_group_member"] = ApiError("boom", 503, retryable=True)
        h.run(STEP_ORDER[:4])
        with self.assertRaises(ApiError):
            run_step("add-groups", h.ctx())
        with self.assertRaises(StepOrderError):
            run_step("assign-rule-bundles", h.ctx())
        self.assertEqual(h.state()["failure"]["step"], "add-groups")

        h.run()  # whole DAG again, same ticket
        state = h.state()
        self.assertEqual(h.graph.calls.count("create_user"), 1)
        self.assertEqual(h.graph.calls.count("assign_licenses"), 1)
        self.assertEqual(state["steps"]["create-user"]["details"]["skipped"], "user already created by an earlier run")
        self.assertIsNone(state["failure"])
        self.assertTrue(h.sdp.closed)

    def test_interrupted_create_is_adopted_on_rerun(self):
        h = self.h
        real_create = h.graph.create_user

        def create_then_lose_response(payload):
            real_create(payload)  # Graph created the user...
            raise ApiError("Network error calling POST /users: read timed out", retryable=True)  # ...but we never heard back

        h.graph.create_user = create_then_lose_response
        h.run(STEP_ORDER[:2])
        with self.assertRaises(ApiError):
            run_step("create-user", h.ctx())
        self.assertNotIn("id", h.state()["user"])
        self.assertEqual(h.state()["user"]["pending_upn"], "asha.verma@sonacomstar.com")

        h.graph.create_user = real_create
        h.run()  # full re-run: validate must not call it a duplicate, create-user adopts it
        state = h.state()
        self.assertTrue(state["steps"]["create-user"]["details"]["adopted"])
        self.assertEqual(len([u for u in h.graph.users.values() if u.get("employeeId") == "E10234"]), 1)
        self.assertTrue(any("password is unknown" in w for w in state["warnings"]))
        self.assertTrue(h.sdp.closed)

    def test_dry_run_after_real_run_does_not_touch_manager(self):
        h = self.h
        h.run(STEP_ORDER[:3])
        new_manager = h.graph.add_user("new.boss@sonacomstar.com")
        h.sdp.ticket["request"]["udf_fields"]["reporting_manager"] = "new.boss@sonacomstar.com"
        h.dry_run = True
        h.run(STEP_ORDER[:3])
        user_id = h.state()["user"]["id"]
        self.assertNotEqual(h.graph.managers[user_id], new_manager)
        self.assertEqual(h.state()["steps"]["create-user"]["details"]["would_change_manager_to"], new_manager)

    def test_failure_cleared_when_failed_step_later_succeeds(self):
        h = self.h
        h.run(STEP_ORDER[:3])
        h.graph.fail_on["assign_licenses"] = ApiError("boom", 503)
        with self.assertRaises(ApiError):
            run_step("assign-license", h.ctx())
        h.run(STEP_ORDER[3:])  # resume from the failed step (as a Dagu retry would)
        self.assertIsNone(h.state()["failure"])
        self.assertEqual(h.state()["report"]["outcome"], "SUCCESS")

    def test_dry_run_changes_nothing(self):
        h = Harness(dry_run=True)
        try:
            h.run()
            self.assertNotIn("create_user", h.graph.calls)
            self.assertNotIn("assign_licenses", h.graph.calls)
            self.assertNotIn("add_group_member", h.graph.calls)
            self.assertFalse(h.sdp.closed)
            self.assertEqual(h.sdp.notes, [])
            state = h.state()
            self.assertEqual(state["report"]["outcome"], "DRY RUN")
            self.assertEqual(len(state["assignments"]["intune_apps"]["would_add"]), 2)
            self.assertEqual(state["plan"]["user_preview"]["upn"], "asha.verma@sonacomstar.com")
        finally:
            h.close()

    def test_mixing_dry_run_and_real_steps_is_refused(self):
        h = Harness(dry_run=True)
        try:
            h.run(STEP_ORDER[:2])
            h.dry_run = False
            with self.assertRaises(StepOrderError):
                run_step("create-user", h.ctx())
        finally:
            h.close()

    def test_intune_app_resolution_via_app_assignment(self):
        def patch(config):
            config["assignments"]["intune_apps"].update(resolve_via="intune_app", name_prefix="", restrict_to_prefix=False)

        h = Harness(config_patch=patch)
        try:
            teams_group = h.graph.add_group("Intune-Teams-Required")
            h.graph.apps = [
                {
                    "id": "app1",
                    "displayName": "Microsoft Teams",
                    "assignments": [
                        {"intent": "required", "target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": teams_group}},
                        {"intent": "uninstall", "target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": "x"}},
                    ],
                },
                {"id": "app2", "displayName": "Adobe Acrobat Reader", "assignments": [
                    {"intent": "available", "target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget",
                                                        "groupId": h.graph.groups[3]["id"]}}]},
            ]
            h.run()
            user_id = h.state()["user"]["id"]
            self.assertIn(user_id, h.graph.members[teams_group])
            added = h.state()["assignments"]["intune_apps"]["added"]
            self.assertEqual({a["app_name"] for a in added}, {"Microsoft Teams", "Adobe Acrobat Reader"})
        finally:
            h.close()

    def test_verification_mismatch_blocks_close(self):
        h = self.h
        h.run(STEP_ORDER[:7])
        user_id = h.state()["user"]["id"]
        for members in h.graph.members.values():
            members.discard(user_id)  # simulate a membership that did not stick
        with self.assertRaises(Exception):
            run_step("generate-report", h.ctx())
        self.assertFalse(h.sdp.closed)
        self.assertEqual(h.state()["failure"]["step"], "generate-report")


# ---------------------------------------------------------------- real clients over a fake HTTP session


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeSession(requests.Session):
    def __init__(self, responses: list[FakeResponse]):
        super().__init__()
        self.responses = list(responses)
        self.sent: list[dict[str, Any]] = []

    def request(self, method, url, **kwargs):  # type: ignore[override]
        self.sent.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def http(self, responses):
        session = FakeSession(responses)
        return RetryHttpClient({"initial_delay_seconds": 0}, session=session), session

    def test_graph_paging_and_address_collection(self):
        http, session = self.http([
            FakeResponse(200, {"value": [{"userPrincipalName": "A@x.com", "mail": None, "proxyAddresses": ["SMTP:a@x.com", "smtp:alias@x.com", "X500:/o=foo"]}],
                               "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=1"}),
            FakeResponse(200, {"value": [{"userPrincipalName": "b@x.com", "mail": "B.Mail@x.com"}]}),
            FakeResponse(200, {"value": [{"userPrincipalName": "0123456789abcdef0123456789abcdefgone@x.com"}]}),
        ])
        graph = GraphClient({}, http, "cid", token="t")
        addresses = graph.list_used_addresses(include_soft_deleted=True)
        self.assertEqual(addresses, {"a@x.com", "alias@x.com", "b@x.com", "b.mail@x.com", "gone@x.com"})
        self.assertIn("$skiptoken=1", session.sent[1]["url"])
        self.assertIn("deletedItems", session.sent[2]["url"])

    def test_graph_add_member_already_exists_is_not_an_error(self):
        http, _ = self.http([FakeResponse(400, {"error": {"message": "One or more added object references already exist for the following modified properties: 'members'."}})])
        graph = GraphClient({}, http, "cid", token="t")
        self.assertFalse(graph.add_group_member("g", "u"))

    def test_graph_retries_404_for_new_user_then_succeeds(self):
        http, session = self.http([FakeResponse(404, {"error": {}}), FakeResponse(204)])
        graph = GraphClient({}, http, "cid", token="t")
        self.assertTrue(graph.add_group_member("g", "u"))
        self.assertEqual(len(session.sent), 2)

    def test_create_user_is_not_retried_after_5xx(self):
        http, session = self.http([FakeResponse(503, {"error": {}}), FakeResponse(201, {"id": "dup"})])
        graph = GraphClient({}, http, "cid", token="t")
        with self.assertRaises(ApiError):
            graph.create_user({"userPrincipalName": "a@x.com"})
        self.assertEqual(len(session.sent), 1)

    def test_create_user_is_retried_after_throttling(self):
        http, session = self.http([FakeResponse(429, {"error": {}}, {"Retry-After": "0"}), FakeResponse(201, {"id": "u1"})])
        graph = GraphClient({}, http, "cid", token="t")
        self.assertEqual(graph.create_user({"userPrincipalName": "a@x.com"})["id"], "u1")

    def test_servicedesk_close_uses_form_input_data_and_authtoken(self):
        os.environ["SDP_API_KEY"] = "test-key"
        config, _ = load_test_config()
        http, session = self.http([FakeResponse(200, {"response_status": {"status_code": 2000, "status": "success"}})])
        sdp = ServiceDeskClient(config, http)
        sdp.close("12345", "done", "Success", True)
        sent = session.sent[0]
        self.assertEqual(sent["method"], "PUT")
        self.assertTrue(sent["url"].endswith("/api/v3/requests/12345/close"))
        self.assertEqual(sent["headers"]["authtoken"], "test-key")
        body = json.loads(sent["data"]["input_data"])
        self.assertEqual(body["request"]["closure_info"]["closure_code"], {"name": "Success"})

    def test_servicedesk_error_status_raises(self):
        os.environ["SDP_API_KEY"] = "test-key"
        config, _ = load_test_config()
        http, _ = self.http([FakeResponse(200, {"response_status": {"status_code": 4000, "status": "failed", "messages": [{"message": "mandatory"}]}})])
        with self.assertRaises(ApiError):
            ServiceDeskClient(config, http).add_note("1", "x")

    def test_cli_dry_run_flag_parsing(self):
        self.assertTrue(parse_args(["fetch", "1", "--dry-run", "true"]).dry_run)
        self.assertFalse(parse_args(["fetch", "1", "--dry-run", "false"]).dry_run)
        self.assertTrue(parse_args(["fetch", "1", "--dry-run"]).dry_run)
        self.assertFalse(parse_args(["fetch", "1"]).dry_run)


if __name__ == "__main__":
    unittest.main()
