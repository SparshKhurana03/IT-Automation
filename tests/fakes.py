"""In-memory stand-ins for Microsoft Graph and ServiceDesk Plus used by the tests."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from helpers.errors import ApiError, ValidationError

E3 = "05e9a617-0261-4cee-bb44-138d3ef5d965"


def gid() -> str:
    return str(uuid.uuid4())


class FakeGraph:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.deleted_users: list[dict[str, Any]] = []
        self.managers: dict[str, str] = {}
        self.members: dict[str, set[str]] = {}
        self.calls: list[str] = []
        self.fail_on: dict[str, Exception] = {}
        self.skus = [
            {
                "skuId": E3,
                "skuPartNumber": "SPE_E3",
                "capabilityStatus": "Enabled",
                "prepaidUnits": {"enabled": 10},
                "consumedUnits": 3,
                "servicePlans": [{"servicePlanName": "YAMMER_ENTERPRISE", "servicePlanId": gid()}],
            }
        ]
        self.groups: list[dict[str, Any]] = []
        self.apps: list[dict[str, Any]] = []
        self.add_user("priya.singh@sonacomstar.com", employee_id="E00001", display="Priya Singh")
        for name in ["SG-Finance", "All Staff Gurugram", "RB-Standard-Office", "APP-Adobe Acrobat Reader", "APP-Microsoft Teams"]:
            self.add_group(name)
        self.add_group("Tier0 Admins", isAssignableToRole=True)
        self.add_group("Dynamic Sales", groupTypes=["DynamicMembership"])
        self.add_group("RB-Synced", onPremisesSyncEnabled=True)

    # helpers for test setup
    def add_user(self, upn: str, employee_id: str | None = None, display: str = "", proxies: list[str] | None = None) -> str:
        user_id = gid()
        self.users[user_id] = {
            "id": user_id,
            "userPrincipalName": upn,
            "mail": upn,
            "displayName": display,
            "employeeId": employee_id,
            "accountEnabled": True,
            "proxyAddresses": proxies or [f"SMTP:{upn}"],
            "assignedLicenses": [],
        }
        return user_id

    def add_group(self, name: str, **extra: Any) -> str:
        group = {
            "id": gid(),
            "displayName": name,
            "groupTypes": [],
            "securityEnabled": True,
            "mailEnabled": False,
            "onPremisesSyncEnabled": None,
            "isAssignableToRole": False,
        }
        group.update(extra)
        self.groups.append(group)
        self.members[group["id"]] = set()
        return group["id"]

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise self.fail_on.pop(name)

    def _find(self, key: str) -> dict[str, Any] | None:
        key = key.lower()
        for user in self.users.values():
            if user["id"].lower() == key or (user["userPrincipalName"] or "").lower() == key:
                return user
        return None

    # GraphClient interface
    def list_used_addresses(self, include_soft_deleted: bool = False) -> set[str]:
        self._maybe_fail("list_used_addresses")
        out: set[str] = set()
        users = list(self.users.values()) + (self.deleted_users if include_soft_deleted else [])
        for user in users:
            out.add(user["userPrincipalName"].lower())
            if user.get("mail"):
                out.add(user["mail"].lower())
            out |= {p[5:].lower() for p in user.get("proxyAddresses", []) if p.lower().startswith("smtp:")}
        return out

    def get_user_by_employee_id(self, employee_id: str) -> dict[str, Any] | None:
        self._maybe_fail("get_user_by_employee_id")
        matches = [u for u in self.users.values() if u.get("employeeId") == employee_id]
        return copy.deepcopy(matches[0]) if matches else None

    def get_user(self, key: str, select: str, retry_new: bool = False) -> dict[str, Any] | None:
        self._maybe_fail("get_user")
        user = self._find(key)
        return copy.deepcopy(user) if user else None

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("create_user")
        assert payload["passwordProfile"]["password"]
        if self._find(payload["userPrincipalName"]):
            raise ApiError("conflict", 400)
        user_id = self.add_user(payload["userPrincipalName"], employee_id=payload.get("employeeId"))
        self.users[user_id]["created_payload"] = payload
        return {"id": user_id, "userPrincipalName": payload["userPrincipalName"]}

    def resolve_manager(self, email_or_upn: str) -> dict[str, Any]:
        self._maybe_fail("resolve_manager")
        user = self._find(email_or_upn)
        if not user:
            raise ValidationError(f"Reporting manager could not be uniquely resolved in Entra: {email_or_upn}")
        return copy.deepcopy(user)

    def assign_manager(self, user_id: str, manager_id: str) -> None:
        self._maybe_fail("assign_manager")
        self.managers[user_id] = manager_id

    def get_manager_id(self, user_id: str) -> str | None:
        return self.managers.get(user_id)

    def list_subscribed_skus(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.skus)

    def get_assigned_sku_ids(self, user_id: str) -> set[str]:
        return {l["skuId"].lower() for l in self.users[user_id]["assignedLicenses"]}

    def assign_licenses(self, user_id: str, licenses: list[dict[str, Any]]) -> None:
        self._maybe_fail("assign_licenses")
        self.users[user_id]["assignedLicenses"].extend(licenses)
        for lic in licenses:
            for sku in self.skus:
                if sku["skuId"] == lic["skuId"]:
                    sku["consumedUnits"] += 1

    def list_groups(self) -> list[dict[str, Any]]:
        self.calls.append("list_groups")
        return copy.deepcopy(self.groups)

    def list_member_group_ids(self, user_id: str) -> set[str]:
        return {g.lower() for g, members in self.members.items() if user_id in members}

    def add_group_member(self, group_id: str, user_id: str) -> bool:
        self._maybe_fail("add_group_member")
        if user_id in self.members[group_id]:
            return False
        self.members[group_id].add(user_id)
        return True

    def list_intune_apps(self) -> list[dict[str, Any]]:
        self.calls.append("list_intune_apps")
        return copy.deepcopy(self.apps)


class FakeSDP:
    def __init__(self, ticket: dict[str, Any]):
        self.ticket = copy.deepcopy(ticket)
        self.notes: list[str] = []
        self.resolution: str | None = None
        self.extra_fields: dict[str, Any] | None = None
        self.closed = False

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.ticket)

    def add_note(self, ticket_id: str, html: str, show_to_requester: bool = False) -> None:
        assert show_to_requester is False
        self.notes.append(html)

    def set_resolution(self, ticket_id: str, html: str, extra_fields: dict[str, Any] | None = None) -> None:
        self.resolution, self.extra_fields = html, extra_fields

    def close(self, ticket_id: str, comments: str, closure_code: str | None, requester_ack: bool) -> None:
        self.closed = True
        self.ticket["request"]["status"]["name"] = "Closed"
