"""FastAPI dashboard application factory (PRD-06, PRD-11)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, text
from starlette.middleware.sessions import SessionMiddleware

from socialcontrol.config.settings import Settings, get_settings
from socialcontrol.dashboard import auth
from socialcontrol.media.storage import storage_from_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
DHAKA = ZoneInfo("Asia/Dhaka")
IDLE_TIMEOUT = 24 * 3600


class NotAuthenticatedError(Exception):
    pass


class CsrfError(Exception):
    pass


def _fmt_dt(value: Any, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not isinstance(value, datetime):
        return "" if value is None else str(value)
    return value.astimezone(DHAKA).strftime(fmt)


class Ctx:
    """Everything routes need: injected so tests can swap the clock, storage and notifiers."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        now: Callable[[], datetime],
        storage: Any,
        adapters: Callable[[dict[str, Any]], Any] | None,
        router: Any,
    ) -> None:
        self.engine, self.settings, self.now = engine, settings, now
        self.storage, self.adapters, self.router = storage, adapters, router
        self.throttle = auth.LoginThrottle()
        self.totp = auth.TotpVerifier(settings.sc_totp_secret) if settings.sc_totp_secret else None
        self.pending_imports: dict[str, tuple[Any, Any, float]] = {}


def create_app(
    engine: Engine,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
    storage: Any = None,
    adapters: Callable[[dict[str, Any]], Any] | None = None,
    router: Any = None,
) -> FastAPI:
    settings = settings or get_settings()
    if len(settings.sc_signing_key) < 16:
        raise RuntimeError("SC_SIGNING_KEY must be set (16+ characters) to run the dashboard")
    ctx = Ctx(
        engine,
        settings,
        now or (lambda: datetime.now(UTC)),
        storage or storage_from_settings(settings),
        adapters,
        router,
    )
    app = FastAPI(title="SocialControl", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.sc_signing_key,
        session_cookie="sc_session",
        same_site="strict",
        https_only=settings.sc_env == "prod",
        max_age=7 * 24 * 3600,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["dt"] = _fmt_dt
    app.state.templates = templates

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if settings.sc_env == "prod":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotAuthenticatedError)
    async def _not_auth(request: Request, exc: NotAuthenticatedError) -> Response:
        if request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "authentication required"}, status_code=401)

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError) -> Response:
        return JSONResponse({"error": "invalid or missing CSRF token"}, status_code=403)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------ login / logout
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        return render(request, "login.html", error=None, needs_code=ctx.totp is not None)

    @app.post("/login")
    def login(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        csrf: str = Form(""),
        code: str = Form(""),
    ) -> Response:
        if not auth.csrf_ok(request.session.get("csrf"), csrf):
            raise CsrfError
        key = f"{request.client.host if request.client else '?'}|{email.lower()}"
        if ctx.throttle.is_locked(key):
            return render(
                request,
                "login.html",
                error="Too many attempts. Try again later.",
                status=429,
                needs_code=ctx.totp is not None,
            )
        ok = bool(settings.sc_admin_email and settings.sc_admin_password_hash) and (
            email.strip().lower() == settings.sc_admin_email.lower()
            and auth.verify_password(password, settings.sc_admin_password_hash)
        )
        if ok and ctx.totp is not None and not ctx.totp.verify(code):
            ok = False  # same generic message: do not reveal which factor failed
        if not ok:
            locked = ctx.throttle.failure(key)
            log_security(ctx, "LOGIN_FAILED", {"email": email[:80], "locked": locked})
            return render(
                request,
                "login.html",
                error="Incorrect email, password or code.",
                status=401,
                needs_code=ctx.totp is not None,
            )
        ctx.throttle.success(key)
        request.session.clear()
        request.session["user"] = settings.sc_admin_email
        request.session["csrf"] = auth.new_csrf_token()
        request.session["seen"] = int(time.time())
        log_security(ctx, "LOGIN_OK", {"email": email[:80]})
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")) -> Response:
        if auth.csrf_ok(request.session.get("csrf"), csrf):
            request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------ shared helpers
    def render(request: Request, name: str, status: int = 200, **kw: Any) -> Response:
        session = request.session
        if "csrf" not in session:
            session["csrf"] = auth.new_csrf_token()
        flashes = session.pop("flash", [])
        banners = banner_list(ctx) if session.get("user") else []
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": session["csrf"],
                "flashes": flashes,
                "banners": banners,
                "user": session.get("user"),
                "now": ctx.now(),
                **kw,
            },
            status_code=status,
        )

    app.state.render = render

    from socialcontrol.dashboard import routes_content, routes_ops

    routes_content.register(app)
    routes_ops.register(app)
    return app


# ---------------------------------------------------------------- dependencies and helpers
def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        # Not signed in: leave the session alone so an open login form keeps its CSRF token.
        raise NotAuthenticatedError
    if time.time() - request.session.get("seen", 0) > IDLE_TIMEOUT:
        request.session.clear()  # idle timeout: sign the user out completely
        raise NotAuthenticatedError
    request.session["seen"] = int(time.time())
    return str(user)


async def require_csrf(request: Request) -> None:
    form = await request.form()
    if not auth.csrf_ok(request.session.get("csrf"), str(form.get("csrf") or "")):
        raise CsrfError


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("flash", [])
    request.session["flash"] = [*request.session["flash"], {"m": message, "l": level}]


def log_security(ctx: Ctx, kind: str, detail: dict[str, Any]) -> None:
    import json

    with ctx.engine.begin() as conn:
        conn.execute(
            text("insert into security_events (kind, detail) values (:k, cast(:d as jsonb))"),
            {"k": kind, "d": json.dumps(detail)},
        )


def banner_list(ctx: Ctx) -> list[dict[str, str]]:
    """Critical banners shown on every page (DSH-02)."""
    out: list[dict[str, str]] = []
    with ctx.engine.connect() as conn:
        if conn.execute(text("select value from settings where key='kill_switch'")).scalar():
            out.append(
                {"level": "danger", "text": "Kill switch is ON: nothing is being published."}
            )
        n = conn.execute(
            text("select count(*) from platform_accounts where state in ('TOKEN_EXPIRED','ERROR')")
        ).scalar_one()
        if n:
            out.append({"level": "danger", "text": f"{n} account(s) need reconnecting."})
        last = conn.execute(
            text("select max(started_at) from job_runs where job='publisher'")
        ).scalar()
        if last is not None and (ctx.now() - last).total_seconds() > 3600:
            out.append({"level": "warning", "text": "The publisher has not run for over an hour."})
    return out
