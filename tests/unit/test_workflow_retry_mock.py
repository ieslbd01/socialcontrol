from datetime import UTC, datetime, timedelta

import pytest

from socialcontrol.domain.enums import FailureClass, PostStatus
from socialcontrol.domain.workflow import (
    TransitionError,
    approval_hash,
    can_transition,
    check_transition,
    is_publishable,
)
from socialcontrol.platforms.adapters.mock import MockAdapter
from socialcontrol.platforms.base import (
    AdapterError,
    MediaView,
    PostView,
    PublishContext,
)
from socialcontrol.retry.policy import RetryAction, decide

NOW = datetime(2026, 10, 1, tzinfo=UTC)


# ---------------------------------------------------------------- workflow
def test_every_status_has_a_transition_entry():
    from socialcontrol.domain.workflow import ALLOWED

    assert set(ALLOWED) == set(PostStatus)


def test_happy_path_is_allowed():
    path = [
        PostStatus.DRAFT,
        PostStatus.IN_REVIEW,
        PostStatus.APPROVED,
        PostStatus.SCHEDULED,
        PostStatus.PUBLISHING,
        PostStatus.PUBLISHED,
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert can_transition(a, b)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (PostStatus.DRAFT, PostStatus.SCHEDULED),
        (PostStatus.DRAFT, PostStatus.PUBLISHING),
        (PostStatus.IN_REVIEW, PostStatus.PUBLISHED),
        (PostStatus.PUBLISHED, PostStatus.DRAFT),
        (PostStatus.PUBLISHED, PostStatus.SCHEDULED),
        (PostStatus.CANCELLED, PostStatus.PUBLISHING),
    ],
)
def test_illegal_transitions(old, new):
    assert not can_transition(old, new)
    with pytest.raises(TransitionError):
        check_transition(old, new)


def test_unapproved_never_publishable_by_default():
    for s in (PostStatus.DRAFT, PostStatus.IN_REVIEW, PostStatus.APPROVED):
        assert not is_publishable(s)
    assert is_publishable(PostStatus.SCHEDULED)
    assert is_publishable(PostStatus.RETRYING)


def test_only_scheduled_reaches_publishing_from_a_normal_path():
    sources = [s for s in PostStatus if can_transition(s, PostStatus.PUBLISHING)]
    assert set(sources) == {PostStatus.SCHEDULED, PostStatus.RETRYING, PostStatus.OVERDUE}


def test_approval_hash_stable_and_sensitive():
    base = {
        "post_type": "text",
        "language": "bn",
        "caption": "তাপমাত্রা ক্যালিব্রেশন 🌡️",
        "hashtags": ["#a"],
    }
    h1 = approval_hash(base)
    assert h1 == approval_hash(dict(base))
    assert h1 == approval_hash({**base, "hashtags": ("#a",)})
    assert h1 != approval_hash({**base, "caption": base["caption"] + " "})
    assert h1 != approval_hash({**base, "media_sha256": ["x"]})


# ---------------------------------------------------------------- retry
def test_temporary_retries_then_final():
    delays = []
    for done in range(3):
        d = decide(FailureClass.TEMPORARY, done, NOW)
        assert d.action == RetryAction.RETRY
        delays.append(d.next_retry_at - NOW)
    assert delays == [timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6)]
    final = decide(FailureClass.TEMPORARY, 3, NOW)
    assert final.action == RetryAction.FINAL and final.notify_critical


def test_unknown_gets_fewer_retries():
    assert decide(FailureClass.UNKNOWN, 1, NOW).action == RetryAction.RETRY
    assert decide(FailureClass.UNKNOWN, 2, NOW).action == RetryAction.FINAL


def test_auth_pauses_account_no_retry():
    d = decide(FailureClass.AUTH, 0, NOW)
    assert d.action == RetryAction.PAUSE_ACCOUNT and d.next_retry_at is None and d.notify_critical


@pytest.mark.parametrize("fc", [FailureClass.VALIDATION, FailureClass.PERMANENT_REJECTION])
def test_non_retryable_goes_final(fc):
    d = decide(fc, 0, NOW)
    assert d.action == RetryAction.FINAL and d.next_retry_at is None


def test_rate_limit_waits_for_reset_and_is_bounded():
    d = decide(FailureClass.RATE_LIMIT, 0, NOW, retry_after=timedelta(hours=5))
    assert d.action == RetryAction.RETRY and d.next_retry_at == NOW + timedelta(hours=5)
    assert decide(FailureClass.RATE_LIMIT, 3, NOW).action == RetryAction.FINAL


# ---------------------------------------------------------------- mock adapter / contract
def post(**kw):
    base = dict(
        post_id="C001-FB",
        post_type="text_image",
        language="en",
        caption="Hello {link}",
        link_url="https://ieslbd.com/x",
        hashtags=("#cal",),
        media=(MediaView("https://m/1.jpg", "image/jpeg"),),
    )
    base.update(kw)
    return PostView(**base)


def ctx(key="k1", **kw):
    return PublishContext(idempotency_key=key, **kw)


def test_validate_post_is_pure_and_flags_issues():
    a = MockAdapter({"caption_limit": 20})
    assert a.validate_post(post(caption="short")) == []
    codes = {i.code for i in a.validate_post(post(caption="x" * 50, media=()))}
    assert {"E032", "E040"} <= codes
    assert a.validate_post(post(post_type="carousel"))[0].code == "E030"
    assert a.calls == 0


def test_publish_success_and_idempotency():
    a = MockAdapter()
    r1 = a.publish(post(), ctx("k1"))
    r2 = a.publish(post(), ctx("k1"))
    assert r1 == r2 and len(a.published) == 1
    assert a.find_existing(post(), ctx("k1")).platform_post_id == r1.platform_post_id
    assert a.find_existing(post(), ctx("other")) is None


def test_dry_run_publishes_nothing():
    a = MockAdapter()
    a.publish(post(), ctx(dry_run=True))
    assert a.published == {}


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("temporary", FailureClass.TEMPORARY),
        ("rate_limit", FailureClass.RATE_LIMIT),
        ("auth", FailureClass.AUTH),
        ("validation", FailureClass.VALIDATION),
        ("permanent", FailureClass.PERMANENT_REJECTION),
        ("unknown", FailureClass.UNKNOWN),
    ],
)
def test_failure_classes_map(outcome, expected):
    a = MockAdapter({"outcome": outcome})
    with pytest.raises(AdapterError) as ei:
        a.publish(post(), ctx())
    assert a.map_error(ei.value) == expected


def test_fail_n_times_then_succeeds():
    a = MockAdapter({"outcome": "temporary", "fail_n_times": 2})
    for _ in range(2):
        with pytest.raises(AdapterError):
            a.publish(post(), ctx("k"))
    assert a.publish(post(), ctx("k")).platform_post_id.startswith("mock-")


def test_assisted_only_channel_never_publishes():
    a = MockAdapter({"supports_auto": False})
    with pytest.raises(AdapterError):
        a.publish(post(), ctx())
    pkg = a.build_assisted_package(post())
    assert "https://ieslbd.com/x" in pkg.text and "#cal" in pkg.text
    assert pkg.char_count == len(pkg.text)


def test_bangla_and_emoji_round_trip_in_package():
    text = "তাপমাত্রা ক্যালিব্রেশন কেন জরুরি? 🌡️"
    pkg = MockAdapter().build_assisted_package(post(caption=text, hashtags=(), link_url=None))
    assert pkg.text == text
