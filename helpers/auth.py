"""Microsoft Graph token acquisition.

Two modes, chosen by ``graph.auth_mode``:

app          Client-credentials flow with an app registration and client secret
             (ENTRA_TENANT_ID / ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET). Use this for
             unattended Dagu runs.

device_code  Delegated sign-in as an M365 admin account. Run ``onboard.py login``
             once: it shows a code to enter at https://microsoft.com/devicelogin,
             where you sign in normally (MFA included). The refresh token is cached
             (file mode 0600) so every later step gets a token silently. Intended for
             testing from a workstation. The signed-in account needs the User
             Administrator and License Administrator roles (or Global Administrator).

Username + password sign-in (ROPC) is deliberately not supported: Microsoft has
deprecated it, it fails for any account with MFA, and it would mean storing an
admin password in an environment variable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from helpers.config import env_required
from helpers.errors import ApiError, ConfigurationError

LOG = logging.getLogger("onboarding.auth")

APP_SCOPES = ["https://graph.microsoft.com/.default"]
# Public client of "Microsoft Graph Command Line Tools" (used by Connect-MgGraph).
DEFAULT_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
DEFAULT_DELEGATED_SCOPES = [
    "User.ReadWrite.All",
    "LicenseAssignment.ReadWrite.All",
    "Organization.Read.All",
    "Group.Read.All",
    "GroupMember.ReadWrite.All",
]


def _graph_scope(scope: str) -> str:
    return scope if scope.startswith("https://") else f"https://graph.microsoft.com/{scope}"


def auth_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("graph", {}).get("auth_mode", "app")).lower()
    if mode not in {"app", "device_code"}:
        raise ConfigurationError(f"graph.auth_mode must be 'app' or 'device_code', not {mode!r}")
    return mode


def acquire_graph_token(config: dict[str, Any], cache_path: Path | None = None, interactive: bool = False) -> str:
    graph = config.get("graph", {})
    credentials = graph.get("credentials", {})
    tenant_id = env_required(credentials.get("tenant_id_env", "ENTRA_TENANT_ID"))
    mode = auth_mode(config)
    if mode == "device_code" and not interactive and (cache_path is None or not cache_path.exists()):
        raise ConfigurationError("No cached sign-in for graph.auth_mode=device_code. Run: python onboard.py login")

    import msal  # imported lazily so offline tooling/tests do not need it

    if mode == "app":
        app = msal.ConfidentialClientApplication(
            client_id=env_required(credentials.get("client_id_env", "ENTRA_CLIENT_ID")),
            client_credential=env_required(credentials.get("client_secret_env", "ENTRA_CLIENT_SECRET")),
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        return _token_or_raise(app.acquire_token_for_client(scopes=APP_SCOPES))

    # ---- device_code (delegated) -------------------------------------------------
    device = graph.get("device_code", {})
    client_id = os.getenv(device.get("client_id_env", "ENTRA_PUBLIC_CLIENT_ID")) or device.get(
        "client_id", DEFAULT_PUBLIC_CLIENT_ID
    )
    scopes = [_graph_scope(s) for s in device.get("scopes", DEFAULT_DELEGATED_SCOPES)]
    if cache_path is None:
        raise ConfigurationError("A token cache path is required for device_code sign-in")

    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", token_cache=cache
    )

    result = None
    accounts = app.get_accounts()
    if accounts and not interactive:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result:
        if not interactive:
            raise ConfigurationError(
                "No cached sign-in for graph.auth_mode=device_code. Run: python onboard.py login"
            )
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise ApiError(f"Could not start device sign-in: {flow.get('error_description') or flow}")
        print(flow["message"], flush=True)  # "To sign in, use a web browser to open ... and enter the code ..."
        result = app.acquire_token_by_device_flow(flow)

    token = _token_or_raise(result)
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize(), encoding="utf-8")
        os.chmod(cache_path, 0o600)
    if interactive:
        claims = result.get("id_token_claims") or {}
        LOG.info("Signed in as %s", claims.get("preferred_username") or claims.get("upn") or "unknown account")
    return token


def _token_or_raise(result: dict[str, Any] | None) -> str:
    token = (result or {}).get("access_token")
    if not token:
        safe_error = (result or {}).get("error_description") or (result or {}).get("error") or "unknown token error"
        raise ApiError(f"Unable to acquire a Microsoft Graph token: {safe_error}")
    return token
