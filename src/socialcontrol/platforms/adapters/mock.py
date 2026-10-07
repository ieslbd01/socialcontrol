"""Mock adapter: a fake channel for tests and dry runs (TDD-05 section 4.7)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from socialcontrol.domain.enums import FailureClass
from socialcontrol.platforms.base import (
    AdapterError,
    AssistedPackage,
    AuthState,
    Capability,
    PlatformAdapter,
    PostView,
    PublishContext,
    PublishResult,
    RemotePost,
    final_text,
)

OUTCOMES: dict[str, FailureClass] = {
    "temporary": FailureClass.TEMPORARY,
    "rate_limit": FailureClass.RATE_LIMIT,
    "auth": FailureClass.AUTH,
    "validation": FailureClass.VALIDATION,
    "permanent": FailureClass.PERMANENT_REJECTION,
    "unknown": FailureClass.UNKNOWN,
}


class MockAdapter(PlatformAdapter):
    """Configurable fake.

    ``settings``: ``{"outcome": "success|temporary|...", "fail_n_times": k,
    "retry_after_s": n, "caption_limit": n, "supports_auto": bool}``
    Published posts are kept in ``self.published`` (shared per instance).
    """

    key = "mock"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self.supports_auto = bool(self.settings.get("supports_auto", True))
        self.published: dict[str, PublishResult] = {}
        self.calls = 0
        self._failures_left = int(self.settings.get("fail_n_times", 0))

    def get_capabilities(self) -> list[Capability]:
        limit = self.settings.get("caption_limit", 3000)
        return [
            Capability("text", max_caption_chars=limit),
            Capability("image", max_caption_chars=limit, requires_media=True),
            Capability("text_image", max_caption_chars=limit, requires_media=True),
            Capability("video", max_caption_chars=limit, requires_media=True),
        ]

    def authenticate(self) -> AuthState:
        if self.settings.get("outcome") == "auth":
            return AuthState(False, "token rejected")
        return AuthState(True, "mock ok")

    def publish(self, post: PostView, ctx: PublishContext) -> PublishResult:
        if not self.supports_auto:
            raise AdapterError(
                "assisted-only channel", FailureClass.PERMANENT_REJECTION, "NOT_AUTO"
            )
        self.calls += 1
        if ctx.dry_run:
            return PublishResult("dry-run", None, {"dry_run": True})
        if ctx.idempotency_key in self.published:  # idempotent: same key, same result
            return self.published[ctx.idempotency_key]

        outcome = self.settings.get("outcome", "success")
        # outcome != success: fail forever, or only the first ``fail_n_times`` calls
        fails = outcome != "success" and (
            "fail_n_times" not in self.settings or self._failures_left > 0
        )
        if fails:
            if "fail_n_times" in self.settings:
                self._failures_left -= 1
            retry_after = self.settings.get("retry_after_s")
            raise AdapterError(
                f"mock {outcome}",
                OUTCOMES[outcome],
                outcome.upper(),
                timedelta(seconds=retry_after) if retry_after else None,
            )

        result = PublishResult(f"mock-{len(self.published) + 1}", f"https://mock/{post.post_id}")
        self.published[ctx.idempotency_key] = result
        return result

    def find_existing(self, post: PostView, ctx: PublishContext) -> RemotePost | None:
        found = self.published.get(ctx.idempotency_key)
        return RemotePost(found.platform_post_id, found.url) if found else None

    def build_assisted_package(self, post: PostView) -> AssistedPackage:
        text = final_text(post)
        return AssistedPackage(
            text=text,
            media_links=tuple(m.url for m in post.media),
            destination_url="https://mock/channel",
            char_count=len(text),
            char_limit=self.settings.get("caption_limit", 3000),
        )
