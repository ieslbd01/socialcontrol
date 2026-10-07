"""Command line entry points: ``python -m socialcontrol <command>``.

Commands: migrate, seed, serve, publisher, report <daily|weekly|monthly>, watchdog, backup,
demo (load a small sample into a LOCAL database for trying the dashboard).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine

from socialcontrol import jobs
from socialcontrol.config.settings import Settings, get_settings
from socialcontrol.database import migrate as migrate_mod
from socialcontrol.database.db import make_engine
from socialcontrol.database.seed import seed
from socialcontrol.notifications.router import EmailNotifier, Router, TelegramNotifier
from socialcontrol.platforms.registry import adapter_for


def build_router(s: Settings) -> Router:
    notifiers: dict[str, object] = {}
    if s.telegram_bot_token and s.telegram_chat_id:
        notifiers["telegram"] = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    if s.smtp_host and s.notify_email_to:
        notifiers["email"] = EmailNotifier(
            s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_pass, s.notify_email_to
        )
    return Router(notifiers)  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(UTC)


def cmd_demo(engine: Engine, s: Settings) -> None:
    """Sample data for a first look (local databases only)."""
    from sqlalchemy import text

    if s.sc_env not in ("local", "test"):
        raise SystemExit("demo data is only for local use")
    with engine.begin() as c:
        if c.execute(text("select count(*) from platform_accounts")).scalar_one():
            print("Database already has accounts; demo skipped.")
            return
        acc = c.execute(
            text(
                """insert into platform_accounts (platform_key, short_name, display_name, mode, state, destination_url)
               values ('whatsapp_channel','iesl_channel','IESL WhatsApp Channel','ASSISTED','CONNECTED',
                       'https://whatsapp.com/channel/example') returning id"""
            )
        ).scalar_one()
        c.execute(
            text(
                """insert into queues (account_id, name, start_at, recurrence, is_default, pattern)
               values (:a,'Main', now() + interval '1 day',
                       cast('{"type": "interval_days", "every": 3, "time_local": "10:00"}' as jsonb), true,
                       cast('["text", "text_image"]' as jsonb))"""
            ),
            {"a": acc},
        )
    print("Demo account and queue created. Import a CSV from the dashboard to continue.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="socialcontrol")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed")
    sub.add_parser("publisher")
    sub.add_parser("watchdog")
    sub.add_parser("backup")
    sub.add_parser("demo")
    rep = sub.add_parser("report")
    rep.add_argument("kind", choices=["daily", "weekly", "monthly"])
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    s = get_settings()
    if args.cmd == "migrate":
        migrate_mod.upgrade()
        print("migrations applied")
        return 0
    engine = make_engine()
    now = _now()
    if args.cmd == "seed":
        print(seed(engine))
    elif args.cmd == "demo":
        cmd_demo(engine, s)
    elif args.cmd == "serve":
        import uvicorn

        from socialcontrol.dashboard.app import create_app

        uvicorn.run(create_app(engine, s), host=args.host, port=args.port)
    elif args.cmd == "publisher":
        summary = jobs.run_publisher_cycle(
            engine, adapter_for, build_router(s), now, s.sc_signing_key, s.sc_base_url
        )
        print(
            f"run {summary.run_id}: published={len(summary.published)} "
            f"delivered={len(summary.delivered)} failed={len(summary.failed)} "
            f"retry={len(summary.retry_scheduled)} overdue={len(summary.overdue)}"
        )
    elif args.cmd == "report":
        r = jobs.run_report(engine, build_router(s), args.kind, now)
        print(f"{args.kind} report: {r.summary}")
    elif args.cmd == "watchdog":
        problems = jobs.watchdog(engine, build_router(s), now)
        print("problems:", problems or "none")
        return 1 if "HEARTBEAT_MISSED" in problems else 0
    elif args.cmd == "backup":
        from socialcontrol.backup import run_backup

        path = run_backup(s.database_url, s.sc_backup_key, Path("backups"), now)
        print(f"backup written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
