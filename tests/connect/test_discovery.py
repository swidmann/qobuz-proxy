"""Discovery stays visible to clients on the LAN and on its own Mac."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.config import Config
from qobuz_proxy.connect.discovery import DiscoveryService


@pytest.mark.parametrize("name", ["Kitchen", "Living Room"])
async def test_mdns_joins_only_the_advertised_interface(name):
    config = Config()
    config.device.name = name
    discovery = DiscoveryService(config, "test")
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        # No InterfaceChoice.All/default: hosts with many Docker bridges must
        # not consume the socket's membership budget before reaching the LAN.
        zeroconf.assert_called_once_with(interfaces=["10.0.1.9"])
        info = zeroconf.return_value.register_service.call_args.args[0]
        assert info.parsed_addresses() == ["10.0.1.9"]
        assert info.port == config.server.http_port
        assert info.properties[b"Name"].decode() == name
        await discovery._unregister_mdns()
        zeroconf.return_value.unregister_service.assert_called_once_with(info)
        zeroconf.return_value.close.assert_called_once()


async def test_no_resolved_address_does_not_fall_back_to_all_interfaces():
    discovery = DiscoveryService(Config(), "test")
    with (
        patch.object(discovery, "_get_local_ip", return_value=None),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        zeroconf.assert_not_called()


async def test_conflicting_service_reuses_the_scoped_socket():
    discovery = DiscoveryService(Config(), "test")
    zc = MagicMock()
    zc.register_service.side_effect = [RuntimeError("name conflict"), None]
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf", return_value=zc) as zeroconf,
    ):
        await discovery._register_mdns()
        zeroconf.assert_called_once_with(interfaces=["10.0.1.9"])
        assert zc.register_service.call_args.kwargs == {"cooperating_responders": True}
        await discovery._unregister_mdns()


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [("speaker", "SPEAKER"), ("tv", "TV"), ("streamer", "STREAMER"), ("soundbar", "SOUNDBAR")],
)
async def test_device_type_is_advertised(device_type, expected):
    config = Config()
    config.device.device_type = device_type
    discovery = DiscoveryService(config, "test")

    response = await discovery._handle_display_info(MagicMock())
    assert json.loads(response.text)["type"] == expected

    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        info = zeroconf.return_value.register_service.call_args.args[0]
        assert info.properties[b"type"].decode() == expected
        await discovery._unregister_mdns()


@pytest.fixture
async def mac_discovery(monkeypatch):
    monkeypatch.setattr("qobuz_proxy.connect.discovery.sys.platform", "darwin")
    monkeypatch.setattr("qobuz_proxy.connect.discovery.MDNS_REFRESH_INTERVAL_SECONDS", 0.01)
    discovery = DiscoveryService(Config(), "test")
    refreshed = asyncio.Queue()

    async def refresh(info):
        refreshed.put_nowait(info)
        broadcast = asyncio.get_running_loop().create_future()
        broadcast.set_result(None)
        return broadcast

    zc = MagicMock()
    zc.async_update_service = AsyncMock(side_effect=refresh)
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf", return_value=zc),
    ):
        await discovery._register_mdns()
        try:
            yield discovery, zc, refreshed
        finally:
            await discovery._unregister_mdns()


async def test_mac_refreshes_announcements_without_client_queries(mac_discovery):
    discovery, zc, refreshed = mac_discovery
    info = zc.register_service.call_args.args[0]
    assert await asyncio.wait_for(refreshed.get(), 1) is info
    assert await asyncio.wait_for(refreshed.get(), 1) is info
    zc.register_service.assert_called_once()
    assert not discovery._mdns_refresh_task.done()


async def test_refresh_failure_does_not_stop_later_announcements(mac_discovery, caplog):
    discovery, zc, refreshed = mac_discovery
    refresh = zc.async_update_service.side_effect
    failed = False

    async def fail_once(info):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("temporary network failure")
        return await refresh(info)

    zc.async_update_service.side_effect = fail_once
    await asyncio.wait_for(refreshed.get(), 1)
    assert "temporary network failure" in caplog.text
    assert zc.async_update_service.await_count >= 2
    assert not discovery._mdns_refresh_task.done()


async def test_stop_cancels_inflight_broadcast_before_goodbye(mac_discovery):
    discovery, zc, _ = mac_discovery
    started = asyncio.Event()
    broadcast = asyncio.get_running_loop().create_future()

    async def refresh(info):
        started.set()
        return broadcast

    def goodbye(info):
        assert broadcast.cancelled()

    zc.async_update_service.side_effect = refresh
    zc.unregister_service.side_effect = goodbye
    await asyncio.wait_for(started.wait(), 1)
    task = discovery._mdns_refresh_task

    await discovery._unregister_mdns()

    assert task.done()
    assert discovery._mdns_refresh_task is None
    assert discovery._zeroconf is None
    assert discovery._service_info is None
    calls = zc.async_update_service.await_count
    await asyncio.sleep(0.03)
    assert zc.async_update_service.await_count == calls
    zc.unregister_service.assert_called_once()


async def test_restart_replaces_the_refresh_task(mac_discovery):
    discovery, _, refreshed = mac_discovery
    previous = discovery._mdns_refresh_task
    await discovery._unregister_mdns()
    await discovery._register_mdns()

    assert previous.done()
    assert discovery._mdns_refresh_task is not previous
    await asyncio.wait_for(refreshed.get(), 1)


async def test_linux_keeps_query_driven_announcements(monkeypatch):
    monkeypatch.setattr("qobuz_proxy.connect.discovery.sys.platform", "linux")
    discovery = DiscoveryService(Config(), "test")
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        try:
            assert discovery._mdns_refresh_task is None
            zeroconf.return_value.async_update_service.assert_not_called()
        finally:
            await discovery._unregister_mdns()
