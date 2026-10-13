"""Per-ticket state shared between Dagu steps.

Every Dagu step is a separate process, so results (Entra object ID, resolved
groups, ...) are handed to the next step through a JSON file per ticket.
The file never contains the generated password or any token.

Keeping state per ticket also makes a re-run safe: a step that already did
its work (user created, license present, already a group member) is skipped.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helpers.errors import ValidationError

TICKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_ticket_id(ticket_id: str) -> str:
    ticket_id = (ticket_id or "").strip()
    if not TICKET_ID_PATTERN.fullmatch(ticket_id):
        raise ValidationError(f"Invalid ticket ID {ticket_id!r}: use letters, digits, '-' or '_' only")
    return ticket_id


def new_state(ticket_id: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "dag_run_id": None,
        "correlation_id": None,
        "dry_run": False,
        "ticket": {},
        "plan": {},
        "user": {},
        "license": {},
        "assignments": {},
        "report": {},
        "ticket_closed": False,
        "steps": {},
        "failure": None,
        "warnings": [],
    }


class StateStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def path(self, ticket_id: str) -> Path:
        return self.directory / f"{validate_ticket_id(ticket_id)}.json"

    def exists(self, ticket_id: str) -> bool:
        return self.path(ticket_id).exists()

    def load(self, ticket_id: str) -> dict[str, Any]:
        path = self.path(ticket_id)
        if not path.exists():
            return new_state(ticket_id)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        path = self.path(state["ticket_id"])
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
