"""Foreign pages, forged hosts, and unbound OAuth callbacks cannot change state."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from qobuz_proxy.webui.routes import register_routes
from qobuz_proxy.webui.security import protect_admin
from tests.webui.helpers import authorize, oauth_callback


@pytest.fixture
async def client():
    app = web.Application()
    app["on_add_speaker"] = AsyncMock(return_value={})
    app["on_logout"] = AsyncMock()
    app["on_auth_token"] = AsyncMock(return_value=True)
    register_routes(app)
    async with TestClient(TestServer(app)) as browser:
        yield browser


@pytest.mark.parametrize("path", ["/api/speakers", "/api/auth/logout"])
async def test_cross_site_simple_post_is_rejected(client, path):
    response = await client.post(
        path,
        data='{"name":"Foreign","dlna_ip":"192.0.2.1"}',
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
    )
    assert response.status == 403
    client.app["on_add_speaker"].assert_not_awaited()
    client.app["on_logout"].assert_not_awaited()


async def test_mutations_require_csrf_even_without_origin(client):
    response = await client.post("/api/auth/logout", json={})
    assert response.status == 403
    client.app["on_logout"].assert_not_awaited()
    authorize(client)
    response = await client.post("/api/auth/logout", json={})
    assert response.status == 200
    client.app["on_logout"].assert_awaited_once()


async def test_valid_token_cannot_override_foreign_origin_or_content_type(client):
    authorize(client)
    for headers, status in [
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ]:
        response = await client.post("/api/auth/logout", data="{}", headers=headers)
        assert response.status == status
    client.app["on_logout"].assert_not_awaited()


async def test_host_cannot_be_rebound_to_an_arbitrary_domain(client):
    response = await client.get("/", headers={"Host": "evil.example"})
    assert response.status == 403


async def test_lan_peer_cannot_spoof_forwarding_headers():
    request = make_mocked_request(
        "GET", "/", headers={"Host": "localhost:8689", "X-Forwarded-For": "127.0.0.1"}
    )
    with patch.object(type(request), "remote", new=property(lambda self: "192.0.2.10")):
        handler = AsyncMock()
        response = await protect_admin(request, handler)
    assert response.status == 403
    handler.assert_not_awaited()


async def test_oauth_rejects_foreign_return_origin(client):
    response = await client.get(
        "/auth/login", params={"origin": "https://evil.example"}, allow_redirects=False
    )
    assert response.status == 400
    assert not client.app["oauth_pending"]


async def test_oauth_callback_requires_initiating_browser_and_is_single_use(client):
    with patch(
        "qobuz_proxy.webui.routes.exchange_code",
        new=AsyncMock(return_value={"user_id": "fake-user", "user_auth_token": "fake-token"}),
    ) as exchange:
        response = await client.get(
            "/auth/callback?code_autorisation=fake-code", allow_redirects=False
        )
        assert response.headers["Location"] == "/?error=invalid_state"
        exchange.assert_not_awaited()
        path = await oauth_callback(client)
        cookies = client.session.cookie_jar.filter_cookies(client.make_url("/"))
        browser = cookies["qobuz_oauth"].value
        client.session.cookie_jar.clear()
        response = await client.get(path + "?code_autorisation=fake-code", allow_redirects=False)
        assert response.headers["Location"] == "/?error=invalid_state"
        exchange.assert_not_awaited()
        client.session.cookie_jar.update_cookies(
            {"qobuz_oauth": browser}, response_url=client.make_url("/")
        )
        response = await client.get(path + "?code_autorisation=fake-code", allow_redirects=False)
        assert response.headers["Location"] == "/"
        response = await client.get(path + "?code_autorisation=fake-code", allow_redirects=False)
        assert response.headers["Location"] == "/?error=invalid_state"
        exchange.assert_awaited_once()


async def test_expired_login_cannot_replace_credentials(client):
    path = await oauth_callback(client)
    state = path.rsplit("/", 1)[1]
    browser, _, origin = client.app["oauth_pending"][state]
    client.app["oauth_pending"][state] = (browser, time.monotonic() - 1, origin)
    response = await client.get(path + "?code_autorisation=fake-code", allow_redirects=False)
    assert response.headers["Location"] == "/?error=invalid_state"
    client.app["on_auth_token"].assert_not_awaited()
