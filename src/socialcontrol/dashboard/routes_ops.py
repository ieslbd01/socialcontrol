"""Dashboard routes: queues, calendar, assisted, failed posts, logs, reports, platforms, settings."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import text

from socialcontrol.assisted import service as assisted
from socialcontrol.dashboard.app import DHAKA, Ctx, flash, require_csrf, require_login
from socialcontrol.reports import builder
from socialcontrol.review import service as review
from socialcontrol.scheduler import queue_service
from socialcontrol.scheduler.recurrence import RecurrenceError, parse_rule

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def build_recurrence(
    kind: str, every: int, weekdays: list[str], day: int, nth: int, weekday: str, time_local: str
) -> dict[str, Any]:
    t = time_local or "10:00"
    if kind == "interval_days":
        rule: dict[str, Any] = {"type": "interval_days", "every": every, "time_local": t}
    elif kind == "weekly":
        rule = {"type": "weekly", "every_weeks": every, "weekdays": weekdays, "time_local": t}
    elif kind == "monthly_day":
        rule = {
            "type": "monthly_day",
            "every_months": every,
            "day": day,
            "clamp": "last_day",
            "time_local": t,
        }
    elif kind == "monthly_nth":
        rule = {
            "type": "monthly_nth",
            "every_months": every,
            "nth": nth,
            "weekday": weekday,
            "time_local": t,
        }
    else:
        rule = {"type": "once", "time_local": t}
    parse_rule(rule)  # raises RecurrenceError when invalid
    return rule


def register(app: FastAPI) -> None:
    ctx: Ctx = app.state.ctx
    render: Callable[..., Response] = app.state.render
    auth = [Depends(require_login)]
    post_auth = [Depends(require_login), Depends(require_csrf)]

    # ------------------------------------------------------------ platforms / accounts
    @app.get("/platforms", dependencies=auth)
    def platforms_page(request: Request) -> Response:
        with ctx.engine.connect() as c:
            platforms = [
                dict(r)
                for r in c.execute(
                    text(
                        """select p.key, p.display_name, p.default_mode,
                          (select count(*) from platform_capabilities pc where pc.platform_key=p.key) as caps
                   from platforms p order by p.key"""
                    )
                ).mappings()
            ]
            accounts = [
                dict(r)
                for r in c.execute(
                    text(
                        """select a.id::text as id, a.platform_key, a.short_name, a.display_name, a.mode, a.state,
                          a.test_mode, a.destination_url, a.last_publish_at, a.last_error,
                          (select count(*) from queues q where q.account_id=a.id and q.status='ACTIVE') as queues
                   from platform_accounts a order by a.platform_key, a.short_name"""
                    )
                ).mappings()
            ]
            caps: dict[str, list[str]] = {}
            for r in c.execute(
                text("select platform_key, post_type from platform_capabilities order by 1,2")
            ):
                caps.setdefault(r[0], []).append(r[1])
        return render(request, "platforms.html", platforms=platforms, accounts=accounts, caps=caps)

    @app.post("/accounts", dependencies=post_auth)
    def account_create(
        request: Request,
        platform_key: str = Form(...),
        short_name: str = Form(...),
        display_name: str = Form(""),
        destination_url: str = Form(""),
    ) -> Response:
        short = short_name.strip().lower().replace(" ", "_")
        if not short:
            flash(request, "Give the account a short name.", "danger")
            return RedirectResponse("/platforms", status_code=303)
        try:
            with ctx.engine.begin() as c:
                mode = c.execute(
                    text("select default_mode from platforms where key=:k"), {"k": platform_key}
                ).scalar_one()
                c.execute(
                    text(
                        """insert into platform_accounts (platform_key, short_name, display_name, mode,
                           state, destination_url) values (:p,:s,:d,:m,'NOT_CONFIGURED',:u)"""
                    ),
                    {
                        "p": platform_key,
                        "s": short,
                        "d": display_name or short,
                        "m": mode,
                        "u": destination_url or None,
                    },
                )
            flash(request, f"Account {short} added.")
        except Exception:
            flash(request, "Could not add the account (does it already exist?).", "danger")
        return RedirectResponse("/platforms", status_code=303)

    @app.post("/accounts/{account_id}/mode", dependencies=post_auth)
    def account_mode(request: Request, account_id: str, mode: str = Form(...)) -> Response:
        if mode not in ("AUTO", "ASSISTED"):
            flash(request, "Invalid mode.", "danger")
        else:
            with ctx.engine.begin() as c:
                c.execute(
                    text("update platform_accounts set mode=:m where id=cast(:i as uuid)"),
                    {"m": mode, "i": account_id},
                )
            flash(request, f"Mode set to {mode}. Only posts not yet delivered are affected.")
        return RedirectResponse("/platforms", status_code=303)

    @app.post("/accounts/{account_id}/toggle", dependencies=post_auth)
    def account_toggle(request: Request, account_id: str) -> Response:
        with ctx.engine.begin() as c:
            c.execute(
                text(
                    """update platform_accounts set state = case when state='DISABLED' then 'NOT_CONFIGURED'
                          else 'DISABLED' end where id=cast(:i as uuid)"""
                ),
                {"i": account_id},
            )
        flash(request, "Account updated.")
        return RedirectResponse("/platforms", status_code=303)

    # ------------------------------------------------------------ queues
    @app.get("/queues", dependencies=auth)
    def queues_page(request: Request) -> Response:
        now = ctx.now()
        with ctx.engine.connect() as c:
            rows = []
            for r in c.execute(
                text(
                    """select q.id, q.name, q.status, q.timezone, q.recurrence, q.pattern, q.pattern_mode,
                          q.require_approval, q.evergreen_enabled, q.runway_threshold_days,
                          a.platform_key, a.short_name, a.mode,
                          (select count(*) from posts p where p.queue_id=q.id and p.status in ('APPROVED','SCHEDULED')) as pending
                   from queues q join platform_accounts a on a.id=q.account_id order by a.platform_key, q.name"""
                )
            ).mappings():
                days = queue_service.runway_days(c, r["id"], now)
                rows.append(
                    {
                        **dict(r),
                        "id": str(r["id"]),
                        "runway": round(days, 1),
                        "low": days < r["runway_threshold_days"],
                        "recurrence_json": json.dumps(r["recurrence"]),
                    }
                )
            accounts = [
                dict(r)
                for r in c.execute(
                    text(
                        "select id::text as id, platform_key, short_name from platform_accounts "
                        "where state <> 'DISABLED' order by 2,3"
                    )
                ).mappings()
            ]
        return render(request, "queues.html", queues=rows, accounts=accounts, weekdays=WEEKDAYS)

    @app.post("/queues", dependencies=post_auth)
    async def queue_create(request: Request) -> Response:
        f = await request.form()
        try:
            rule = build_recurrence(
                str(f.get("kind") or "interval_days"),
                int(str(f.get("every") or 1)),
                [str(x) for x in f.getlist("weekdays")],
                int(str(f.get("day") or 1)),
                int(str(f.get("nth") or 1)),
                str(f.get("weekday") or "MON"),
                str(f.get("time_local") or "10:00"),
            )
            from zoneinfo import ZoneInfo

            tz = str(f.get("timezone") or "Asia/Dhaka")
            start_local = datetime.fromisoformat(f"{f.get('start_date')}T{rule['time_local']}")
            start_utc = start_local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
            pattern = [
                p.strip()
                for p in str(f.get("pattern") or "").replace("→", ",").split(",")
                if p.strip()
            ]
            with ctx.engine.begin() as c:
                has_default = c.execute(
                    text("select 1 from queues where account_id=cast(:a as uuid) and is_default"),
                    {"a": f.get("account_id")},
                ).first()
                qid = c.execute(
                    text(
                        """insert into queues (account_id, name, start_at, timezone, recurrence, pattern, pattern_mode,
                           is_default, require_approval, evergreen_enabled)
                       values (cast(:a as uuid), :n, :s, :tz, cast(:r as jsonb), cast(:p as jsonb), :pm,
                               :d, :ra, :ev) returning id"""
                    ),
                    {
                        "a": f.get("account_id"),
                        "n": str(f.get("name") or "Main").strip(),
                        "s": start_utc,
                        "tz": tz,
                        "r": json.dumps(rule),
                        "p": json.dumps(pattern),
                        "pm": str(f.get("pattern_mode") or "RELAXED"),
                        "d": not has_default,
                        "ra": f.get("require_approval") == "on",
                        "ev": f.get("evergreen") == "on",
                    },
                ).scalar_one()
                queue_service.ensure_horizon(c, qid, ctx.now())
            flash(request, "Queue created. Slots were generated for the next 90 days.")
        except (RecurrenceError, ValueError, TypeError) as exc:
            flash(request, f"Could not create the queue: {exc}", "danger")
        except Exception:
            flash(
                request,
                "Could not create the queue (name already used for this account?).",
                "danger",
            )
        return RedirectResponse("/queues", status_code=303)

    @app.post("/queues/{queue_id}/status", dependencies=post_auth)
    def queue_status(request: Request, queue_id: str, status: str = Form(...)) -> Response:
        if status not in ("ACTIVE", "PAUSED", "DISABLED"):
            flash(request, "Invalid status.", "danger")
        else:
            with ctx.engine.begin() as c:
                c.execute(
                    text("update queues set status=:s where id=cast(:i as uuid)"),
                    {"s": status, "i": queue_id},
                )
            flash(request, f"Queue is now {status}. Nothing was deleted.")
        return RedirectResponse("/queues", status_code=303)

    @app.post("/queues/{queue_id}/simulate", dependencies=post_auth)
    def queue_simulate(request: Request, queue_id: str) -> Response:
        """Show the next slots and which approved posts would fill them (no changes)."""
        with ctx.engine.connect() as c:
            slots = [
                dict(r)
                for r in c.execute(
                    text(
                        """select s.slot_at, s.state, s.post_id from queue_slots s
                   where s.queue_id=cast(:q as uuid) and s.slot_at >= :n order by s.slot_at limit 12"""
                    ),
                    {"q": queue_id, "n": ctx.now()},
                ).mappings()
            ]
        return render(request, "simulate.html", slots=slots, queue_id=queue_id)

    # ------------------------------------------------------------ calendar
    @app.get("/calendar", dependencies=auth)
    def calendar_page(
        request: Request, days: int = 30, platform: str = "", status: str = ""
    ) -> Response:
        days = max(1, min(days, 120))
        now = ctx.now()
        where: list[str] = ["s.slot_at >= :a", "s.slot_at < :b"]
        params: dict[str, Any] = {"a": now - timedelta(days=1), "b": now + timedelta(days=days)}
        if platform:
            where.append("a.platform_key = :pl")
            params["pl"] = platform
        if status:
            where.append("coalesce(p.status, s.state) = :st")
            params["st"] = status
        with ctx.engine.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    text(
                        f"""select s.slot_at, s.state as slot_state, p.post_id, p.status, p.post_type,
                           left(coalesce(p.caption,p.title,''),60) as snippet, a.platform_key, a.mode, q.name as queue
                    from queue_slots s join queues q on q.id=s.queue_id
                    join platform_accounts a on a.id=q.account_id
                    left join posts p on p.post_id=s.post_id
                    where {" and ".join(where)}
                    order by s.slot_at"""
                    ),
                    params,
                ).mappings()
            ]
            platforms = [r[0] for r in c.execute(text("select key from platforms order by 1"))]
        by_day: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_day.setdefault(r["slot_at"].astimezone(DHAKA).strftime("%a %d %b %Y"), []).append(r)
        return render(
            request,
            "calendar.html",
            by_day=by_day,
            platforms=platforms,
            f={"days": days, "platform": platform, "status": status},
        )

    # ------------------------------------------------------------ assisted tasks (logged in)
    @app.get("/assisted", dependencies=auth)
    def assisted_page(request: Request) -> Response:
        with ctx.engine.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    text(
                        """select t.id::text as id, t.state, t.delivered_at, t.reminders_sent, p.post_id, p.caption, p.title,
                          p.link_url, p.hashtags, a.platform_key, a.display_name, a.destination_url
                   from assisted_tasks t join posts p on p.post_id=t.post_id
                   join platform_accounts a on a.id=p.account_id
                   where t.state in ('DELIVERED','OVERDUE') order by t.delivered_at"""
                    )
                ).mappings()
            ]
        return render(request, "assisted.html", tasks=rows)

    def _task_token(task_id: str) -> str:
        with ctx.engine.begin() as c:
            return assisted.issue_token(c, task_id, ctx.settings.sc_signing_key, ctx.now())

    @app.post("/assisted/{task_id}/done", dependencies=post_auth)
    def assisted_done(request: Request, task_id: str, published_url: str = Form("")) -> Response:
        try:
            tok = _task_token(task_id)
            with ctx.engine.begin() as c:
                assisted.confirm(
                    c, tok, ctx.settings.sc_signing_key, ctx.now(), published_url or None
                )
            flash(request, "Marked as published.")
        except assisted.TokenError as exc:
            flash(request, str(exc), "danger")
        return RedirectResponse("/assisted", status_code=303)

    @app.post("/assisted/{task_id}/skip", dependencies=post_auth)
    def assisted_skip(request: Request, task_id: str) -> Response:
        try:
            tok = _task_token(task_id)
            with ctx.engine.begin() as c:
                assisted.skip(c, tok, ctx.settings.sc_signing_key, ctx.now())
            flash(request, "Skipped.")
        except assisted.TokenError as exc:
            flash(request, str(exc), "danger")
        return RedirectResponse("/assisted", status_code=303)

    # ------------------------------------------------------------ public signed link (no login)
    @app.get("/a/{token}")
    def public_assisted(request: Request, token: str) -> Response:
        try:
            with ctx.engine.begin() as c:
                data = assisted.load_package_data(c, token, ctx.settings.sc_signing_key, ctx.now())
        except assisted.TokenError as exc:
            return render(request, "error.html", status=410, message=str(exc))
        return render(request, "assisted_public.html", d=data, token=token)

    @app.post("/a/{token}/done", dependencies=[Depends(require_csrf)])
    def public_done(request: Request, token: str, published_url: str = Form("")) -> Response:
        try:
            with ctx.engine.begin() as c:
                assisted.confirm(
                    c, token, ctx.settings.sc_signing_key, ctx.now(), published_url or None
                )
        except assisted.TokenError as exc:
            return render(request, "error.html", status=410, message=str(exc))
        return render(request, "error.html", message="Thank you. Marked as published.")

    @app.post("/a/{token}/skip", dependencies=[Depends(require_csrf)])
    def public_skip(request: Request, token: str) -> Response:
        try:
            with ctx.engine.begin() as c:
                assisted.skip(c, token, ctx.settings.sc_signing_key, ctx.now())
        except assisted.TokenError as exc:
            return render(request, "error.html", status=410, message=str(exc))
        return render(request, "error.html", message="Skipped.")

    # ------------------------------------------------------------ failed posts
    @app.get("/failed", dependencies=auth)
    def failed_page(request: Request) -> Response:
        with ctx.engine.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    text(
                        """select p.post_id, p.status, p.attempt_count, p.next_retry_at, a.platform_key,
                          (select error_message from publish_attempts x where x.post_id=p.post_id
                           and x.result='FAILED' order by started_at desc limit 1) as reason,
                          (select failure_class from publish_attempts x where x.post_id=p.post_id
                           and x.result='FAILED' order by started_at desc limit 1) as fclass
                   from posts p join platform_accounts a on a.id=p.account_id
                   where p.status in ('FAILED_FINAL','NEEDS_ATTENTION','OVERDUE','RETRYING','FAILED')
                   order by p.updated_at desc"""
                    )
                ).mappings()
            ]
        return render(request, "failed.html", rows=rows)

    # ------------------------------------------------------------ logs
    @app.get("/logs", dependencies=auth)
    def logs_page(
        request: Request, platform: str = "", result: str = "", q: str = "", fmt: str = ""
    ) -> Response:
        where, params = ["1=1"], {}
        if platform:
            where.append("platform_key=:pl")
            params["pl"] = platform
        if result:
            where.append("result=:r")
            params["r"] = result
        if q:
            where.append("(post_id ilike :q or error_message ilike :q)")
            params["q"] = f"%{q}%"
        with ctx.engine.connect() as c:
            rows = [
                dict(r)
                for r in c.execute(
                    text(
                        f"""select started_at, post_id, platform_key, attempt_type, result, failure_class,
                           error_code, error_message, published_url
                    from publish_attempts where {" and ".join(where)} order by started_at desc limit 500"""
                    ),
                    params,
                ).mappings()
            ]
        if fmt == "csv":
            out = io.StringIO()
            w = csv.writer(out, lineterminator="\n")
            cols = [
                "started_at",
                "post_id",
                "platform_key",
                "attempt_type",
                "result",
                "failure_class",
                "error_code",
                "error_message",
                "published_url",
            ]
            w.writerow(cols)
            for r in rows:
                w.writerow([builder._csv_safe(r[k]) for k in cols])
            return Response(
                out.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="logs.csv"'},
            )
        return render(
            request, "logs.html", rows=rows, f={"platform": platform, "result": result, "q": q}
        )

    # ------------------------------------------------------------ reports
    @app.get("/reports", dependencies=auth)
    def reports_page(request: Request, kind: str = "daily") -> Response:
        if kind not in ("daily", "weekly", "monthly"):
            kind = "daily"
        start, end = builder.period_for(kind, ctx.now())
        with ctx.engine.connect() as c:
            rep = builder.build_report(c, kind, start, end)
        return render(
            request, "reports.html", rep=rep, kind=kind, text_summary=builder.to_text_summary(rep)
        )

    @app.get("/reports/download", dependencies=auth)
    def report_download(kind: str = "daily", fmt: str = "csv") -> Response:
        start, end = builder.period_for(
            kind if kind in ("daily", "weekly", "monthly") else "daily", ctx.now()
        )
        with ctx.engine.connect() as c:
            rep = builder.build_report(c, kind, start, end)
        if fmt == "json":
            return Response(
                builder.to_json(rep),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{kind}.json"'},
            )
        if fmt == "xlsx":
            return Response(
                builder.to_xlsx(rep),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="{kind}.xlsx"'},
            )
        return Response(
            builder.to_csv(rep),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
        )

    # ------------------------------------------------------------ settings / kill switch
    @app.get("/settings", dependencies=auth)
    def settings_page(request: Request) -> Response:
        with ctx.engine.connect() as c:
            kill = bool(
                c.execute(text("select value from settings where key='kill_switch'")).scalar()
            )
            events = [
                dict(r)
                for r in c.execute(
                    text("select at, kind, detail from security_events order by id desc limit 15")
                ).mappings()
            ]
        return render(request, "settings.html", kill=kill, events=events)

    @app.post("/settings/kill-switch", dependencies=post_auth)
    def kill_switch(request: Request, active: str = Form("")) -> Response:
        on = active == "on"
        with ctx.engine.begin() as c:
            review.set_kill_switch(c, on, actor=request.session.get("user", "owner"))
        flash(
            request,
            "Kill switch ON: publishing stopped." if on else "Kill switch OFF: publishing resumed.",
            "danger" if on else "success",
        )
        return RedirectResponse("/settings", status_code=303)
