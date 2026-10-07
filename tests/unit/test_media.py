import io
import zipfile

import pytest

from socialcontrol.media.matcher import MatchRequest, match_media
from socialcontrol.media.zipsafe import UnsafeZipError, read_zip, sniff

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"0" * 32


def make_zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_sniff():
    assert sniff(JPEG) == "image/jpeg" and sniff(PNG) == "image/png"
    assert sniff(MP4) == "video/mp4" and sniff(PDF) == "application/pdf"
    assert sniff(b"garbage") is None


def test_reads_valid_media_with_hash():
    res = read_zip(
        make_zip({"C001.jpg": JPEG, "sub/C002.PNG": PNG, "C003.mp4": MP4, "doc.pdf": PDF})
    )
    assert sorted(f.name for f in res.files) == ["C001.jpg", "C002.PNG", "C003.mp4", "doc.pdf"]
    assert res.rejected == []
    assert len(res.files[0].sha256) == 64


def test_rejects_wrong_content_and_unsupported_types_but_keeps_good_files():
    res = read_zip(
        make_zip({"good.jpg": JPEG, "fake.jpg": b"not an image", "run.exe": b"MZ", "noext": b"x"})
    )
    assert [f.name for f in res.files] == ["good.jpg"]
    assert {n for n, _ in res.rejected} == {"fake.jpg", "run.exe", "noext"}


def test_ignores_os_metadata():
    res = read_zip(
        make_zip({".DS_Store": b"x", "Thumbs.db": b"x", "__x/.hidden": b"x", "a.jpg": JPEG})
    )
    assert [f.name for f in res.files] == ["a.jpg"] and res.rejected == []


@pytest.mark.parametrize(
    "name", ["../evil.jpg", "/abs/evil.jpg", "a/../../evil.jpg", "C:/evil.jpg"]
)
def test_zip_slip_rejected(name):
    with pytest.raises(UnsafeZipError):
        read_zip(make_zip({name: JPEG}))


def test_symlink_rejected():
    with pytest.raises(UnsafeZipError, match="symlink"):
        read_zip(make_zip({"a.jpg": JPEG}, symlink="link.jpg"))


def test_zip_bomb_ratio_rejected():
    bomb = make_zip({"big.png": PNG + b"\x00" * 50_000_000})
    with pytest.raises(UnsafeZipError, match="ratio"):
        read_zip(bomb)


def test_total_size_cap():
    with pytest.raises(UnsafeZipError, match="too large"):
        read_zip(make_zip({"a.jpg": JPEG + b"1" * 100}), max_total=50)


def test_not_a_zip():
    with pytest.raises(UnsafeZipError):
        read_zip(b"hello")


def test_too_many_entries(monkeypatch):
    monkeypatch.setattr("socialcontrol.media.zipsafe.MAX_ENTRIES", 2)
    with pytest.raises(UnsafeZipError, match="too many"):
        read_zip(make_zip({"a.jpg": JPEG, "b.jpg": JPEG, "c.jpg": JPEG}))


# ---------------------------------------------------------------- matcher
def req(pid, cid=None, media=(), needs=True):
    return MatchRequest(pid, cid or pid.split("-")[0], list(media), needs)


def test_priority_explicit_then_post_then_content():
    avail = ["C001-FB.jpg", "C001.jpg", "custom.jpg", "C002.jpg"]
    res = match_media([req("C001-FB"), req("C001-LI"), req("C002-FB", media=["custom.jpg"])], avail)
    assert res.matched == {
        "C001-FB": ["C001-FB.jpg"],  # post id beats content id
        "C001-LI": ["C001.jpg"],  # falls back to content id (shared)
        "C002-FB": ["custom.jpg"],  # explicit beats everything
    }
    assert res.unmatched_files == ["C002.jpg"]


def test_ambiguous_extensions_error():
    res = match_media([req("C001-FB")], ["C001-FB.jpg", "C001-FB.png"])
    assert res.errors["C001-FB"][0] == "E042" and res.matched == {}


def test_missing_required_vs_optional():
    res = match_media([req("C001-FB"), req("C002-FB", needs=False)], [])
    assert res.errors["C001-FB"][0] == "E040" and "C002-FB" not in res.errors


def test_explicit_missing_file_is_e041_and_urls_pass_through():
    res = match_media(
        [req("C001-FB", media=["nope.jpg"]), req("C002-FB", media=["https://x.test/a.jpg"])],
        ["other.jpg"],
    )
    assert res.errors["C001-FB"][0] == "E041"
    assert res.matched["C002-FB"] == ["https://x.test/a.jpg"]


def test_case_insensitive_and_carousel_numbering():
    avail = ["c005-ig_2.jpg", "C005-IG_1.JPG", "C005-IG_10.jpg"]
    res = match_media([req("C005-IG")], avail)
    assert res.matched["C005-IG"] == ["C005-IG_1.JPG", "c005-ig_2.jpg", "C005-IG_10.jpg"]


def test_multiple_explicit_files_keep_order():
    res = match_media([req("C001-IG", media=["b.jpg", "a.jpg"])], ["a.jpg", "b.jpg"])
    assert res.matched["C001-IG"] == ["b.jpg", "a.jpg"]
