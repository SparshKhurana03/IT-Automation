"""Microsoft Graph client (application permissions, client-credentials flow).

Application permissions required on the app registration (admin consent):
  User.ReadWrite.All               create users, set manager, read all users
  GroupMember.ReadWrite.All        add members to groups
  Group.Read.All                   build the local group map
  LicenseAssignment.ReadWrite.All  assignLicense + read subscribedSkus
                                   (or Organization.Read.All + User.ReadWrite.All)
  DeviceManagementApps.Read.All    only when assignments.intune_apps.resolve_via
                                   is "intune_app"
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator
from urllib.parse import quote

from helpers.config import env_required
from helpers.errors import ApiError, ValidationError
from helpers.http_client import RetryHttpClient

LOG = logging.getLogger("onboarding.graph")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DELETED_UPN_PREFIX = re.compile(r"^[0-9a-f]{32}", re.IGNORECASE)
# Graph can briefly return 404 for a user created seconds earlier.
NEW_OBJECT_RETRY = {404}


def _q(value: str) -> str:
    return quote(value, safe="")


def _odata_literal(value: str) -> str:
    return value.replace("'", "''")


class GraphClient:
    def __init__(self, config: dict[str, Any], http: RetryHttpClient, correlation_id: str, token: str | None = None):
        graph = config.get("graph", {})
        self.http = http
        self.base_url = graph.get("base_url", GRAPH_ROOT).rstrip("/")
        self.page_size = int(graph.get("page_size", 999))
        token = token or self._acquire_token(graph)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "client-request-id": correlation_id,
            "return-client-request-id": "true",
        }

    @staticmethod
    def _acquire_token(graph: dict[str, Any]) -> str:
        import msal  # imported lazily so offline tooling/tests do not need it

        credentials = graph.get("credentials", {})
        tenant_id = env_required(credentials.get("tenant_id_env", "ENTRA_TENANT_ID"))
        client_id = env_required(credentials.get("client_id_env", "ENTRA_CLIENT_ID"))
        client_secret = env_required(credentials.get("client_secret_env", "ENTRA_CLIENT_SECRET"))
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
        return token

    # ------------------------------------------------------------------ basics

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None, expected: set[int] | None = None, **kw: Any):
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        return self.http.request("GET", url, expected=expected or {200}, headers=self.headers, params=params, **kw)

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Iterate every item of a collection, following ``@odata.nextLink``."""
        response = self._get(path, params=params)
        while True:
            body = response.json()
            yield from body.get("value", [])
            next_link = body.get("@odata.nextLink")
            if not next_link:
                return
            response = self._get(next_link)

    # ------------------------------------------------------------------ users

    def list_used_addresses(self, include_soft_deleted: bool = False) -> set[str]:
        """Every UPN, primary mail and SMTP proxy address in the tenant (lower case)."""
        select = {"$select": "id,userPrincipalName,mail,proxyAddresses", "$top": self.page_size}
        sources = [("/users", self.get_all("/users", dict(select)))]
        if include_soft_deleted:
            sources.append(
                ("deleted users", self.get_all("/directory/deletedItems/microsoft.graph.user", dict(select)))
            )

        addresses: set[str] = set()
        count = 0
        for label, items in sources:
            for user in items:
                count += 1
                upn = user.get("userPrincipalName") or ""
                if label != "/users":
                    upn = DELETED_UPN_PREFIX.sub("", upn)  # deleted UPNs are prefixed with the object id
                for value in [upn, user.get("mail")]:
                    if value:
                        addresses.add(value.lower())
                for proxy in user.get("proxyAddresses") or []:
                    if proxy.lower().startswith("smtp:"):
                        addresses.add(proxy[5:].lower())
        LOG.info("Loaded %d addresses from %d directory users", len(addresses), count)
        return addresses

    def get_user_by_employee_id(self, employee_id: str) -> dict[str, Any] | None:
        params = {
            "$filter": f"employeeId eq '{_odata_literal(employee_id)}'",
            "$select": "id,userPrincipalName,employeeId,accountEnabled",
        }
        values = self._get("/users", params=params).json().get("value", [])
        if len(values) > 1:
            raise ValidationError(f"Multiple Entra users have employeeId {employee_id}")
        return values[0] if values else None

    def get_user(self, user_id_or_upn: str, select: str, retry_new: bool = False) -> dict[str, Any] | None:
        response = self._get(
            f"/users/{_q(user_id_or_upn)}",
            params={"$select": select},
            expected={200} if retry_new else {200, 404},
            extra_retryable=NEW_OBJECT_RETRY if retry_new else None,
        )
        return None if response.status_code == 404 else response.json()

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.http.request(
            "POST", self.base_url + "/users", expected={201}, idempotent=False, headers=self.headers, json=payload
        ).json()

    def resolve_manager(self, email_or_upn: str) -> dict[str, Any]:
        select = "id,displayName,mail,userPrincipalName,accountEnabled"
        manager = self.get_user(email_or_upn, select)
        if manager is None:
            params = {"$filter": f"mail eq '{_odata_literal(email_or_upn)}'", "$select": select}
            matches = self._get("/users", params=params).json().get("value", [])
            if len(matches) != 1:
                raise ValidationError(f"Reporting manager could not be uniquely resolved in Entra: {email_or_upn}")
            manager = matches[0]
        if manager.get("accountEnabled") is False:
            raise ValidationError(f"Reporting manager account is disabled: {email_or_upn}")
        return manager

    def assign_manager(self, user_id: str, manager_id: str) -> None:
        url = self.base_url + f"/users/{_q(user_id)}/manager/$ref"
        payload = {"@odata.id": f"{self.base_url}/users/{manager_id}"}
        self.http.request("PUT", url, expected={204}, extra_retryable=NEW_OBJECT_RETRY, headers=self.headers, json=payload)

    def get_manager_id(self, user_id: str) -> str | None:
        response = self._get(f"/users/{_q(user_id)}/manager", params={"$select": "id"}, expected={200, 404})
        return None if response.status_code == 404 else response.json().get("id")

    # ---------------------------------------------------------------- licenses

    def list_subscribed_skus(self) -> list[dict[str, Any]]:
        return self._get("/subscribedSkus").json().get("value", [])

    def get_assigned_sku_ids(self, user_id: str) -> set[str]:
        user = self.get_user(user_id, "id,assignedLicenses", retry_new=True) or {}
        return {item["skuId"].lower() for item in user.get("assignedLicenses", []) if item.get("skuId")}

    def assign_licenses(self, user_id: str, licenses: list[dict[str, Any]]) -> None:
        url = self.base_url + f"/users/{_q(user_id)}/assignLicense"
        payload = {"addLicenses": licenses, "removeLicenses": []}
        self.http.request("POST", url, expected={200}, extra_retryable=NEW_OBJECT_RETRY, headers=self.headers, json=payload)

    # ------------------------------------------------------------------ groups

    def list_groups(self) -> list[dict[str, Any]]:
        params = {
            "$select": "id,displayName,mailNickname,groupTypes,securityEnabled,mailEnabled,"
            "onPremisesSyncEnabled,isAssignableToRole,membershipRule",
            "$top": self.page_size,
        }
        return list(self.get_all("/groups", params))

    def list_member_group_ids(self, user_id: str) -> set[str]:
        """Direct group memberships of a user."""
        params = {"$select": "id", "$top": self.page_size}
        return {
            item["id"].lower()
            for item in self.get_all(f"/users/{_q(user_id)}/memberOf", params)
            if item.get("@odata.type") == "#microsoft.graph.group"
        }

    def add_group_member(self, group_id: str, user_id: str) -> bool:
        """Add a user to a group. Returns False when the user was already a member."""
        url = self.base_url + f"/groups/{_q(group_id)}/members/$ref"
        payload = {"@odata.id": f"{self.base_url}/directoryObjects/{user_id}"}
        try:
            self.http.request(
                "POST", url, expected={204}, extra_retryable=NEW_OBJECT_RETRY, headers=self.headers, json=payload
            )
        except ApiError as exc:
            if exc.status_code == 400 and "already exist" in str(exc).lower():
                return False
            raise
        return True

    # ------------------------------------------------------------------ intune

    def list_intune_apps(self) -> list[dict[str, Any]]:
        params = {"$select": "id,displayName", "$expand": "assignments"}
        return list(self.get_all("/deviceAppManagement/mobileApps", params))
