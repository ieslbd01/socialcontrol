"""Retry policy (PRD-07 PUB-20..27)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from socialcontrol.domain.enums import FailureClass


class RetryAction(StrEnum):
    RETRY = "RETRY"
    PAUSE_ACCOUNT = "PAUSE_ACCOUNT"  # AUTH: no retry until reconnect
    FINAL = "FINAL"  # FAILED_FINAL


@dataclass(frozen=True)
class RetryPolicy:
    delays: tuple[timedelta, ...] = (
        timedelta(minutes=15),
        timedelta(hours=1),
        timedelta(hours=6),
    )
    unknown_max_retries: int = 2
    rate_limit_default_wait: timedelta = timedelta(hours=1)
    rate_limit_max_waits: int = 3


@dataclass(frozen=True)
class Decision:
    action: RetryAction
    next_retry_at: datetime | None = None
    reason: str = ""
    notify_critical: bool = field(default=False)


def decide(
    failure: FailureClass,
    retries_done: int,
    now: datetime,
    policy: RetryPolicy | None = None,
    retry_after: timedelta | None = None,
) -> Decision:
    """What to do after a failed attempt.

    ``retries_done`` counts retries already performed (0 after the first failure).
    """
    policy = policy or RetryPolicy()
    if failure == FailureClass.AUTH:
        return Decision(RetryAction.PAUSE_ACCOUNT, None, "authentication failed", True)

    if failure in (FailureClass.VALIDATION, FailureClass.PERMANENT_REJECTION):
        return Decision(RetryAction.FINAL, None, f"{failure.value}: not retryable", True)

    if failure == FailureClass.RATE_LIMIT:
        if retries_done >= policy.rate_limit_max_waits:
            return Decision(RetryAction.FINAL, None, "rate limit persisted", True)
        wait = retry_after if retry_after is not None else policy.rate_limit_default_wait
        return Decision(RetryAction.RETRY, now + wait, "rate limited")

    limit = (
        min(len(policy.delays), policy.unknown_max_retries)
        if failure == FailureClass.UNKNOWN
        else len(policy.delays)
    )
    if retries_done >= limit:
        return Decision(RetryAction.FINAL, None, "retries exhausted", True)
    return Decision(RetryAction.RETRY, now + policy.delays[retries_done], "temporary failure")
