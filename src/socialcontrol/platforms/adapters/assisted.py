"""Generic assisted adapter: builds ready-to-post packages for any channel.

Used for every account in ASSISTED mode and as the safe fallback when an account is
set to AUTO but no real API adapter exists yet. It never calls a platform API.
"""

from __future__ import annotations

from typing import Any

from socialcontrol.database.seed import load_seed
from socialcontrol.platforms.base import (
    AssistedPackage,
    AuthState,
    Capability,
    NotSupportedError,
    PlatformAdapter,
    PostView,
    PublishContext,
    PublishResult,
    RemotePost,
    final_text,
)

HINTS: dict[str, tuple[str, ...]] = {
    "whatsapp_channel": (
        "Open your WhatsApp Channel, tap the pencil/＋ icon, paste the text and attach the media.",
    ),
    "linkedin_company": (
        "Post as the company page. Paste the text first; LinkedIn builds the link preview from the URL.",
        "Add the image or document before publishing.",
    ),
    "google_business": (
        "In Business Profile choose Add update. Pick a button (e.g. Learn more) and use the link below.",
    ),
    "youtube": (
        "Upload the video in YouTube Studio, paste the title and description, then publish or schedule it.",
    ),
    "facebook_page": ("Post as the Page, not your personal profile.",),
    "instagram": (
        "Instagram posts need the image or video. Paste the caption and publish from the Instagram app.",
    ),
}


class AssistedAdapter(PlatformAdapter):
    supports_auto = False

    def __init__(self, platform_key: str, settings: dict[str, Any] | None = None) -> None:
        self.key = platform_key
        self.settings = settings or {}
        seed = load_seed()
        self._caps = [
            Capability(
                post_type=c["post_type"],
                max_caption_chars=c.get("max_caption_chars"),
                max_hashtags=c.get("max_hashtags"),
                requires_media=bool(c.get("requires_media", False)),
                media_types=tuple(c.get("media_types", [])),
            )
            for c in seed["capabilities"]
            if c["platform_key"] == platform_key
        ]

    def get_capabilities(self) -> list[Capability]:
        return self._caps

    def authenticate(self) -> AuthState:
        return AuthState(True, "assisted: no API connection needed")

    def publish(self, post: PostView, ctx: PublishContext) -> PublishResult:
        raise NotSupportedError("assisted channel: the owner publishes by hand")

    def find_existing(self, post: PostView, ctx: PublishContext) -> RemotePost | None:
        return None

    def build_assisted_package(self, post: PostView) -> AssistedPackage:
        text = final_text(post)
        if post.title and self.key in ("youtube", "google_business", "linkedin_company"):
            text = f"{post.title}\n\n{text}"
        cap = next((c for c in self._caps if c.post_type == post.post_type), None)
        return AssistedPackage(
            text=text,
            media_links=tuple(m.url for m in post.media),
            destination_url=self.settings.get("destination_url"),
            hints=HINTS.get(self.key, ()),
            char_count=len(text),
            char_limit=cap.max_caption_chars if cap else None,
        )
