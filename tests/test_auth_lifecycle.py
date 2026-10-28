"""Authentication changes must reach every speaker and survive failed writes."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.auth import clear_user_token, save_user_token
from qobuz_proxy.config import Config, SpeakerConfig
from qobuz_proxy.webui.routes import register_routes
from tests.webui.helpers import authorize, oauth_callback


@pytest.fixture
def proxy():
    config = Config(
        speakers=[SpeakerConfig(name="Office", backend_type="stub", uuid="stable-uuid")]
    )
    app = QobuzProxy(config)
    app._app_id = "fake-app-id"
    app._app_secret = "fake-app-secret"
    app._api_client = MagicMock()
    app._api_client.user_auth_token = "old-token"
    app._auth_state.update(
        authenticated=True,
        user_id="old-user",
        email="old@example.test",
        name="Old",
        avatar="old-avatar",
    )
    speaker = MagicMock()
    speaker.name = "Office"
    speaker.stop = AsyncMock()
    app._speakers = [speaker]
    return app


async def test_new_login_recreates_speakers_with_new_client(proxy):
    old_client, old_speaker = proxy._api_client, proxy._speakers[0]
    shared_state = proxy._auth_state
    replacement = MagicMock()
    replacement.name = "Office"
    replacement.start = AsyncMock(return_value=True)
    replacement.stop = AsyncMock()
    with (
        patch("qobuz_proxy.app.save_user_token", return_value=True),
        patch("qobuz_proxy.app.Speaker", return_value=replacement) as factory,
    ):
        await proxy._on_auth_token("new-user", "new-token", {"name": "New"})
    old_speaker.stop.assert_awaited_once()
    assert proxy._speakers == [replacement]
    client = factory.call_args.kwargs["api_client"]
    assert client is proxy._api_client and client is not old_client
    assert client.user_auth_token == "new-token"
    assert client.user_id == "new-user"
    assert factory.call_args.kwargs["config"].uuid == "stable-uuid"
    assert shared_state is proxy._auth_state
    assert shared_state["user_id"] == "new-user"
    assert shared_state["avatar"] == ""


async def test_failed_token_save_preserves_existing_session_and_retry(proxy):
    client, speakers, state = proxy._api_client, list(proxy._speakers), dict(proxy._auth_state)
    retry = asyncio.create_task(asyncio.Event().wait())
    proxy._auth_retry_task = retry
    with patch("qobuz_proxy.app.save_user_token", return_value=False):
        try:
            with pytest.raises(OSError):
                await proxy._on_auth_token("new-user", "new-token")
            assert proxy._api_client is client
            assert proxy._speakers == speakers
            assert proxy._auth_state == state
            speakers[0].stop.assert_not_awaited()
            assert not retry.done()
        finally:
            await proxy._cancel_auth_retry()


async def test_logout_failure_preserves_session(proxy):
    state, client = dict(proxy._auth_state), proxy._api_client
    old = proxy._speakers[0]
    with patch("qobuz_proxy.app.clear_user_token", return_value=False):
        with pytest.raises(OSError):
            await proxy._on_logout()
    assert proxy._auth_state == state
    assert proxy._api_client is client
    old.stop.assert_not_awaited()


async def test_logout_clears_complete_profile(proxy):
    old_client = proxy._api_client
    old = proxy._speakers[0]
    with patch("qobuz_proxy.app.clear_user_token", return_value=True):
        await proxy._on_logout()
    assert proxy._auth_state == {
        "authenticated": False,
        "user_id": "",
        "email": "",
        "name": "",
        "avatar": "",
    }
    assert proxy._api_client is None
    assert proxy._speakers == []
    old.stop.assert_awaited_once()
    old_client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("operation", ["save", "clear"])
@pytest.mark.parametrize("failure_stage", ["write", "replace"])
def test_token_write_failure_keeps_previous_credentials(tmp_path, operation, failure_stage):
    path = tmp_path / "credentials.json"
    original = json.dumps(
        {"user_id": "fake-user", "user_auth_token": "fake-token", "other": "retain"}
    )
    path.write_text(original)

    def interrupted_write(_credentials, stream, **_kwargs):
        stream.write("partial")
        raise OSError("disk full")

    failure = (
        patch("qobuz_proxy.auth.credentials.json.dump", side_effect=interrupted_write)
        if failure_stage == "write"
        else patch("qobuz_proxy.auth.credentials.os.replace", side_effect=OSError("disk full"))
    )
    with (
        patch("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path),
        patch("qobuz_proxy.auth.credentials.CACHE_FILE", path),
        failure,
    ):
        success = (
            save_user_token("new-user", "new-token", "")
            if operation == "save"
            else clear_user_token()
        )
    assert success is False
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]


async def test_oauth_save_failure_redirects_to_useful_error():
    app = web.Application()
    callback = AsyncMock(side_effect=OSError("/private/path"))
    app["on_auth_token"] = callback
    register_routes(app)
    async with TestClient(TestServer(app)) as client:
        authorize(client)
        with (
            patch("qobuz_proxy.webui.routes.extract_code", return_value="fake-code"),
            patch(
                "qobuz_proxy.webui.routes.exchange_code",
                new=AsyncMock(
                    return_value={"user_id": "fake-user", "user_auth_token": "fake-token"}
                ),
            ),
        ):
            response = await client.get(
                (await oauth_callback(client)) + "?code=fake-code", allow_redirects=False
            )
            assert response.status == 302
            assert response.headers["Location"] == "/?error=save_failed"


async def test_logout_api_reports_failure():
    app = web.Application()
    app["on_logout"] = AsyncMock(side_effect=OSError("/private/path"))
    register_routes(app)
    async with TestClient(TestServer(app)) as client:
        authorize(client)
        response = await client.post("/api/auth/logout")
        assert response.status == 500
        assert "sign out" in (await response.json())["error"].lower()
        assert "/private/path" not in await response.text()
