"""Configuration loading.

``onboarding.yaml`` holds business rules and execution settings and points at
``servicedesk-entra-mapping.yaml`` (SDP field paths) through
``configuration.field_mapping_file``. Secrets never live in YAML; they are read
from environment variables named in the config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from helpers.errors import ConfigurationError


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and require a mapping at its root."""
    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML syntax in file: {path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"Unable to read configuration file: {path}") from exc

    if value is None:
        raise ConfigurationError(f"Configuration file is empty: {path}")
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected a YAML object at the root of {path}")
    return value


def load_configuration(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(config, mapping)``; the mapping path is relative to the config file."""
    config = load_yaml(config_path)
    mapping_ref = config.get("configuration", {}).get("field_mapping_file")
    if not mapping_ref:
        raise ConfigurationError("configuration.field_mapping_file is required")
    mapping_path = (config_path.parent / mapping_ref).resolve()
    mapping = load_yaml(mapping_path)

    if not config.get("defaults", {}).get("domain"):
        raise ConfigurationError("defaults.domain is required")
    if not mapping.get("source", {}).get("fields"):
        raise ConfigurationError("source.fields is missing from the mapping YAML")
    return config, mapping


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigurationError(f"Required environment variable is not set: {name}")
    return value


def data_directories(config: dict[str, Any]) -> dict[str, Path]:
    """Resolve the state / report / cache directories.

    ``ONBOARDING_DATA_DIR`` overrides ``paths.data_dir`` so the same config can
    be used inside the Dagu container and on a workstation.
    """
    paths = config.get("paths", {})
    base = Path(os.getenv("ONBOARDING_DATA_DIR") or paths.get("data_dir") or "./data")
    return {
        "state": base / paths.get("state_subdir", "state"),
        "reports": base / paths.get("reports_subdir", "reports"),
        "cache": base / paths.get("cache_subdir", "cache"),
    }
