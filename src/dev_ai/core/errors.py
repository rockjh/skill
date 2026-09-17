"""Stable process exit codes and structured command errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1
    ARGUMENT = 2
    CREDENTIAL = 3
    NOT_FOUND = 4
    AMBIGUOUS = 5
    UNAVAILABLE = 6
    UNAUTHORIZED = 7
    GATE_FAILED = 8
    TEST_FAILED = 10


@dataclass(slots=True)
class DevAIError(Exception):
    code: str
    message: str
    exit_code: ExitCode = ExitCode.INTERNAL
    hint: str = ""
    details_path: str = ""

    def __str__(self) -> str:
        return self.message


def classify_failure(command: str, message: str, return_code: int) -> DevAIError:
    """Map legacy domain failures to the public semantic exit contract."""

    text = message.lower()
    if return_code == int(ExitCode.ARGUMENT) and ("usage:" in text or "error:" in text):
        return DevAIError("INVALID_ARGUMENT", message, ExitCode.ARGUMENT)
    if any(token in text for token in ("credential", "credentials", "secret is missing", "token is missing")):
        return DevAIError("CREDENTIAL_INVALID", message, ExitCode.CREDENTIAL)
    if any(token in text for token in ("does not exist", "not found", "no such file", "missing target")):
        return DevAIError("TARGET_NOT_FOUND", message, ExitCode.NOT_FOUND)
    if any(token in text for token in ("ambiguous", "cannot be uniquely", "multiple matches")):
        return DevAIError("TARGET_AMBIGUOUS", message, ExitCode.AMBIGUOUS)
    if any(token in text for token in ("unavailable", "connection refused", "timed out", "executable was not found")):
        return DevAIError("EXTERNAL_UNAVAILABLE", message, ExitCode.UNAVAILABLE)
    if any(token in text for token in ("authorization", "not authorized", "permission denied", "requires approval")):
        return DevAIError("AUTHORIZATION_REQUIRED", message, ExitCode.UNAUTHORIZED)
    if return_code == int(ExitCode.GATE_FAILED):
        return DevAIError("GATE_FAILED", message, ExitCode.GATE_FAILED)
    if return_code == int(ExitCode.TEST_FAILED):
        return DevAIError("TEST_FAILED", message, ExitCode.TEST_FAILED)
    if command.endswith((".run", ".reconcile", ".aggregate")):
        return DevAIError("TEST_FAILED", message, ExitCode.TEST_FAILED)
    return DevAIError("GATE_FAILED", message, ExitCode.GATE_FAILED)
