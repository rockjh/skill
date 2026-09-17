"""Canonical generated-project paths for the API test domain."""

from pathlib import Path


BRUNO = Path("bruno")
CONTRACTS = Path("contracts")
CONSTRAINTS = Path("constraints")
EXECUTION = Path("execution")
RESULTS = Path("results")
GLOBAL_RESULTS = RESULTS / "global"
MODULE_RESULTS = RESULTS / "modules"
GLOBAL_EVIDENCE = GLOBAL_RESULTS / "evidence"
MODULE_EVIDENCE = MODULE_RESULTS / "evidence"
LOGS = RESULTS / "logs"
