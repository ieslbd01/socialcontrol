"""Storage backends for media (TDD-01 section 6). Supabase/R2 implement the same protocol later."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from socialcontrol.config.settings import Settings
    from socialcontrol.media.supabase_storage import SupabaseStorage


class LocalStorage:
    """Writes media under a local directory (development and the local-first dashboard)."""

    def __init__(self, root: str | Path, base_url: str = "/media") -> None:
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")

    def put(self, key: str, data: bytes, mime: str) -> str:
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError("unsafe storage key")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return f"{self.base_url}/{key}"

    def get(self, key: str) -> bytes:
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ValueError("unsafe storage key")
        return target.read_bytes()


def storage_from_settings(settings: Settings) -> LocalStorage | SupabaseStorage:
    """Supabase Storage when configured, otherwise a local folder (development)."""
    if settings.supabase_url and settings.supabase_service_key:
        from socialcontrol.media.supabase_storage import SupabaseStorage

        return SupabaseStorage(
            settings.supabase_url, settings.supabase_service_key, settings.supabase_bucket
        )
    return LocalStorage(settings.media_dir)
