"""One-shot job runner (no daemon). Dry-run default; no paid grok in tests."""

from .plan import ApprovedExecutionPlan, PlanError, build_approved_plan
from .worker import RunOutcome, execute_one_shot

__all__ = [
    "ApprovedExecutionPlan",
    "PlanError",
    "RunOutcome",
    "build_approved_plan",
    "execute_one_shot",
]
