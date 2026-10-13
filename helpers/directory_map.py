"""Local map of Entra groups / Intune apps and resolution of ticket values.

The user-group, rule-bundle and Intune-app steps all work the same way:

1. Load every Entra group once into a local map (cached on disk for
   ``directory_cache.ttl_minutes`` so the three steps share one download).
2. Map each value selected on the SDP ticket to exactly one group, using
   (in order) the configured alias table, the exact display name, then
   ``name_prefix`` + value. A cache miss refreshes the map once before failing.
3. Refuse groups whose membership cannot (or must not) be managed from here:
   dynamic, on-premises synced, mail-enabled security / distribution lists,
   role-assignable groups, or groups outside the category's allowed prefix.

All values are resolved *before* any membership change, so one bad value
fails the step without leaving a half-applied set of groups.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from helpers.errors import ValidationError

LOG = logging.getLogger("onboarding.directory")
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


@dataclass
class ResolvedTarget:
    requested: str
    group_id: str
    group_name: str
    matched_by: str
    app_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JsonCache:
    """Tiny file cache with a time-to-live."""

    def __init__(self, directory: Path, ttl_minutes: float):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = float(ttl_minutes) * 60

    def load(self, name: str) -> list[dict[str, Any]] | None:
        path = self.directory / f"{name}.json"
        if not path.exists() or self.ttl_seconds <= 0:
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if time.time() - float(data.get("fetched_at", 0)) > self.ttl_seconds:
            return None
        return data.get("items", [])

    def save(self, name: str, items: list[dict[str, Any]]) -> None:
        path = self.directory / f"{name}.json"
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=self.directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": time.time(), "items": items}, handle)
        os.replace(tmp_name, path)


class _Index:
    def __init__(self, items: list[dict[str, Any]]):
        self.by_id = {item["id"].lower(): item for item in items}
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            self.by_name.setdefault(normalize_name(item.get("displayName") or ""), []).append(item)


class DirectoryMap:
    def __init__(self, graph: Any, cache: JsonCache):
        self.graph = graph
        self.cache = cache
        self._groups: _Index | None = None
        self._apps: _Index | None = None
        self._refreshed: set[str] = set()

    # ------------------------------------------------------------ loading

    def _load(self, name: str, fetch: Callable[[], list[dict[str, Any]]], refresh: bool) -> _Index:
        items = None if refresh else self.cache.load(name)
        if items is None:
            LOG.info("Downloading %s from Microsoft Graph", name)
            items = fetch()
            self.cache.save(name, items)
            self._refreshed.add(name)
        return _Index(items)

    def groups(self, refresh: bool = False) -> _Index:
        if self._groups is None or refresh:
            self._groups = self._load("groups", self.graph.list_groups, refresh)
        return self._groups

    def apps(self, refresh: bool = False) -> _Index:
        if self._apps is None or refresh:
            self._apps = self._load("intune_apps", self.graph.list_intune_apps, refresh)
        return self._apps

    # ------------------------------------------------------------ resolution

    def resolve(self, category: str, values: list[str], settings: dict[str, Any]) -> list[ResolvedTarget]:
        values = list(dict.fromkeys(list(settings.get("default_values") or []) + list(values or [])))
        if not values:
            return []
        resolved, errors = self._resolve_all(values, settings)
        if errors and settings.get("resolve_via", "group") == "intune_app" and "intune_apps" not in self._refreshed:
            self.apps(refresh=True)
            resolved, errors = self._resolve_all(values, settings)
        if errors and "groups" not in self._refreshed:
            self.groups(refresh=True)
            resolved, errors = self._resolve_all(values, settings)
        if errors:
            label = settings.get("label", category)
            raise ValidationError(f"{label}: " + "; ".join(errors))

        unique: dict[str, ResolvedTarget] = {}
        for target in resolved:
            unique.setdefault(target.group_id.lower(), target)
        return list(unique.values())

    def _resolve_all(self, values: list[str], settings: dict[str, Any]) -> tuple[list[ResolvedTarget], list[str]]:
        resolved: list[ResolvedTarget] = []
        errors: list[str] = []
        for value in values:
            try:
                if settings.get("resolve_via", "group") == "intune_app":
                    resolved.extend(self._resolve_app(value, settings))
                else:
                    resolved.append(self._resolve_group(value, settings))
            except ValidationError as exc:
                errors.append(str(exc))
        return resolved, errors

    @staticmethod
    def _alias(value: str, settings: dict[str, Any]) -> tuple[str, bool]:
        aliases = {normalize_name(k): v for k, v in (settings.get("aliases") or {}).items()}
        target = aliases.get(normalize_name(value))
        return (target, True) if target else (value, False)

    def _resolve_group(self, value: str, settings: dict[str, Any], via: str | None = None) -> ResolvedTarget:
        index = self.groups()
        target_name, aliased = self._alias(value, settings)
        prefix = settings.get("name_prefix") or ""

        if GUID.match(target_name):
            group = index.by_id.get(target_name.lower())
            if not group:
                raise ValidationError(f"'{value}' is not a known Entra group id")
            matched_by = "object_id"
        else:
            names = [target_name]
            if prefix and not normalize_name(target_name).startswith(normalize_name(prefix)):
                names.append(prefix + target_name)
            group, matched_by = None, ""
            for position, name in enumerate(names):
                matches = index.by_name.get(normalize_name(name), [])
                if len(matches) > 1:
                    ids = ", ".join(m["id"] for m in matches)
                    raise ValidationError(f"'{value}' matches {len(matches)} Entra groups named '{name}' ({ids})")
                if matches:
                    group = matches[0]
                    matched_by = "alias" if aliased else ("display_name" if position == 0 else "prefix")
                    break
            if group is None:
                raise ValidationError(f"no Entra group found for '{value}' (tried: {', '.join(names)})")

        self._check_assignable(value, group, settings)
        return ResolvedTarget(value, group["id"], group.get("displayName") or "", via or matched_by)

    def _resolve_app(self, value: str, settings: dict[str, Any]) -> list[ResolvedTarget]:
        target_name, _ = self._alias(value, settings)
        matches = self.apps().by_name.get(normalize_name(target_name), [])
        if not matches:
            raise ValidationError(f"no Intune app named '{target_name}'")
        if len(matches) > 1:
            raise ValidationError(f"'{target_name}' matches {len(matches)} Intune apps")
        app = matches[0]

        intents = {i.lower() for i in settings.get("intune_assignment_intents", ["required", "available"])}
        group_ids = []
        for assignment in app.get("assignments") or []:
            target = assignment.get("target") or {}
            if target.get("@odata.type") == "#microsoft.graph.groupAssignmentTarget" and str(
                assignment.get("intent", "")
            ).lower() in intents:
                group_ids.append(target.get("groupId"))
        group_ids = [g for g in dict.fromkeys(group_ids) if g]
        if not group_ids:
            raise ValidationError(
                f"Intune app '{app['displayName']}' has no group assignment with intent {sorted(intents)}"
            )
        if len(group_ids) > 1 and settings.get("app_group_selection", "single") != "all":
            raise ValidationError(
                f"Intune app '{app['displayName']}' is assigned to {len(group_ids)} groups; set an alias to the "
                "exact group or app_group_selection: all"
            )
        results = []
        for group_id in group_ids:
            target = self._resolve_group(group_id, {**settings, "aliases": {}}, via="intune_app")
            target.requested, target.app_name = value, app["displayName"]
            results.append(target)
        return results

    @staticmethod
    def _check_assignable(value: str, group: dict[str, Any], settings: dict[str, Any]) -> None:
        name = group.get("displayName") or group["id"]
        prefix = settings.get("name_prefix") or ""
        if settings.get("restrict_to_prefix") and prefix and not normalize_name(name).startswith(normalize_name(prefix)):
            raise ValidationError(f"group '{name}' for '{value}' is outside the allowed prefix '{prefix}'")
        deny = {normalize_name(n) for n in settings.get("deny_groups") or []}
        if normalize_name(name) in deny or group["id"].lower() in deny:
            raise ValidationError(f"group '{name}' is on the deny list")
        if group.get("isAssignableToRole"):
            raise ValidationError(f"group '{name}' is role-assignable (grants admin roles) and cannot be requested by ticket")
        if "DynamicMembership" in (group.get("groupTypes") or []):
            raise ValidationError(f"group '{name}' has dynamic membership; members cannot be added")
        if group.get("onPremisesSyncEnabled"):
            raise ValidationError(f"group '{name}' is synced from on-premises AD; add the user there instead")
        is_unified = "Unified" in (group.get("groupTypes") or [])
        if group.get("mailEnabled") and not is_unified:
            kind = "mail-enabled security group" if group.get("securityEnabled") else "distribution list"
            raise ValidationError(f"group '{name}' is a {kind}; Graph cannot manage its membership")


def resolve_licenses(graph: Any, values: list[str], license_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ticket license values to subscribed SKUs and check that seats are free."""
    aliases = {normalize_name(k): v for k, v in (license_cfg.get("sku_aliases") or {}).items()}
    requested = list(dict.fromkeys(list(values or []) or list(license_cfg.get("default_skus") or [])))
    if not requested:
        return []

    skus = graph.list_subscribed_skus()
    by_part = {normalize_name(s.get("skuPartNumber", "")): s for s in skus}
    by_id = {s.get("skuId", "").lower(): s for s in skus}
    disabled_cfg = {normalize_name(k): v for k, v in (license_cfg.get("disabled_plans") or {}).items()}

    resolved, errors = [], []
    for value in requested:
        part = aliases.get(normalize_name(value), value)
        sku = by_id.get(part.lower()) if GUID.match(part) else by_part.get(normalize_name(part))
        if not sku:
            errors.append(f"license '{value}' (SKU '{part}') is not subscribed in this tenant")
            continue
        if str(sku.get("capabilityStatus", "Enabled")) != "Enabled":
            errors.append(f"license '{sku['skuPartNumber']}' is {sku.get('capabilityStatus')}")
            continue
        enabled = int((sku.get("prepaidUnits") or {}).get("enabled", 0))
        available = enabled - int(sku.get("consumedUnits", 0))

        plan_ids = {normalize_name(p.get("servicePlanName", "")): p.get("servicePlanId") for p in sku.get("servicePlans") or []}
        disabled = []
        for plan_name in disabled_cfg.get(normalize_name(sku["skuPartNumber"]), []):
            plan_id = plan_ids.get(normalize_name(plan_name))
            if not plan_id:
                errors.append(f"service plan '{plan_name}' not found in {sku['skuPartNumber']}")
            else:
                disabled.append(plan_id)
        resolved.append(
            {
                "requested": value,
                "sku_id": sku["skuId"],
                "sku_part_number": sku["skuPartNumber"],
                "available_units": available,
                "disabled_plans": disabled,
            }
        )
    if errors:
        raise ValidationError("License: " + "; ".join(errors))
    return resolved
