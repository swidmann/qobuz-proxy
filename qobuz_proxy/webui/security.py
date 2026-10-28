"""Local administration boundary, independent of LAN audio and discovery."""

import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from aiohttp import web


@web.middleware
async def protect_admin(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    try:
        peer = ipaddress.ip_address(request.remote or "")
        host = request.url.host
    except ValueError:
        return web.json_response({"error": "Invalid request address"}, status=400)
    if not peer.is_loopback or host not in ("localhost", "127.0.0.1", "::1"):
        return web.json_response(
            {"error": "Administration is available on this Mac only."}, status=403
        )

    origin = f"{request.scheme}://{request.host}"
    supplied = request.headers.get("Origin")
    callback = request.method == "GET" and (
        request.path == "/auth/callback" or request.path.startswith("/auth/callback/")
    )
    if (supplied is not None and supplied != origin) or (
        request.headers.get("Sec-Fetch-Site") == "cross-site" and not callback
    ):
        return web.json_response({"error": "Request origin not allowed"}, status=403)

    if request.method not in ("GET", "HEAD"):
        token = request.headers.get("X-Qobuz-CSRF", "")
        if not secrets.compare_digest(token, request.app["csrf_token"]):
            return web.json_response({"error": "Reload this page and try again."}, status=403)
        if request.content_type != "application/json":
            return web.json_response({"error": "JSON content is required"}, status=415)

    try:
        response = await handler(request)
    except web.HTTPException as redirect:
        response = redirect
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' https: http: data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        }
    )
    return response
