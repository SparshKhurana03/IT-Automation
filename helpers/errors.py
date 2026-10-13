"""Exception hierarchy shared by every onboarding step.

Each exception maps to a distinct process exit code so Dagu (and the
administrator reading the run log) can tell *why* a step stopped.
"""

from __future__ import annotations


class LifecycleError(Exception):
    """Base lifecycle error."""

    exit_code = 1


class ValidationError(LifecycleError):
    """ServiceDesk request (or directory state) failed business validation."""

    exit_code = 2


class ConfigurationError(LifecycleError):
    """Invalid or incomplete configuration."""

    exit_code = 3


class ApiError(LifecycleError):
    """Remote system call failed."""

    exit_code = 4

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class StepOrderError(LifecycleError):
    """A step was started before the step it depends on completed."""

    exit_code = 5


class VerificationError(LifecycleError):
    """Post-provisioning verification found a mismatch."""

    exit_code = 6
