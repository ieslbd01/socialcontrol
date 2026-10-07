import importlib.util
from pathlib import Path

import pytest

from socialcontrol.dashboard import auth

SPEC = importlib.util.spec_from_file_location(
    "setup_admin", Path(__file__).resolve().parents[2] / "scripts" / "setup_admin.py"
)
setup_admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_admin)

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
NOW = 1_700_000_000.0


def passwords(*values):
    it = iter(values)
    return lambda prompt: next(it)


def good_code():
    return auth.totp_code(SECRET, NOW)


def make_env(tmp_path, body="SC_ENV=prod\nDATABASE_URL=postgresql://x\nSC_ADMIN_EMAIL=\n"):
    p = tmp_path / ".env.prod"
    p.write_text(body, encoding="utf-8")
    return p


def run(
    env, email="Owner@ieslbd.com", pw=("long enough pass", "long enough pass"), code=None, **kw
):
    out = []
    setup_admin.run(
        env,
        lambda: email,
        passwords(*pw),
        code or good_code,
        show=out.append,
        secret=SECRET,
        now=lambda: NOW,
        **kw,
    )
    return "\n".join(out)


def test_writes_all_three_settings_and_keeps_other_lines(tmp_path):
    env = make_env(tmp_path)
    shown = run(env)
    text = env.read_text(encoding="utf-8")
    assert "SC_ENV=prod" in text and "DATABASE_URL=postgresql://x" in text
    assert "SC_ADMIN_EMAIL=Owner@ieslbd.com" in text and "SC_TOTP_SECRET=" + SECRET in text
    line = next(x for x in text.splitlines() if x.startswith("SC_ADMIN_PASSWORD_HASH="))
    assert auth.verify_password("long enough pass", line.split("=", 1)[1])
    assert "long enough pass" not in text and "long enough pass" not in shown
    assert text.count("SC_ADMIN_EMAIL=") == 1  # replaced, not duplicated
    assert "G EZD" not in shown and "GEZD GNBV" in shown  # key shown in readable groups of four


def test_appends_when_keys_are_missing(tmp_path):
    env = make_env(tmp_path, "SC_ENV=prod\n")
    run(env)
    assert "SC_TOTP_SECRET=" + SECRET in env.read_text(encoding="utf-8")


def test_nothing_is_saved_when_the_code_never_matches(tmp_path):
    env = make_env(tmp_path)
    before = env.read_text(encoding="utf-8")
    with pytest.raises(setup_admin.SetupError, match="never matched"):
        run(env, code=lambda: "000000", attempts=3)
    assert env.read_text(encoding="utf-8") == before


def test_a_wrong_code_then_a_right_one_succeeds(tmp_path):
    env = make_env(tmp_path)
    codes = iter(["111111", good_code()])
    shown = run(env, code=lambda: next(codes))
    assert "not correct" in shown and "SC_TOTP_SECRET=" in env.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (dict(email="not-an-email"), "email"),
        (dict(pw=("short", "short")), "at least 12"),
        (dict(pw=("long enough pass", "different one!!")), "different"),
    ],
)
def test_bad_input_saves_nothing(tmp_path, kwargs, message):
    env = make_env(tmp_path)
    before = env.read_text(encoding="utf-8")
    with pytest.raises(setup_admin.SetupError, match=message):
        run(env, **kwargs)
    assert env.read_text(encoding="utf-8") == before
