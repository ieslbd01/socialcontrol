"""Notification router: routing table, dedup, quiet hours, fallback (PRD-09)."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Connection, text


class Notifier(Protocol):
    name: str

    def send(self, subject: str, body: str) -> bool: ...


# ---------------------------------------------------------------- channels
class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=15)

    def send(self, subject: str, body: str) -> bool:
        if not (self.token and self.chat_id):
            return False
        try:
            r = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": f"{subject}\n\n{body}"[:4000],
                    "disable_web_page_preview": True,
                },
            )
            return r.status_code == 200
        except httpx.HTTPError:
            return False


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        to: str,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self.host, self.port, self.user, self.password, self.to = host, port, user, password, to
        self.smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, subject: str, body: str) -> bool:
        if not (self.host and self.to):
            return False
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.user or self.to, self.to
        msg.set_content(body)
        try:
            with self.smtp_factory(self.host, self.port, timeout=20) as smtp:
                smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException):
            return False


# ---------------------------------------------------------------- rules
@dataclass(frozen=True)
class Rule:
    channels: tuple[str, ...]
    severity: str  # info | warning | action | critical
    dedup_hours: float = 6.0
    digest: bool = False  # held for the daily summary instead of sent now


DEFAULT_RULES: dict[str, Rule] = {
    "PUBLISH_SUCCESS": Rule(("telegram",), "info", digest=True),
    "PUBLISH_WARNING": Rule(("telegram",), "warning"),
    "FINAL_FAILURE": Rule(("telegram", "email"), "critical"),
    "TOKEN_EXPIRED": Rule(("telegram", "email"), "critical", dedup_hours=24),
    "TOKEN_EXPIRING": Rule(("telegram", "email"), "warning", dedup_hours=24),
    "ACCOUNT_ERROR": Rule(("telegram",), "critical"),
    "ASSISTED_DUE": Rule(("telegram", "email"), "action", dedup_hours=0),
    "ASSISTED_REMINDER": Rule(("telegram", "email"), "action", dedup_hours=0),
    "POST_OVERDUE": Rule(("telegram",), "warning"),
    "RUNWAY_LOW": Rule(("telegram",), "warning", dedup_hours=24),
    "EMPTY_SLOT": Rule(("telegram",), "warning", dedup_hours=24),
    "NEEDS_ATTENTION": Rule(("telegram",), "critical"),
    "IMPORT_DONE": Rule(("telegram",), "info", digest=True),
    "HEARTBEAT_MISSED": Rule(("telegram", "email"), "critical", dedup_hours=6),
    "STORAGE_HIGH": Rule(("telegram",), "warning", dedup_hours=24),
    "KILL_SWITCH": Rule(("telegram",), "info", dedup_hours=0),
    "DAILY_REPORT": Rule(("telegram",), "info", dedup_hours=20),
    "WEEKLY_REPORT": Rule(("email", "telegram"), "info", dedup_hours=120),
    "MONTHLY_REPORT": Rule(("email", "telegram"), "info", dedup_hours=500),
}

CRITICAL_BYPASS = {"critical", "action"}


def in_quiet_hours(now: datetime, tz: str = "Asia/Dhaka", start: int = 22, end: int = 7) -> bool:
    h = now.astimezone(ZoneInfo(tz)).hour
    return h >= start or h < end


@dataclass
class DispatchResult:
    sent: list[str]
    held: str | None = None  # "digest" | "quiet_hours" | "deduplicated" | "disabled"
    failed: list[str] | None = None


class Router:
    def __init__(
        self,
        notifiers: dict[str, Notifier],
        rules: dict[str, Rule] | None = None,
        quiet_hours: tuple[int, int] | None = (22, 7),
        tz: str = "Asia/Dhaka",
    ) -> None:
        self.notifiers = notifiers
        self.rules = rules or DEFAULT_RULES
        self.quiet = quiet_hours
        self.tz = tz

    def _log(
        self,
        conn: Connection,
        event: str,
        channel: str,
        key: str,
        ok: bool,
        detail: str,
        now: datetime,
    ) -> None:
        conn.execute(
            text(
                """insert into notification_logs (at, event, channel, dedup_key, ok, detail)
                   values (:at, :e, :c, :k, :ok, :d)"""
            ),
            {"at": now, "e": event, "c": channel, "k": key, "ok": ok, "d": detail[:500]},
        )

    def dispatch(
        self, conn: Connection, event: str, key: str, subject: str, body: str, now: datetime
    ) -> DispatchResult:
        rule = self.rules.get(event)
        if rule is None:
            return DispatchResult([], "disabled")
        dedup_key = f"{event}:{key}"

        if rule.dedup_hours > 0:
            recent = conn.execute(
                text(
                    """select 1 from notification_logs where dedup_key=:k and ok
                       and at > :since limit 1"""
                ),
                {"k": dedup_key, "since": now - timedelta(hours=rule.dedup_hours)},
            ).first()
            if recent:
                return DispatchResult([], "deduplicated")

        if rule.digest:
            self._log(conn, event, "digest", dedup_key, True, subject, now)
            return DispatchResult([], "digest")

        if (
            self.quiet
            and rule.severity not in CRITICAL_BYPASS
            and in_quiet_hours(now, self.tz, *self.quiet)
        ):
            self._log(conn, event, "held", dedup_key, False, "quiet hours", now)
            return DispatchResult([], "quiet_hours")

        sent: list[str] = []
        failed: list[str] = []
        for channel in rule.channels:
            n = self.notifiers.get(channel)
            ok = bool(n and n.send(subject, body))
            self._log(conn, event, channel, dedup_key, ok, subject, now)
            (sent if ok else failed).append(channel)
            if ok and rule.severity not in ("critical",):
                break  # one working channel is enough for non-critical events
        if not sent:
            # fallback: any other configured channel, then dashboard banner (logged)
            for name, n in self.notifiers.items():
                if name in rule.channels:
                    continue
                if n.send(subject, body):
                    sent.append(name)
                    self._log(conn, event, name, dedup_key, True, subject + " (fallback)", now)
                    break
            if not sent:
                self._log(conn, event, "banner", dedup_key, True, subject, now)
        return DispatchResult(sent, None, failed)


# ---------------------------------------------------------------- templates
def format_event(
    event: str, post_id: str = "", detail: str = "", link: str = ""
) -> tuple[str, str]:
    titles = {
        "FINAL_FAILURE": f"❌ Failed: {post_id}",
        "PUBLISH_SUCCESS": f"✅ Published: {post_id}",
        "TOKEN_EXPIRED": "🔑 Account needs reconnecting",
        "ASSISTED_DUE": f"📝 Post now: {post_id}",
        "ASSISTED_REMINDER": f"⏰ Reminder: {post_id}",
        "POST_OVERDUE": f"⚠️ Overdue: {post_id}",
        "RUNWAY_LOW": "⚠️ Queue is running low on content",
        "EMPTY_SLOT": "⚠️ A slot had no content",
        "NEEDS_ATTENTION": f"🔍 Check manually: {post_id}",
        "HEARTBEAT_MISSED": "🚨 Publisher has not run",
    }
    subject = titles.get(event, event)
    body = "\n".join(x for x in (detail, link) if x)
    return subject, body
