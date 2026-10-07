import pytest

from socialcontrol.imports.csv_importer import Level, Registry, errors_csv, parse_csv

HEADER = (
    "content_id,platform,account,queue,post_type,language,title,caption,link,"
    "media_file,hashtags,evergreen,approved\n"
)


@pytest.fixture
def reg() -> Registry:
    return Registry(
        platform_codes={
            "facebook_page": "FB",
            "instagram": "IG",
            "youtube": "YT",
            "linkedin_company": "LI",
            "whatsapp_channel": "WA",
        },
        accounts={
            ("facebook_page", "iesl_page"): "CONNECTED",
            ("instagram", "iesl_ig"): "CONNECTED",
            ("youtube", "iesl_yt"): "CONNECTED",
            ("linkedin_company", "iesl_company"): "CONNECTED",
            ("whatsapp_channel", "iesl_channel"): "CONNECTED",
            ("facebook_page", "old_page"): "DISABLED",
        },
        queues={
            ("facebook_page", "iesl_page"): {"main"},
            ("linkedin_company", "iesl_company"): {"technical"},
        },
        capabilities={
            ("facebook_page", "text_image"): {"max_caption_chars": 100, "requires_media": True},
            ("facebook_page", "text"): {"max_caption_chars": 100},
            ("instagram", "image"): {
                "max_caption_chars": 2200,
                "max_hashtags": 3,
                "requires_media": True,
            },
            ("youtube", "video"): {
                "max_caption_chars": 5000,
                "requires_media": True,
                "extra": {"max_title_chars": 20},
            },
            ("linkedin_company", "text"): {"max_caption_chars": 3000},
            ("whatsapp_channel", "text"): {},
            ("facebook_page", "link"): {"max_caption_chars": 100},
        },
    )


def run(reg, body, header=HEADER):
    return parse_csv(header + body, reg)


def codes(res, level=None):
    return [(i.row, i.code) for i in res.issues if level is None or i.level == level]


def test_valid_row_produces_post(reg):
    res = run(
        reg, 'C001,FB,iesl_page,main,text,en,,"Hello {link}",https://ieslbd.com/a,,#cal,yes,no\n'
    )
    assert codes(res, Level.ERROR) == []
    row = res.rows[0]
    assert row.post_id == "C001-FB" and row.platform_key == "facebook_page"
    assert row.evergreen and not row.approved and row.hashtags == ["#cal"]
    assert res.summary == {"valid": 1, "warning": 0, "error": 0}


def test_missing_required_column_is_file_error(reg):
    res = parse_csv("content_id,caption\nC001,hi\n", reg)
    assert codes(res) == [(0, "E001")] and res.rows == []


def test_empty_file(reg):
    assert codes(parse_csv("", reg)) == [(0, "E001")]


def test_bom_and_unknown_column(reg):
    res = parse_csv(
        "﻿" + HEADER.strip() + ",mystery\nC001,FB,iesl_page,main,text,en,,hi,,,#a,,,x\n", reg
    )
    assert (0, "W007") in codes(res, Level.WARNING)
    assert len(res.rows) == 1


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ("X1,FB,iesl_page,main,text,en,,hi,,,#a,,\n", "E010"),
        ("C001,ZZ,iesl_page,main,text,en,,hi,,,#a,,\n", "E020"),
        ("C001,FB,nope,main,text,en,,hi,,,#a,,\n", "E021"),
        ("C001,FB,old_page,main,text,en,,hi,,,#a,,\n", "E023"),
        ("C001,FB,iesl_page,nope,text,en,,hi,,,#a,,\n", "E022"),
        ("C001,FB,iesl_page,main,carousel,en,,hi,,,#a,,\n", "E030"),
        ("C001,FB,iesl_page,main,text,fr,,hi,,,#a,,\n", "E060"),
        ("C001,FB,iesl_page,main,text,en,,hi,,,#a,maybe,\n", "E060"),
        ("C001,FB,iesl_page,main,text,en,,,,,#a,,\n", "E031"),
        ("C001,FB,iesl_page,main,text,en,,hi,not a url,,#a,,\n", "E050"),
    ],
)
def test_row_errors(reg, row, code):
    res = run(reg, row)
    assert code in [c for _, c in codes(res, Level.ERROR)]
    assert res.rows == []


def test_caption_limit_counts_resolved_link(reg):
    link = "https://ieslbd.com/" + "x" * 90
    res = run(reg, f'C001,FB,iesl_page,main,text,en,,"{"a" * 50} {{link}}",{link},,#a,,\n')
    assert (1, "E032") in codes(res, Level.ERROR)


def test_hashtag_limit(reg):
    res = run(reg, "C001,IG,iesl_ig,,image,en,,hi,,C001.jpg,#a #b #c #d,,\n")
    assert (1, "E033") in codes(res, Level.ERROR)


def test_youtube_title_required_and_limited(reg):
    res = run(reg, "C001,YT,iesl_yt,,video,en,,desc,,C001.mp4,#a,,\n")
    assert (1, "E031") in codes(res, Level.ERROR)
    res = run(
        reg, "C001,YT,iesl_yt,,video,en,This title is way too long for it,desc,,C001.mp4,#a,,\n"
    )
    assert (1, "E032") in codes(res, Level.ERROR)


def test_duplicate_post_in_file_and_suffix(reg):
    body = (
        "C001,FB,iesl_page,main,text,en,,hi,,,#a,,\n"
        "C001,FB,iesl_page,main,text,en,,hi again,,,#a,,\n"
    )
    res = run(reg, body)
    assert (2, "E011") in codes(res, Level.ERROR) and len(res.rows) == 1
    res2 = parse_csv(
        HEADER.strip() + ",post_suffix\n"
        "C001,FB,iesl_page,main,text,en,,hi,,,#a,,,\n"
        "C001,FB,iesl_page,main,text,en,,hi again,,,#a,,,2\n",
        reg,
    )
    assert [r.post_id for r in res2.rows] == ["C001-FB", "C001-FB-2"]


def test_published_post_blocks_and_approved_warns(reg):
    reg.existing_status = {"C001-FB": "PUBLISHED", "C002-FB": "APPROVED"}
    res = run(
        reg,
        "C001,FB,iesl_page,main,text,en,,hi,,,#a,,\nC002,FB,iesl_page,main,text,en,,hi,,,#a,,\n",
    )
    assert (1, "E070") in codes(res, Level.ERROR)
    assert (2, "W008") in codes(res, Level.WARNING)
    assert [r.post_id for r in res.rows] == ["C002-FB"]


def test_warnings_do_not_block(reg):
    res = run(reg, "C001,FB,iesl_page,,text,en,,hi,,,,,\n")  # no queue, no hashtags
    assert {"W003", "W009"} <= {c for _, c in codes(res, Level.WARNING)}
    assert len(res.rows) == 1
    assert res.summary == {"valid": 0, "warning": 1, "error": 0}


def test_language_sanity_warnings(reg):
    res = run(reg, "C001,WA,iesl_channel,,text,bn,,only english text,,,#a,,\n")
    assert (1, "W010") in codes(res, Level.WARNING)
    res = run(reg, "C002,WA,iesl_channel,,text,en,,তাপমাত্রা ক্যালিব্রেশন কেন জরুরি,,,#a,,\n")
    assert (1, "W010") in codes(res, Level.WARNING)


def test_bangla_caption_accepted_and_round_trips(reg):
    cap = "তাপমাত্রা ক্যালিব্রেশন 🌡️"
    res = run(reg, f"C001,WA,iesl_channel,,text,bn,,{cap},,,#a,,\n")
    assert res.rows[0].caption == cap and res.rows[0].language == "bn"


def test_platform_accepts_key_or_code_case_insensitive(reg):
    res = run(
        reg,
        "C001,Facebook_Page,iesl_page,main,text,en,,hi,,,#a,,\nC002,fb,iesl_page,main,text,en,,hi,,,#a,,\n",
    )
    assert [r.post_id for r in res.rows] == ["C001-FB", "C002-FB"]


def test_hashtags_normalised(reg):
    res = run(reg, "C001,FB,iesl_page,main,text,en,,hi,,,cal pharma #gxp,,\n")
    assert res.rows[0].hashtags == ["#cal", "#pharma", "#gxp"]


def test_multiple_media_split(reg):
    res = run(reg, "C001,FB,iesl_page,main,text_image,en,,hi,,a.jpg; b.jpg,#a,,\n")
    assert res.rows[0].media_files == ["a.jpg", "b.jpg"]


def test_summary_counts_100_rows_like_prd(reg):
    lines = [f"C{i:03d},FB,iesl_page,main,text,en,,hi,,,#a,,\n" for i in range(1, 98)]
    lines.append("C098,FB,iesl_page,,text,en,,hi,,,#a,,\n")  # warning W009
    lines.append("C099,FB,iesl_page,main,text,en,,,,,#a,,\n")  # error E031
    lines.append("C100,FB,iesl_page,main,text,en,,hi,,,#a,,\n")
    res = run(reg, "".join(lines))
    assert res.summary == {"valid": 98, "warning": 1, "error": 1}


def test_errors_csv_exports_only_bad_rows_and_neutralises_formulas(reg):
    raw = (
        HEADER
        + "C001,FB,iesl_page,main,text,en,,hi,,,#a,,\nC002,FB,iesl_page,main,text,en,,=HYPERLINK(1),not a url,,#a,,\n"
    )
    res = parse_csv(raw, reg)
    out = errors_csv(raw, res.issues)
    lines = out.strip().splitlines()
    assert len(lines) == 2 and lines[0].endswith("error_codes,message")
    assert "E050" in lines[1] and "'=HYPERLINK(1)" in lines[1]


def test_too_many_rows_stops(reg, monkeypatch):
    monkeypatch.setattr("socialcontrol.imports.csv_importer.MAX_ROWS", 2)
    body = "".join(f"C00{i},FB,iesl_page,main,text,en,,hi,,,#a,,\n" for i in range(1, 5))
    res = run(reg, body)
    assert len(res.rows) == 2 and (0, "E001") in codes(res, Level.ERROR)
