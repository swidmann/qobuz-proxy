"""Web UI API routes for status and authentication."""

import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from qobuz_proxy.auth.oauth import build_oauth_url, exchange_code, extract_code
from qobuz_proxy.webui.speaker_routes import register_speaker_routes
from qobuz_proxy.webui.security import protect_admin

logger = logging.getLogger(__name__)

_start_time = time.monotonic()

_STATIC_DIR = Path(__file__).parent / "static"


def _format_uptime(seconds: float) -> str:
    """Format uptime as 'Xd Yh Zm', 'Xh Ym', or 'Xm'."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_INGRESS_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


def _ingress_base(request: web.Request) -> str:
    """Return the base path the UI is served under, with a trailing slash.

    Home Assistant ingress forwards requests with an ``X-Ingress-Path`` header
    holding the path prefix (e.g. ``/api/hassio_ingress/<token>``). Using it as
    the document ``<base href>`` lets all relative links/fetches resolve both
    behind ingress and on direct ``http://host:8689/`` access.

    The value is reflected into the served HTML and into redirect ``Location``
    headers, so it is validated against a strict path charset; anything else
    (including header-injection attempts on the directly-exposed port) falls
    back to the root base.
    """
    ingress_path = request.headers.get("X-Ingress-Path", "")
    if ingress_path and _INGRESS_PATH_RE.match(ingress_path):
        return f"{ingress_path.rstrip('/')}/"
    return "/"


async def _handle_index(request: web.Request) -> web.Response:
    """Serve index.html with version-based cache busting."""
    html = (_STATIC_DIR / "index.html").read_text()
    html = html.replace("{{version}}", request.app.get("version", "0"))
    html = html.replace("{{base}}", _ingress_base(request))
    html = html.replace("{{http_port}}", str(request.app.get("http_port", 8689)))
    html = html.replace("{{csrf_token}}", request.app["csrf_token"])
    html = html.replace("{{commit}}", request.app.get("commit", ""))
    return web.Response(text=html, content_type="text/html")


async def _handle_status(request: web.Request) -> web.Response:
    """Return JSON with auth state, speakers, and system info."""
    app = request.app
    uptime_seconds = time.monotonic() - _start_time
    data: dict[str, Any] = {
        "auth": app["auth_state"],
        "speakers": app["get_speakers"](),
        "version": app["version"],
        "commit": app.get("commit", ""),
        "uptime": _format_uptime(uptime_seconds),
    }
    return web.json_response(data)


async def _handle_auth_login(request: web.Request) -> web.Response:
    """Redirect the user to the Qobuz OAuth sign-in page."""
    origin = request.query.get("origin", "")
    if not origin:
        return web.json_response({"error": "missing origin parameter"}, status=400)
    expected = f"{request.scheme}://{request.host}{_ingress_base(request).rstrip('/')}"
    if origin != expected:
        return web.json_response({"error": "Invalid login origin"}, status=400)
    now = time.monotonic()
    pending = request.app["oauth_pending"]
    for key, (_, expiry, _) in list(pending.items()):
        if expiry <= now:
            del pending[key]
    if len(pending) >= 64:
        return web.json_response({"error": "Too many pending logins. Try again later."}, status=429)
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    pending[state] = (browser, now + 600, f"{request.scheme}://{request.host}")
    # Qobuz's custom OAuth flow does not promise to echo a state parameter.
    # Put the nonce in the callback path, which survives its redirect unchanged.
    redirect_url = f"{origin}/auth/callback/{state}"
    oauth_url = build_oauth_url(redirect_url)
    response = web.HTTPFound(oauth_url)
    response.set_cookie(
        "qobuz_oauth", browser, httponly=True, samesite="Lax", secure=request.secure, max_age=600
    )
    raise response


async def _handle_auth_callback(request: web.Request) -> web.Response:
    """Handle the OAuth redirect from Qobuz, exchange code, and authenticate."""
    base = _ingress_base(request)

    state = request.match_info.get("state", "")
    pending = request.app["oauth_pending"].get(state)
    cookie = request.cookies.get("qobuz_oauth", "")
    if (
        pending is None
        or pending[1] <= time.monotonic()
        or pending[2] != f"{request.scheme}://{request.host}"
        or not secrets.compare_digest(cookie, pending[0])
    ):
        raise web.HTTPFound(f"{base}?error=invalid_state")
    del request.app["oauth_pending"][state]  # One use, consumed before network I/O.

    try:
        code = extract_code(str(request.url))
    except ValueError:
        raise web.HTTPFound(f"{base}?error=missing_code")

    try:
        creds = await exchange_code(code)
    except Exception:
        logger.exception("OAuth code exchange failed")
        raise web.HTTPFound(f"{base}?error=exchange_failed")

    profile = {
        "email": creds.get("email", ""),
        "name": creds.get("display_name", ""),
        "avatar": creds.get("avatar", ""),
    }

    callback = request.app["on_auth_token"]
    try:
        success: bool = await callback(
            creds["user_id"], creds["user_auth_token"], profile, validated=True
        )
    except OSError:
        logger.exception("Could not save Qobuz login")
        raise web.HTTPFound(f"{base}?error=save_failed")

    if not success:
        raise web.HTTPFound(f"{base}?error=auth_failed")

    response = web.HTTPFound(base)
    response.del_cookie("qobuz_oauth")
    raise response


async def _handle_logout(request: web.Request) -> web.Response:
    """Clear auth token via callback."""
    callback = request.app["on_logout"]
    try:
        await callback()
    except OSError:
        logger.exception("Could not clear Qobuz login")
        return web.json_response({"error": "Could not sign out. Please try again."}, status=500)
    return web.json_response({"status": "ok"})


def register_routes(app: web.Application) -> None:
    """Register all web UI routes on the given application."""
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/status", _handle_status)
    app.router.add_get("/auth/login", _handle_auth_login)
    app.router.add_get("/auth/callback", _handle_auth_callback)
    app.router.add_get("/auth/callback/{state}", _handle_auth_callback)
    app.router.add_post("/api/auth/logout", _handle_logout)
    register_speaker_routes(app)
    app.router.add_static("/static", _STATIC_DIR)
    app["csrf_token"] = secrets.token_urlsafe(32)
    app["oauth_pending"] = {}
    app.middlewares.append(protect_admin)
