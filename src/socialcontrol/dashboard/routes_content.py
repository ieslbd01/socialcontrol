"""Dashboard routes: home, posts, review actions, imports (PRD-02/03/04/06)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import text

from socialcontrol.dashboard.app import Ctx, flash, require_csrf, require_login
from socialcontrol.imports import import_service
from socialcontrol.imports.csv_importer import Level, errors_csv
from socialcontrol.media.zipsafe import UnsafeZipError
from socialcontrol.review import service as review
from socialcontrol.scheduler import queue_service

PAGE_SIZE = 50
MAX_UPLOAD = 200 * 1024 * 1024
PENDING_TTL = 3600


def register(app: FastAPI) -> None:
    ctx: Ctx = app.state.ctx
    render: Callable[..., Response] = app.state.render
    auth = [Depends(require_login)]
    post_auth = [Depends(require_login), Depends(require_csrf)]

    # ------------------------------------------------------------ home
    @app.get("/", dependencies=auth)
    def home(request: Request) -> Response:
        now = ctx.now()
        with ctx.engine.connect() as c:
            tiles = dict(
                c.execute(
                    text(
                        """select
                      count(*) filter (where status in ('SCHEDULED','RETRYING')) as scheduled,
                      count(*) filter (where status in ('DRAFT','IN_REVIEW')) as review,
                      count(*) filter (where status in ('AWAITING_CONFIRMATION','OVERDUE')) as assisted,
                      count(*) filter (where status in ('FAILED_FINAL','NEEDS_ATTENTION')) as failed,
                      count(*) filter (where status='RETRYING') as retrying
                    from posts where deleted_at is null"""
                    )
                )
                .mappings()
                .one()
            )
            tiles["published_today"] = c.execute(
                text(
                    """select count(*) from posts where status='PUBLISHED'
                       and (published_at at time zone 'Asia/Dhaka')::date = (:n at time zone 'Asia/Dhaka')::date"""
                ),
                {"n": now},
            ).scalar_one()
            tiles["empty_slots"] = c.execute(
                text(
                    "select count(*) from queue_slots where state='EMPTY' and slot_at > :n - interval '7 days'"
                ),
                {"n": now},
            ).scalar_one()
            accounts = [
                dict(r)
                for r in c.execute(
                    text(
                        """select a.display_name, a.platform_key, a.mode, a.state, a.last_publish_at,
                                  a.token_expires_at from platform_accounts a order by a.platform_key"""
                    )
                ).mappings()
            ]
            nxt = [
                dict(r)
                for r in c.execute(
                    text(
                        """select p.post_id, p.post_type, s.slot_at, a.platform_key, a.mode, q.name as queue
                           from queue_slots s join posts p on p.post_id = s.post_id
                           join queues q on q.id = s.queue_id
                           join platform_accounts a on a.id = q.account_id
                           where s.state='FILLED' and s.slot_at >= :n order by s.slot_at limit 10"""
                    ),
                    {"n": now},
                ).mappings()
            ]
            last = [
                dict(r)
                for r in c.execute(
                    text(
                        """select post_id, published_at, published_url from posts
                           where status='PUBLISHED' order by published_at desc nulls last limit 10"""
                    )
                ).mappings()
            ]
            queues = []
            for r in c.execute(
                text("select id, name, status, runway_threshold_days from queues order by name")
            ).mappings():
                days = queue_service.runway_days(c, r["id"], now)
                queues.append(
                    {**dict(r), "runway": round(days, 1), "low": days < r["runway_threshold_days"]}
                )
        return render(
            request,
            "home.html",
            tiles=tiles,
            accounts=accounts,
            next_posts=nxt,
            last_posts=last,
            queues=queues,
        )

    # ------------------------------------------------------------ posts list
    @app.get("/posts", dependencies=auth)
    def posts_list(
        request: Request,
        status: str = "",
        platform: str = "",
        q: str = "",
        queue: str = "",
        page: int = 1,
    ) -> Response:
        where, params = ["p.deleted_at is null"], {}
        if status:
            where.append("p.status = :status")
            params["status"] = status
        if platform:
            where.append("a.platform_key = :platform")
            params["platform"] = platform
        if queue:
            where.append("p.queue_id = cast(:queue as uuid)")
            params["queue"] = queue
        if q:
            where.append("(p.caption ilike :q or p.title ilike :q or p.post_id ilike :q)")
            params["q"] = f"%{q}%"
        clause = " and ".join(where)
        page = max(1, page)
        with ctx.engine.connect() as c:
            total = c.execute(
                text(
                    f"select count(*) from posts p join platform_accounts a on a.id=p.account_id where {clause}"
                ),
                params,
            ).scalar_one()
            rows = [
                dict(r)
                for r in c.execute(
                    text(
                        f"""select p.post_id, p.status, p.post_type, p.language, p.scheduled_at,
                                   left(coalesce(p.caption, p.title, ''), 90) as snippet, a.platform_key,
                                   a.mode, qu.name as queue
                            from posts p join platform_accounts a on a.id=p.account_id
                            left join queues qu on qu.id = p.queue_id
                            where {clause} order by p.created_at desc, p.post_id
                            limit {PAGE_SIZE} offset {(page - 1) * PAGE_SIZE}"""
                    ),
                    params,
                ).mappings()
            ]
            statuses = [
                r[0] for r in c.execute(text("select distinct status from posts order by 1"))
            ]
            platforms = [r[0] for r in c.execute(text("select key from platforms order by 1"))]
            queues = [
                dict(r)
                for r in c.execute(
                    text(
                        "select q.id::text as id, q.name || ' (' || a.platform_key || ')' as label "
                        "from queues q join platform_accounts a on a.id=q.account_id order by 2"
                    )
                ).mappings()
            ]
        return render(
            request,
            "posts.html",
            rows=rows,
            total=total,
            page=page,
            pages=max(1, -(-total // PAGE_SIZE)),
            f={"status": status, "platform": platform, "q": q, "queue": queue},
            statuses=statuses,
            platforms=platforms,
            queues=queues,
        )

    @app.get("/posts/{post_id}", dependencies=auth)
    def post_detail(request: Request, post_id: str) -> Response:
        with ctx.engine.connect() as c:
            p = (
                c.execute(
                    text(
                        """select p.*, a.platform_key, a.mode, a.display_name as account, qu.name as queue
                       from posts p join platform_accounts a on a.id=p.account_id
                       left join queues qu on qu.id=p.queue_id where p.post_id=:p"""
                    ),
                    {"p": post_id},
                )
                .mappings()
                .first()
            )
            if p is None:
                return render(request, "error.html", status=404, message="Post not found")
            audit = [
                dict(r)
                for r in c.execute(
                    text(
                        "select at, actor, action, field, old_value, new_value, reason from post_audit "
                        "where post_id=:p order by id desc limit 50"
                    ),
                    {"p": post_id},
                ).mappings()
            ]
            attempts = [
                dict(r)
                for r in c.execute(
                    text(
                        "select started_at, attempt_type, result, failure_class, error_code, error_message, "
                        "published_url from publish_attempts where post_id=:p order by started_at desc"
                    ),
                    {"p": post_id},
                ).mappings()
            ]
            media = [
                dict(r)
                for r in c.execute(
                    text(
                        "select m.filename, m.mime, m.public_url from post_media pm join media m on m.id=pm.media_id "
                        "where pm.post_id=:p order by pm.sort"
                    ),
                    {"p": post_id},
                ).mappings()
            ]
            problems = review.validate_for_approval(c, dict(p))
        return render(
            request,
            "post_detail.html",
            p=dict(p),
            audit=audit,
            attempts=attempts,
            media=media,
            problems=problems,
        )

    def _act(
        request: Request, post_id: str, fn: Any, ok_msg: str, back: str | None = None
    ) -> Response:
        try:
            with ctx.engine.begin() as c:
                fn(c)
            flash(request, ok_msg)
        except review.ReviewError as exc:
            flash(request, str(exc), "danger")
        return RedirectResponse(back or f"/posts/{post_id}", status_code=303)

    @app.post("/posts/{post_id}/approve", dependencies=post_auth)
    def approve(request: Request, post_id: str) -> Response:
        return _act(request, post_id, lambda c: review.approve(c, post_id, ctx.now()), "Approved")

    @app.post("/posts/{post_id}/submit", dependencies=post_auth)
    def submit(request: Request, post_id: str) -> Response:
        return _act(
            request, post_id, lambda c: review.submit_for_review(c, post_id), "Sent for review"
        )

    @app.post("/posts/{post_id}/reject", dependencies=post_auth)
    def reject(request: Request, post_id: str, comment: str = Form("")) -> Response:
        return _act(request, post_id, lambda c: review.reject(c, post_id, comment), "Rejected")

    @app.post("/posts/{post_id}/cancel", dependencies=post_auth)
    def cancel(request: Request, post_id: str) -> Response:
        return _act(request, post_id, lambda c: review.cancel(c, post_id, ctx.now()), "Cancelled")

    @app.post("/posts/{post_id}/skip", dependencies=post_auth)
    def skip(request: Request, post_id: str) -> Response:
        return _act(
            request, post_id, lambda c: review.cancel(c, post_id, ctx.now(), skip=True), "Skipped"
        )

    @app.post("/posts/{post_id}/retry", dependencies=post_auth)
    def retry(request: Request, post_id: str) -> Response:
        return _act(
            request,
            post_id,
            lambda c: review.retry_now(c, post_id, ctx.now()),
            "Queued for retry on the next publisher run",
        )

    @app.post("/posts/{post_id}/edit", dependencies=post_auth)
    def edit(
        request: Request,
        post_id: str,
        caption: str = Form(""),
        title: str = Form(""),
        link_url: str = Form(""),
        language: str = Form("en"),
        hashtags: str = Form(""),
        evergreen: str = Form(""),
    ) -> Response:
        tags = [
            t if t.startswith("#") else f"#{t}" for t in hashtags.replace(",", " ").split() if t
        ]
        changes = {
            "caption": caption,
            "title": title or None,
            "link_url": link_url or None,
            "language": language,
            "hashtags": tags,
            "evergreen": evergreen == "on",
        }
        return _act(
            request,
            post_id,
            lambda c: review.edit_post(c, post_id, changes, ctx.now()),
            "Saved. Approved posts return to review and must be approved again.",
        )

    @app.post("/posts/bulk-approve", dependencies=post_auth)
    async def bulk_approve(request: Request) -> Response:
        form = await request.form()
        ids = [str(v) for v in form.getlist("ids")]
        if not ids:
            flash(request, "Select at least one post.", "warning")
            return RedirectResponse("/posts", status_code=303)
        with ctx.engine.begin() as c:
            res = review.bulk_approve(c, ids, ctx.now())
        msg = f"Approved {len(res.approved)} post(s)."
        if res.excluded:
            msg += " Not approved: " + "; ".join(
                f"{k}: {v}" for k, v in list(res.excluded.items())[:5]
            )
        flash(request, msg, "warning" if res.excluded else "success")
        return RedirectResponse("/posts", status_code=303)

    # ------------------------------------------------------------ imports
    def _evict() -> None:
        cutoff = time.time() - PENDING_TTL
        for k in [k for k, v in ctx.pending_imports.items() if v[2] < cutoff]:
            ctx.pending_imports.pop(k, None)

    @app.get("/imports", dependencies=auth)
    def imports_page(request: Request) -> Response:
        with ctx.engine.connect() as c:
            batches = [
                dict(r)
                for r in c.execute(
                    text(
                        "select id::text as id, created_at, csv_name, status, counts from import_batches "
                        "order by created_at desc limit 30"
                    )
                ).mappings()
            ]
        return render(request, "imports.html", batches=batches)

    @app.post("/imports", dependencies=post_auth)
    async def imports_upload(
        request: Request,
        csv_file: UploadFile = File(...),
        zips: list[UploadFile] = File(default=[]),
    ) -> Response:
        csv_bytes = await csv_file.read(MAX_UPLOAD + 1)
        blobs = [await z.read(MAX_UPLOAD + 1) for z in zips if z.filename]
        if len(csv_bytes) > MAX_UPLOAD or any(len(b) > MAX_UPLOAD for b in blobs):
            flash(request, "File too large.", "danger")
            return RedirectResponse("/imports", status_code=303)
        try:
            with ctx.engine.begin() as c:
                run, files = import_service.dry_run(
                    c, csv_bytes, blobs, csv_file.filename or "content.csv"
                )
        except UnsafeZipError as exc:
            flash(request, f"ZIP rejected: {exc}", "danger")
            return RedirectResponse("/imports", status_code=303)
        _evict()
        ctx.pending_imports[run.batch_id] = (run, files, time.time())
        ctx.pending_imports[run.batch_id + ":csv"] = (csv_bytes, None, time.time())
        return RedirectResponse(f"/imports/{run.batch_id}", status_code=303)

    @app.get("/imports/template.csv", dependencies=auth)
    def import_template() -> Response:
        header = (
            "content_id,platform,account,queue,post_type,language,title,caption,link,media_file,"
            "hashtags,evergreen,approved,tags,notes,website_url\n"
        )
        example = (
            'C001,FB,iesl_page,main,text_image,en,,"Why calibration matters {link}",'
            "https://ieslbd.com/blog/example,C001.jpg,#calibration #pharma,no,no,,,\n"
        )
        return Response(
            header + example,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="template.csv"'},
        )

    @app.get("/imports/{batch_id}", dependencies=auth)
    def import_report(request: Request, batch_id: str) -> Response:
        with ctx.engine.connect() as c:
            row = (
                c.execute(
                    text(
                        "select id::text as id, created_at, csv_name, status, counts, report from import_batches "
                        "where id = cast(:i as uuid)"
                    ),
                    {"i": batch_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return render(request, "error.html", status=404, message="Import not found")
        pending = batch_id in ctx.pending_imports
        return render(request, "import_report.html", b=dict(row), pending=pending)

    @app.post("/imports/{batch_id}/confirm", dependencies=post_auth)
    def import_confirm(
        request: Request,
        batch_id: str,
        include_warnings: str = Form("on"),
        overwrite: str = Form(""),
    ) -> Response:
        entry = ctx.pending_imports.get(batch_id)
        if entry is None:
            flash(request, "This import has expired. Upload the files again.", "danger")
            return RedirectResponse("/imports", status_code=303)
        run, files, _ = entry
        with ctx.engine.begin() as c:
            counts = import_service.confirm(
                c,
                run,
                files,
                ctx.storage,
                include_warnings=include_warnings == "on",
                overwrite=overwrite == "on",
            )
        ctx.pending_imports.pop(batch_id, None)
        flash(
            request,
            f"Imported: {counts['new']} new, {counts['updated']} updated, "
            f"{counts['unchanged']} unchanged, {counts['skipped']} skipped.",
        )
        return RedirectResponse("/posts", status_code=303)

    @app.get("/imports/{batch_id}/errors.csv", dependencies=auth)
    def import_errors(batch_id: str) -> Response:
        csv_entry = ctx.pending_imports.get(batch_id + ":csv")
        entry = ctx.pending_imports.get(batch_id)
        if csv_entry is None or entry is None:
            return PlainTextResponse("expired", status_code=404)
        body = errors_csv(csv_entry[0], [i for i in entry[0].issues if i.level == Level.ERROR])
        return Response(
            body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="errors.csv"'},
        )

    @app.post("/imports/{batch_id}/undo", dependencies=post_auth)
    def import_undo(request: Request, batch_id: str) -> Response:
        with ctx.engine.begin() as c:
            res = import_service.undo_batch(c, batch_id)
        flash(
            request,
            f"Undone: {res['undone']} post(s) removed; {res['kept']} kept (already published).",
        )
        return RedirectResponse("/imports", status_code=303)
