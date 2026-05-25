"""Backwards-compat re-export shim for :mod:`bob.sub_agent.runner`.

PRD 0006 / issue 0045 moves the sub-agent runner into a structured
package under :mod:`bob.sub_agent`. Several call sites (the FastAPI
boot in :mod:`bob.main`, tests in
``backend/tests/test_sub_agent_runner.py`` and
``backend/tests/test_orchestrator.py``) still import the legacy module
path; we keep this shim so the migration lands without a coordinated
rename pass.

New code should import from :mod:`bob.sub_agent` directly. The shim
will be removed after every caller is migrated.

The legacy constant ``MAX_PROGRESS_ITERATIONS`` is preserved as the
default for :attr:`bob.sub_agent.policy.SubAgentPolicy.max_iterations`
— grep-friendly for code reading older comments.
"""

from __future__ import annotations

from bob.sub_agent.runner import (
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_TOKEN_CAP,
    REASON_TOOL_FAILED,
    REASON_USER_CANCELLED,
    REASON_WALL_CLOCK_CAP,
    SubAgentRunner,
)

# Legacy alias retained for greps; ``SubAgentPolicy.max_iterations`` is the
# new dial. Stays at 10 to mirror the historical cap.
MAX_PROGRESS_ITERATIONS = 10

__all__ = [
    "MAX_PROGRESS_ITERATIONS",
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_USER_CANCELLED",
    "REASON_WALL_CLOCK_CAP",
    "SubAgentRunner",
]
