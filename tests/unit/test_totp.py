import pytest

from socialcontrol.dashboard import auth

# RFC 6238 appendix B secret ("12345678901234567890") in base32, SHA-1, 6 digits
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_matches_rfc_6238_vectors(t, expected):
    assert auth.totp_code(RFC_SECRET, t) == expected


def test_verifier_accepts_window_and_blocks_replay():
    v = auth.TotpVerifier(RFC_SECRET)
    now = 1_700_000_000
    code = auth.totp_code(RFC_SECRET, now)
    assert v.verify(code, now)
    assert not v.verify(code, now)  # same code twice = replay
    nxt = auth.totp_code(RFC_SECRET, now, 1)
    assert v.verify(nxt, now)  # one step of clock drift is tolerated
    assert not v.verify(auth.totp_code(RFC_SECRET, now, 5), now)  # far-off code is refused


def test_verifier_rejects_malformed_codes():
    v = auth.TotpVerifier(RFC_SECRET)
    for bad in ("", "12345", "1234567", "abcdef", "12 34"):
        assert not v.verify(bad, 1_700_000_000)


def test_spaces_in_code_are_ignored_and_secret_generation():
    v = auth.TotpVerifier(RFC_SECRET)
    c = auth.totp_code(RFC_SECRET, 1_700_000_000)
    assert v.verify(c[:3] + " " + c[3:], 1_700_000_000)
    s1, s2 = auth.generate_totp_secret(), auth.generate_totp_secret()
    assert s1 != s2 and len(s1) == 32 and auth.totp_code(s1).isdigit()


def test_provisioning_uri():
    uri = auth.provisioning_uri("ABC", "owner@ieslbd.com")
    assert uri.startswith("otpauth://totp/SocialControl:owner%40ieslbd.com?secret=ABC")


def test_empty_environment_values_are_treated_as_unset(monkeypatch):
    """Regression: GitHub passes unset secrets as '' and an empty SMTP_PORT crashed every job."""
    from socialcontrol.config.settings import Settings

    for name in ("SMTP_PORT", "SMTP_HOST", "TELEGRAM_BOT_TOKEN", "SC_TOTP_SECRET"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    s = Settings(_env_file=None)
    assert s.smtp_port == 587 and s.smtp_host == "" and s.sc_totp_secret == ""
    assert s.database_url.endswith("@h:5432/d")  # real values are still read
