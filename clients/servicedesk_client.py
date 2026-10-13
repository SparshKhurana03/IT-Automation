"""ServiceDesk Plus (on-premises) REST API v3 client.

On-premises v3 calls send ``authtoken`` (technician API key) and the
``application/vnd.manageengine.sdp.v3+json`` Accept header. Write calls post
the JSON body as a form field named ``input_data``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import requests

from helpers.config import env_required
from helpers.errors import ApiError, ConfigurationError
from helpers.http_client import RetryHttpClient

LOG = logging.getLogger("onboarding.servicedesk")


class ServiceDeskClient:
    def __init__(self, config: dict[str, Any], http: RetryHttpClient):
        self.config = config["servicedesk"]
        self.http = http
        self.base_url = self.config["base_url"].rstrip("/")
        self.headers = {"Accept": "application/vnd.manageengine.sdp.v3+json"}
        self.auth: tuple[str, str] | None = None

        auth_type = str(self.config.get("auth", "apikey")).lower()
        credentials = self.config.get("credentials", {})
        if auth_type == "apikey":
            token = env_required(credentials.get("token_env", "SDP_API_KEY"))
            self.headers[credentials.get("header_name", "authtoken")] = token
        elif auth_type == "bearer":
            self.headers["Authorization"] = f"Bearer {env_required(credentials.get('token_env', 'SDP_TOKEN'))}"
        elif auth_type == "basic":
            self.auth = (
                env_required(credentials.get("username_env", "SDP_USERNAME")),
                env_required(credentials.get("password_env", "SDP_PASSWORD")),
            )
        else:
            raise ConfigurationError(f"Unsupported ServiceDesk auth type: {auth_type}")

    # ---------------------------------------------------------------- helpers

    def _url(self, key: str, default: str, ticket_id: str) -> str:
        return self.base_url + self.config.get(key, default).format(ticket_id=quote(str(ticket_id), safe=""))

    @staticmethod
    def _check(response: requests.Response, action: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(f"ServiceDesk {action}: response is not JSON: {response.text[:300]}") from exc
        status = body.get("response_status")
        statuses = status if isinstance(status, list) else [status] if status else []
        for item in statuses:
            if isinstance(item, dict) and str(item.get("status", "success")).lower() != "success":
                raise ApiError(f"ServiceDesk {action} failed: {json.dumps(item)[:500]}", response.status_code)
        return body

    def _write(self, method: str, url: str, payload: dict[str, Any], action: str) -> dict[str, Any]:
        response = self.http.request(
            method,
            url,
            expected={200, 201},
            idempotent=method != "POST",  # a retried POST could add the same note twice
            headers=self.headers,
            auth=self.auth,
            data={"input_data": json.dumps(payload)},
        )
        return self._check(response, action)

    # ---------------------------------------------------------------- API

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        url = self._url("ticket_path", "/api/v3/requests/{ticket_id}", ticket_id)
        response = self.http.request("GET", url, expected={200}, headers=self.headers, auth=self.auth)
        return self._check(response, "get request")

    def add_note(self, ticket_id: str, html: str, show_to_requester: bool = False) -> None:
        notes = self.config.get("notes", {})
        # Older on-prem builds use "request_note"; newer builds renamed the entity to "note".
        entity = notes.get("entity", "request_note")
        payload = {
            entity: {
                "description": html,
                "show_to_requester": show_to_requester,
                "notify_technician": bool(notes.get("notify_technician", False)),
                "mark_first_response": False,
                "add_to_linkedrequests": False,
            }
        }
        self._write("POST", self._url("note_path", "/api/v3/requests/{ticket_id}/notes", ticket_id), payload, "add note")

    def set_resolution(self, ticket_id: str, resolution_html: str, extra_fields: dict[str, Any] | None = None) -> None:
        request: dict[str, Any] = {"resolution": {"content": resolution_html}}
        request.update(extra_fields or {})
        url = self._url("ticket_path", "/api/v3/requests/{ticket_id}", ticket_id)
        self._write("PUT", url, {"request": request}, "set resolution")

    def close(self, ticket_id: str, comments: str, closure_code: str | None, requester_ack: bool) -> None:
        closure: dict[str, Any] = {"requester_ack_resolution": requester_ack, "closure_comments": comments}
        if closure_code:
            closure["closure_code"] = {"name": closure_code}
        url = self._url("close_path", "/api/v3/requests/{ticket_id}/close", ticket_id)
        self._write("PUT", url, {"request": {"closure_info": closure}}, "close request")
