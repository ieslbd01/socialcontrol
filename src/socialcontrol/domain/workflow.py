"""Post status state machine and approval hash (PRD-04)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from socialcontrol.domain.enums import PostStatus as S


class TransitionError(ValueError):
    """Illegal status transition."""


ALLOWED: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.IN_REVIEW, S.APPROVED, S.CANCELLED, S.ARCHIVED}),
    S.IN_REVIEW: frozenset({S.APPROVED, S.DRAFT, S.CANCELLED, S.ARCHIVED}),  # reject -> DRAFT
    S.APPROVED: frozenset({S.IN_REVIEW, S.QUEUED, S.SCHEDULED, S.CANCELLED, S.SKIPPED}),
    S.QUEUED: frozenset({S.IN_REVIEW, S.SCHEDULED, S.CANCELLED, S.SKIPPED}),
    S.SCHEDULED: frozenset(
        {
            S.IN_REVIEW,
            S.PUBLISHING,
            S.OVERDUE,
            S.CANCELLED,
            S.SKIPPED,
            S.APPROVED,  # slot released
        }
    ),
    S.PUBLISHING: frozenset(
        {S.PUBLISHED, S.AWAITING_CONFIRMATION, S.FAILED, S.NEEDS_ATTENTION, S.SCHEDULED}
    ),
    S.AWAITING_CONFIRMATION: frozenset({S.PUBLISHED, S.SKIPPED, S.OVERDUE}),
    S.FAILED: frozenset({S.RETRYING, S.FAILED_FINAL}),
    S.RETRYING: frozenset({S.PUBLISHING, S.FAILED_FINAL, S.CANCELLED}),
    S.FAILED_FINAL: frozenset({S.DRAFT, S.IN_REVIEW, S.RETRYING, S.CANCELLED, S.SKIPPED}),
    S.OVERDUE: frozenset({S.SCHEDULED, S.PUBLISHING, S.PUBLISHED, S.SKIPPED, S.CANCELLED}),
    S.NEEDS_ATTENTION: frozenset({S.PUBLISHED, S.RETRYING, S.SKIPPED, S.CANCELLED, S.FAILED_FINAL}),
    S.SKIPPED: frozenset({S.DRAFT, S.ARCHIVED}),
    S.CANCELLED: frozenset({S.DRAFT, S.ARCHIVED}),
    S.PUBLISHED: frozenset({S.ARCHIVED}),
    S.ARCHIVED: frozenset(),
}


def can_transition(old: S, new: S) -> bool:
    return new in ALLOWED[old]


def check_transition(old: S, new: S) -> None:
    if not can_transition(old, new):
        raise TransitionError(f"illegal transition {old.value} -> {new.value}")


def is_publishable(status: S) -> bool:
    """Only scheduled/retrying posts reach the publisher (REV-01).

    A queue with require_approval=False lets imports enter as APPROVED directly,
    but a post is never publishable without having passed through APPROVED.
    """
    return status in {S.SCHEDULED, S.RETRYING}


APPROVAL_FIELDS = (
    "post_type",
    "language",
    "title",
    "caption",
    "link_url",
    "hashtags",
    "media_sha256",
    "account_id",
)


def approval_hash(post: Mapping[str, Any]) -> str:
    """Stable hash of everything the owner approved (REV-06).

    Publishing recomputes it; a mismatch means the post changed after approval.
    """
    payload = {k: post.get(k) for k in APPROVAL_FIELDS}
    for key in ("hashtags", "media_sha256"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            payload[key] = list(value)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
