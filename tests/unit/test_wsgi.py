import pytest

from socialcontrol.config.settings import Settings
from socialcontrol.dashboard import wsgi

GOOD = dict(
    _env_file=None,
    sc_env="prod",
    sc_signing_key="k" * 40,
    sc_admin_email="owner@ieslbd.com",
    sc_admin_password_hash="scrypt$1$1$1$AA==$AA==",
    database_url="postgresql+psycopg://u:p@db.example.com:5432/postgres",
)


def patch(monkeypatch, **overrides):
    s = Settings(**{**GOOD, **overrides})
    monkeypatch.setattr(wsgi, "get_settings", lambda: s)
    monkeypatch.setattr(wsgi, "make_engine", lambda: object())


def test_production_refuses_to_start_without_admin_login(monkeypatch):
    patch(monkeypatch, sc_admin_email="", sc_admin_password_hash="")
    with pytest.raises(RuntimeError, match="SC_ADMIN_EMAIL"):
        wsgi.build()


def test_production_refuses_a_local_database(monkeypatch):
    patch(monkeypatch, database_url="postgresql+psycopg://u:p@127.0.0.1:5433/x")
    with pytest.raises(RuntimeError, match="local database"):
        wsgi.build()


def test_production_refuses_a_weak_signing_key(monkeypatch):
    patch(monkeypatch, sc_signing_key="short")
    with pytest.raises(RuntimeError):
        wsgi.build()


def test_valid_production_settings_build_the_app(monkeypatch):
    patch(monkeypatch)
    app = wsgi.build()
    assert app.title == "SocialControl"
