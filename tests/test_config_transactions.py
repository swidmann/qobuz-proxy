"""Configuration failures must not change the running installation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config, SpeakerConfig, load_config
from qobuz_proxy.webui.config_writer import save_config
from qobuz_proxy.webui.routes import register_routes
from tests.webui.helpers import authorize


@pytest.fixture
def proxy(tmp_path):
    config = Config(config_path=tmp_path / "config.yaml")
    config.speakers = [
        SpeakerConfig(
            name="Office",
            uuid="office-uuid",
            dlna_ip="192.0.2.10",
            http_port=8690,
            proxy_port=7120,
        )
    ]
    save_config(config, config.config_path)
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    running = MagicMock()
    running.name = "Office"
    running.stop = AsyncMock()
    app._speakers = [running]
    candidate = MagicMock()
    candidate.start = AsyncMock(return_value=True)
    with patch("qobuz_proxy.app.Speaker", return_value=candidate):
        yield app


@pytest.mark.parametrize("operation", ["add", "edit", "remove"])
async def test_failed_save_keeps_disk_and_live_configuration(proxy, operation):
    original = proxy._config.speakers[0]
    running = proxy._speakers[0]
    path = proxy._config.config_path
    before = path.read_bytes()
    with patch("qobuz_proxy.webui.config_writer.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            if operation == "add":
                await proxy._on_add_speaker({"name": "Kitchen", "dlna_ip": "192.0.2.11"})
            elif operation == "edit":
                await proxy._on_edit_speaker("office", {"name": "Changed"})
            else:
                await proxy._on_remove_speaker("office")
    assert proxy._config.speakers == [original]
    assert proxy._config.speakers[0] is original
    assert proxy._speakers == [running]
    running.stop.assert_not_awaited()
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("operation", ["add", "edit"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"name": "   "},
        {"name": "!!!"},
        {"max_quality": 99},
        {"dlna_port": 0},
        {"dlna_port": 65536},
    ],
)
async def test_invalid_configuration_is_rejected_before_writing(proxy, operation, invalid):
    path = proxy._config.config_path
    before = path.read_bytes()
    with pytest.raises(ValueError):
        if operation == "add":
            await proxy._on_add_speaker({"name": "Kitchen", "dlna_ip": "192.0.2.11", **invalid})
        else:
            await proxy._on_edit_speaker("office", invalid)
    assert path.read_bytes() == before
    proxy._speakers[0].stop.assert_not_awaited()
    assert [s.name for s in proxy._config.speakers] == ["Office"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"backend": "unknown"},
        {"backend": "local", "buffer_size": 0},
        {"dlna_ip": ""},
    ],
)
async def test_invalid_backend_configuration_is_not_saved(proxy, invalid):
    path = proxy._config.config_path
    before = path.read_bytes()
    with pytest.raises(ValueError):
        await proxy._on_add_speaker({"name": "Kitchen", "dlna_ip": "192.0.2.11", **invalid})
    assert path.read_bytes() == before


async def test_successful_edit_keeps_credentials_ports_and_other_speakers(proxy):
    path = proxy._config.config_path
    data = yaml.safe_load(path.read_text())
    data["qobuz"] = {"user_id": "fake-user", "auth_token": "fake-token"}
    data["custom"] = {"retain": True}
    data["speakers"][0].update(http_port=8690, proxy_port=7120, bind_address="127.0.0.1")
    path.write_text(yaml.safe_dump(data))
    proxy._config = load_config(path)
    other = SpeakerConfig(name="Kitchen", uuid="kitchen-uuid", backend_type="local", http_port=8691)
    proxy._config.speakers.append(other)
    new_speaker = MagicMock()
    new_speaker.start = AsyncMock(return_value=True)
    with patch("qobuz_proxy.app.Speaker", return_value=new_speaker):
        await proxy._on_edit_speaker("office", {"name": "Study", "max_quality": 6})
    saved = yaml.safe_load(path.read_text())
    assert saved["qobuz"] == data["qobuz"]
    assert saved["custom"] == data["custom"]
    assert saved["speakers"][0]["http_port"] == 8690
    assert saved["speakers"][0]["proxy_port"] == 7120
    assert saved["speakers"][0]["bind_address"] == "127.0.0.1"
    assert proxy._config.speakers[1] is other  # P3 callbacks retain this object.
    restarted = load_config(path)
    assert [s.name for s in restarted.speakers] == ["Study", "Kitchen"]
    assert restarted.speakers[0].uuid == "office-uuid"


@pytest.mark.parametrize("operation", ["add", "edit", "remove"])
async def test_api_reports_save_failure_without_exposing_local_paths(proxy, operation):
    app = web.Application()
    app["on_add_speaker"] = proxy._on_add_speaker
    app["on_edit_speaker"] = proxy._on_edit_speaker
    app["on_remove_speaker"] = proxy._on_remove_speaker
    register_routes(app)
    async with TestClient(TestServer(app)) as client:
        authorize(client)
        with patch("qobuz_proxy.app.save_config", side_effect=PermissionError("/private/path")):
            if operation == "add":
                response = await client.post(
                    "/api/speakers", json={"name": "Kitchen", "dlna_ip": "192.0.2.11"}
                )
            elif operation == "edit":
                response = await client.put("/api/speakers/office", json={"name": "Study"})
            else:
                response = await client.delete("/api/speakers/office")
            assert response.status == 500
            assert "save" in (await response.json())["error"].lower()
            assert "/private/path" not in await response.text()


@pytest.mark.parametrize("body", [[], None, {"name": 123}])
async def test_api_rejects_malformed_speaker_body(body):
    app = web.Application()
    callback = AsyncMock()
    app["on_add_speaker"] = callback
    app["on_edit_speaker"] = callback
    register_routes(app)
    async with TestClient(TestServer(app)) as client:
        authorize(client)
        for method, url in [(client.post, "/api/speakers"), (client.put, "/api/speakers/office")]:
            response = await method(url, json=body)
            assert response.status == 400
    callback.assert_not_awaited()
