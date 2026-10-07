"""Shared enumerations (PRD-04 status model, PRD-07 failure classes)."""

from __future__ import annotations

from enum import StrEnum


class PostStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    FAILED_FINAL = "FAILED_FINAL"
    OVERDUE = "OVERDUE"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class QueueStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    DISABLED = "DISABLED"


class AccountState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class PublishMode(StrEnum):
    AUTO = "AUTO"
    ASSISTED = "ASSISTED"


class FailureClass(StrEnum):
    TEMPORARY = "TEMPORARY"
    RATE_LIMIT = "RATE_LIMIT"
    AUTH = "AUTH"
    VALIDATION = "VALIDATION"
    PERMANENT_REJECTION = "PERMANENT_REJECTION"
    UNKNOWN = "UNKNOWN"


class SlotState(StrEnum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    EMPTY = "EMPTY"
    SKIPPED = "SKIPPED"
    DONE = "DONE"


class PatternMode(StrEnum):
    STRICT = "STRICT"
    RELAXED = "RELAXED"


class Language(StrEnum):
    EN = "en"
    BN = "bn"
    EN_BN = "en+bn"


# Statuses a post may be in while it still needs to occupy / be given a slot.
SLOT_ELIGIBLE = frozenset({PostStatus.APPROVED, PostStatus.QUEUED})
# Statuses that are final for a publication (cannot be edited or re-imported over).
IMMUTABLE = frozenset(
    {PostStatus.PUBLISHING, PostStatus.AWAITING_CONFIRMATION, PostStatus.PUBLISHED}
)
