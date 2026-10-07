import httpx
import pytest

from socialcontrol.config.settings import Settings
from socialcontrol.media.storage import LocalStorage, storage_from_settings
from socialcontrol.media.supabase_storage import StorageError, SupabaseStorage

URL = "https://abc.supabase.co"
KEY = "service-key-123"


def make(handler):
    return SupabaseStorage(URL, KEY, "media", httpx.Client(transport=httpx.MockTransport(handler)))


def test_put_uploads_with_auth_headers_and_returns_public_url():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(
            url=str(req.url),
            method=req.method,
            auth=req.headers["authorization"],
            apikey=req.headers["apikey"],
            ctype=req.headers["content-type"],
            upsert=req.headers["x-upsert"],
            body=req.content,
        )
        return httpx.Response(200, json={"Key": "media/ab/abc.jpg"})

    url = make(handler).put("ab/abc.jpg", b"JPEGDATA", "image/jpeg")
    assert url == f"{URL}/storage/v1/object/public/media/ab/abc.jpg"
    assert seen["url"] == f"{URL}/storage/v1/object/media/ab/abc.jpg" and seen["method"] == "POST"
    assert seen["auth"] == f"Bearer {KEY}" and seen["apikey"] == KEY
    assert (
        seen["ctype"] == "image/jpeg" and seen["upsert"] == "false" and seen["body"] == b"JPEGDATA"
    )


@pytest.mark.parametrize(
    "status,body",
    [(409, "{}"), (400, '{"error":"Duplicate","message":"The resource already exists"}')],
)
def test_duplicate_upload_is_idempotent(status, body):
    url = make(lambda r: httpx.Response(status, text=body)).put("k.jpg", b"x", "image/jpeg")
    assert url.endswith("/public/media/k.jpg")


def test_other_errors_raise():
    with pytest.raises(StorageError, match="500"):
        make(lambda r: httpx.Response(500, text="boom")).put("k.jpg", b"x", "image/jpeg")
    with pytest.raises(StorageError, match="401"):
        make(lambda r: httpx.Response(401, text="bad key")).put("k.jpg", b"x", "image/jpeg")


def test_keys_are_url_quoted():
    assert (
        make(lambda r: httpx.Response(200))
        .put("a b/c d.jpg", b"x", "image/jpeg")
        .endswith("a%20b/c%20d.jpg")
    )


def test_ensure_bucket_created_or_already_there():
    assert make(lambda r: httpx.Response(200, json={"name": "media"})).ensure_bucket() is True
    assert (
        make(lambda r: httpx.Response(409, text='{"error":"Duplicate"}')).ensure_bucket() is False
    )
    with pytest.raises(StorageError):
        make(lambda r: httpx.Response(403, text="no")).ensure_bucket()


def test_delete_object():
    seen = {}

    def handler(req):
        seen["m"], seen["body"] = req.method, req.content
        return httpx.Response(200, json=[])

    make(handler).delete("ab/x.jpg")
    assert seen["m"] == "DELETE" and b"ab/x.jpg" in seen["body"]
    with pytest.raises(StorageError):
        make(lambda r: httpx.Response(500)).delete("x")


def test_requires_credentials():
    with pytest.raises(StorageError):
        SupabaseStorage("", "")


def test_factory_picks_backend_from_settings(tmp_path):
    local = storage_from_settings(Settings(_env_file=None, media_dir=str(tmp_path)))
    assert isinstance(local, LocalStorage)
    remote = storage_from_settings(
        Settings(_env_file=None, supabase_url=URL, supabase_service_key=KEY, supabase_bucket="m2")
    )
    assert isinstance(remote, SupabaseStorage) and remote.bucket == "m2"


def test_local_storage_blocks_path_traversal(tmp_path):
    s = LocalStorage(tmp_path)
    assert s.put("ab/x.jpg", b"1", "image/jpeg") == "/media/ab/x.jpg"
    with pytest.raises(ValueError):
        s.put("../evil.jpg", b"1", "image/jpeg")
