"""Supabase Storage backend (REST API, no SDK). Same protocol as LocalStorage."""

from __future__ import annotations

from urllib.parse import quote

import httpx


class StorageError(RuntimeError):
    pass


class SupabaseStorage:
    """Uploads to a **public** bucket so channels such as Instagram can fetch the media by URL.

    Object keys are content hashes (sha256), so URLs are unguessable and uploads are idempotent.
    """

    def __init__(
        self,
        url: str,
        service_key: str,
        bucket: str = "media",
        client: httpx.Client | None = None,
    ) -> None:
        if not url or not service_key:
            raise StorageError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        self.base = url.rstrip("/")
        self.bucket = bucket
        self.client = client or httpx.Client(timeout=120)
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def public_url(self, key: str) -> str:
        return f"{self.base}/storage/v1/object/public/{self.bucket}/{quote(key)}"

    def put(self, key: str, data: bytes, mime: str) -> str:
        r = self.client.post(
            f"{self.base}/storage/v1/object/{self.bucket}/{quote(key)}",
            content=data,
            headers={**self._headers, "Content-Type": mime, "x-upsert": "false"},
        )
        if r.status_code in (200, 201):
            return self.public_url(key)
        body = r.text[:300]
        # Same key twice means the same bytes (sha256 key): treat as success.
        if r.status_code == 409 or ("Duplicate" in body or "already exists" in body):
            return self.public_url(key)
        raise StorageError(f"upload failed ({r.status_code}): {body}")

    def delete(self, key: str) -> None:
        r = self.client.request(
            "DELETE",
            f"{self.base}/storage/v1/object/{self.bucket}",
            json={"prefixes": [key]},
            headers=self._headers,
        )
        if r.status_code not in (200, 204):
            raise StorageError(f"delete failed ({r.status_code}): {r.text[:200]}")

    def ensure_bucket(self) -> bool:
        """Create the public bucket if missing. Returns True if it was created."""
        r = self.client.post(
            f"{self.base}/storage/v1/bucket",
            json={"id": self.bucket, "name": self.bucket, "public": True},
            headers=self._headers,
        )
        if r.status_code in (200, 201):
            return True
        if r.status_code in (400, 409) and ("already exists" in r.text or "Duplicate" in r.text):
            return False
        raise StorageError(f"could not create bucket ({r.status_code}): {r.text[:200]}")
