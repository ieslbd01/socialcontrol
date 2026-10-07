"""Platform adapter interface and DTOs (TDD-05)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from socialcontrol.domain.enums import FailureClass


@dataclass(frozen=True)
class MediaView:
    url: str
    mime: str
    bytes: int = 0
    sha256: str = ""
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None


@dataclass(frozen=True)
class PostView:
    post_id: str
    post_type: str
    language: str
    caption: str
    title: str | None = None
    link_url: str | None = None
    hashtags: tuple[str, ...] = ()
    media: tuple[MediaView, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    post_type: str
    max_caption_chars: int | None = None
    max_hashtags: int | None = None
    requires_media: bool = False
    media_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class PublishContext:
    idempotency_key: str
    attempt_no: int = 1
    run_id: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class PublishResult:
    platform_post_id: str
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemotePost:
    platform_post_id: str
    url: str | None = None


@dataclass(frozen=True)
class AssistedPackage:
    text: str
    media_links: tuple[str, ...] = ()
    destination_url: str | None = None
    hints: tuple[str, ...] = ()
    char_count: int = 0
    char_limit: int | None = None


@dataclass(frozen=True)
class AuthState:
    ok: bool
    detail: str = ""
    expires_at: datetime | None = None


class AdapterError(Exception):
    """Raised by adapters; carries the failure class used by the retry policy."""

    def __init__(
        self,
        message: str,
        failure_class: FailureClass = FailureClass.UNKNOWN,
        code: str = "",
        retry_after: timedelta | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.code = code
        self.retry_after = retry_after
        self.raw = raw or {}


class NotSupportedError(AdapterError):
    """Operation not available (e.g. publish on an assisted-only channel)."""

    def __init__(self, message: str = "operation not supported") -> None:
        super().__init__(message, FailureClass.PERMANENT_REJECTION, "NOT_SUPPORTED")


class PlatformAdapter(ABC):
    key: str = ""
    supports_auto: bool = True

    @abstractmethod
    def get_capabilities(self) -> list[Capability]: ...

    @abstractmethod
    def authenticate(self) -> AuthState: ...

    @abstractmethod
    def publish(self, post: PostView, ctx: PublishContext) -> PublishResult: ...

    @abstractmethod
    def find_existing(self, post: PostView, ctx: PublishContext) -> RemotePost | None: ...

    @abstractmethod
    def build_assisted_package(self, post: PostView) -> AssistedPackage: ...

    def refresh_auth(self) -> AuthState:
        return self.authenticate()

    def token_expiry(self) -> datetime | None:
        return None

    def map_error(self, exc: Exception) -> FailureClass:
        if isinstance(exc, AdapterError):
            return exc.failure_class
        return FailureClass.UNKNOWN

    def validate_post(self, post: PostView) -> list[ValidationIssue]:
        """Pure capability-based validation (no network)."""
        caps = {c.post_type: c for c in self.get_capabilities()}
        cap = caps.get(post.post_type)
        if cap is None:
            return [ValidationIssue("E030", f"post type {post.post_type!r} not supported")]
        issues: list[ValidationIssue] = []
        text = _final_text(post)
        if cap.max_caption_chars is not None and len(text) > cap.max_caption_chars:
            issues.append(
                ValidationIssue("E032", f"text is {len(text)} chars; limit {cap.max_caption_chars}")
            )
        if cap.max_hashtags is not None and len(post.hashtags) > cap.max_hashtags:
            issues.append(ValidationIssue("E033", f"more than {cap.max_hashtags} hashtags"))
        if cap.requires_media and not post.media:
            issues.append(ValidationIssue("E040", "media required for this post type"))
        return issues


def _final_text(post: PostView) -> str:
    text = post.caption
    if post.link_url and "{link}" in text:
        text = text.replace("{link}", post.link_url)
    if post.hashtags:
        text = f"{text}\n{' '.join(post.hashtags)}" if text else " ".join(post.hashtags)
    return text


def final_text(post: PostView) -> str:
    """Caption with ``{link}`` resolved and hashtags appended (CNT-09/10)."""
    return _final_text(post)
