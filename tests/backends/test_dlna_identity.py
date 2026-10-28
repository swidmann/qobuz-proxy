"""A renderer's stable UDN, rather than its old IP, owns its controls."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qobuz_proxy.backends.dlna.client import DLNAClient, DLNAClientError, DLNADeviceInfo
from qobuz_proxy.backends.dlna.discovery import DiscoveredDevice


EXPECTED = "uuid:renderer-original"
OTHER = "uuid:renderer-other"
OLD_IP = "192.0.2.10"
NEW_IP = "192.0.2.20"


def device_info(udn=EXPECTED, ip=NEW_IP):
    return DLNADeviceInfo(
        friendly_name="Same room name",
        udn=udn,
        av_transport_url=f"http://{ip}:1400/transport",
        rendering_control_url=f"http://{ip}:1400/volume",
    )


def discovered(udn=EXPECTED, ip=NEW_IP):
    return DiscoveredDevice(
        friendly_name="Same room name",
        ip=ip,
        port=1400,
        udn=udn,
        location=f"http://{ip}:1400/description.xml",
    )


def make_client():
    client = DLNAClient(
        OLD_IP, description_url=f"http://{OLD_IP}:1400/description.xml", udn=EXPECTED
    )
    client._build_session = MagicMock(return_value=AsyncMock())
    return client


async def test_startup_finds_original_renderer_after_address_change():
    client = make_client()
    client._fetch_device_description = AsyncMock(
        side_effect=[DLNAClientError("old address offline"), device_info()]
    )
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[discovered()],
    ):
        try:
            info = await client.connect()
            assert info.udn == EXPECTED
            assert client.ip == NEW_IP
            assert client.description_url == f"http://{NEW_IP}:1400/description.xml"
        finally:
            await client.disconnect()


@pytest.mark.parametrize(
    "devices", [[], [discovered(OTHER)], [discovered(), discovered(ip="192.0.2.21")]]
)
async def test_missing_or_ambiguous_identity_does_not_choose_another_speaker(devices):
    client = make_client()
    client._fetch_device_description = AsyncMock(side_effect=DLNAClientError("offline"))
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=devices,
    ):
        with pytest.raises(DLNAClientError):
            await client.connect()
    assert client._session is None
    assert client.ip == OLD_IP


async def test_discovery_advertisement_must_match_the_actual_description():
    client = make_client()
    client._fetch_device_description = AsyncMock(
        side_effect=[DLNAClientError("offline"), device_info(OTHER)]
    )
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[discovered()],
    ):
        with pytest.raises(DLNAClientError):
            await client.connect()
    assert client.ip == OLD_IP
    assert client.udn == EXPECTED


async def test_failed_discovery_is_shared_and_rate_limited():
    client = make_client()
    client._session = AsyncMock()
    client._fetch_device_description = AsyncMock(side_effect=DLNAClientError("offline"))
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[],
    ) as scan:
        try:
            assert (
                await asyncio.gather(*(client.ensure_device_identity() for _ in range(10)))
                == [False] * 10
            )
            scan.assert_awaited_once()
        finally:
            await client.disconnect()


async def test_successful_discovery_is_not_repeated_by_concurrent_requests():
    client = make_client()
    client._session = AsyncMock()

    async def fetch():
        if client.ip == OLD_IP:
            raise DLNAClientError("offline")
        return device_info()

    client._fetch_device_description = AsyncMock(side_effect=fetch)
    changed = MagicMock()
    client._on_location_changed = changed
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[discovered()],
    ) as scan:
        try:
            assert all(await asyncio.gather(*(client.ensure_device_identity() for _ in range(10))))
            scan.assert_awaited_once()
            changed.assert_called_once()
        finally:
            await client.disconnect()


async def test_temporary_scan_error_is_retryable():
    client = make_client()
    client._session = AsyncMock()
    client._fetch_device_description = AsyncMock(
        side_effect=[DLNAClientError("offline"), DLNAClientError("offline"), device_info()]
    )
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        side_effect=[OSError("network down"), [discovered()]],
    ):
        try:
            assert not await client.ensure_device_identity()
            client._identity_retry_at = 0
            assert await client.ensure_device_identity()
            assert client.ip == NEW_IP
        finally:
            await client.disconnect()


async def test_failed_persistence_does_not_block_controls_and_is_retried():
    client = make_client()
    client._fetch_device_description = AsyncMock(return_value=device_info(EXPECTED, OLD_IP))
    client._on_location_changed = MagicMock(side_effect=[OSError("read-only config"), None])
    try:
        await client.connect()
        assert client._reported_location is None
        assert await client.ensure_device_identity()
        assert client._on_location_changed.call_count == 2
    finally:
        await client.disconnect()


async def test_sonos_media_renderer_identity_matches_the_root_device():
    client = DLNAClient(OLD_IP, udn="UUID:RINCON_ORIGINAL_MR")
    client._build_session = MagicMock(return_value=AsyncMock())
    client._fetch_device_description = AsyncMock(
        return_value=device_info("uuid:RINCON_ORIGINAL", OLD_IP)
    )
    try:
        await client.connect()
        assert client.udn == "uuid:RINCON_ORIGINAL"
    finally:
        await client.disconnect()


async def test_cancelled_recovery_restores_the_previous_endpoint():
    client = make_client()
    client._session = AsyncMock()
    started = asyncio.Event()

    async def fetch():
        if client.ip == OLD_IP:
            raise DLNAClientError("offline")
        started.set()
        await asyncio.Future()

    client._fetch_device_description = AsyncMock(side_effect=fetch)
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[discovered()],
    ):
        task = asyncio.create_task(client.ensure_device_identity())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.ip == OLD_IP
    assert client.description_url == f"http://{OLD_IP}:1400/description.xml"
    await client.disconnect()


async def test_idle_backend_checks_identity_without_advancing_playback(monkeypatch):
    from qobuz_proxy.backends.dlna.backend import DLNABackend

    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.STATE_POLL_INTERVAL_SECONDS", 0.01)
    backend = DLNABackend(OLD_IP)
    backend._is_connected = True
    backend._client = AsyncMock()
    backend._notify_track_ended = MagicMock()
    checked = asyncio.Event()

    async def unavailable():
        checked.set()
        return False

    backend._client.ensure_device_identity.side_effect = unavailable
    task = asyncio.create_task(backend._poll_state_loop())
    await asyncio.wait_for(checked.wait(), 1)
    task.cancel()
    await task
    backend._client.get_transport_info.assert_not_awaited()
    backend._notify_track_ended.assert_not_called()


def test_sonos_root_identity_is_not_replaced_by_embedded_media_renderer():
    client = DLNAClient(OLD_IP)
    info = client._parse_device_description(
        "<root><device><UDN>uuid:RINCON_ORIGINAL</UDN><deviceList><device>"
        "<UDN>uuid:RINCON_ORIGINAL_MR</UDN></device></deviceList></device></root>",
        f"http://{OLD_IP}:1400",
    )
    assert info.udn == "uuid:RINCON_ORIGINAL"


def renderer_app(identity, controls):
    app = web.Application()

    async def description(request):
        return web.Response(
            text=(
                "<root><device><friendlyName>Same room name</friendlyName>"
                f"<UDN>{identity[0]}</UDN><serviceList>"
                "<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>"
                "<controlURL>/control</controlURL></service>"
                "<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
                "<controlURL>/control</controlURL></service></serviceList></device></root>"
            ),
            content_type="text/xml",
        )

    async def control(request):
        controls.append(request.headers["SOAPAction"])
        return web.Response(
            text="<root><CurrentVolume>31</CurrentVolume></root>", content_type="text/xml"
        )

    app.router.add_get("/description.xml", description)
    app.router.add_post("/control", control)
    return app


async def test_real_http_controls_follow_identity_when_old_endpoint_is_reassigned():
    old_identity, old_controls, new_controls = [EXPECTED], [], []
    async with TestServer(renderer_app(old_identity, old_controls)) as old:
        async with TestServer(renderer_app([EXPECTED], new_controls)) as new:
            changed = []
            client = DLNAClient(
                "127.0.0.1",
                old.port,
                str(old.make_url("/description.xml")),
                on_location_changed=changed.append,
            )
            await client.connect()  # Existing configurations learn their identity here.
            assert client.udn == EXPECTED
            old_identity[0] = OTHER
            candidate = DiscoveredDevice(
                "Same room name",
                "127.0.0.1",
                new.port,
                udn=EXPECTED,
                location=str(new.make_url("/description.xml")),
            )
            with patch(
                "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
                new_callable=AsyncMock,
                return_value=[candidate],
            ):
                try:
                    assert await client.set_volume(27)
                    assert await client.get_volume() == 31
                    assert old_controls == []
                    assert len(new_controls) == 2
                    assert client.port == new.port
                    assert changed[-1].description_url == str(new.make_url("/description.xml"))
                    assert changed[0].udn == changed[-1].udn == EXPECTED
                finally:
                    await client.disconnect()


async def test_real_http_does_not_control_a_different_renderer_when_original_is_missing():
    identity, controls = [EXPECTED], []
    async with TestServer(renderer_app(identity, controls)) as server:
        client = DLNAClient("127.0.0.1", server.port, str(server.make_url("/description.xml")))
        await client.connect()
        identity[0] = OTHER
        with patch(
            "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
            new_callable=AsyncMock,
            return_value=[],
        ):
            try:
                assert not await client.set_volume(27)
                assert controls == []
            finally:
                await client.disconnect()


async def test_reassigned_address_is_not_accepted_as_the_original_renderer():
    client = make_client()
    client._fetch_device_description = AsyncMock(
        side_effect=[device_info(OTHER, OLD_IP), device_info()]
    )
    with patch(
        "qobuz_proxy.backends.dlna.discovery.DLNADiscovery.discover",
        new_callable=AsyncMock,
        return_value=[discovered(OTHER, OLD_IP), discovered()],
    ):
        try:
            info = await client.connect()
            assert info.udn == EXPECTED
            assert client.ip == NEW_IP
        finally:
            await client.disconnect()
