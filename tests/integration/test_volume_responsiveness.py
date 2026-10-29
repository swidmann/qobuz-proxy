"""Volume gestures must converge without replaying a backlog of old positions."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import xml.etree.ElementTree as ET

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qobuz_proxy.backends.dlna.backend import DLNABackend
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.volume_handler import VolumeCommandHandler
from qobuz_proxy.proto import qconnect_payload_pb2 as pb


TRACK = "http://proxy.invalid/audio/current.flac"


@asynccontextmanager
async def volume_rig(description_delay=0.005, volume=30):
    rig = SimpleNamespace(
        volume=volume,
        writes=[],
        descriptions=0,
        control_reads=0,
        delay=0,
        start=time.monotonic(),
        track_uri=TRACK,
    )

    async def description(_request):
        rig.descriptions += 1
        await asyncio.sleep(rig.delay)
        return web.Response(
            text=(
                "<root><device><UDN>uuid:volume-test-renderer</UDN><serviceList>"
                "<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>"
                "<controlURL>/control</controlURL></service>"
                "<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
                "<controlURL>/control</controlURL></service></serviceList></device></root>"
            ),
            content_type="text/xml",
        )

    async def control(request):
        action = request.headers["SOAPAction"].strip('"').split("#")[-1]
        if action == "SetVolume":
            body = ET.fromstring(await request.text())
            value = int(next(e.text for e in body.iter() if e.tag.endswith("DesiredVolume")))
            rig.volume = value
            rig.writes.append((value, time.monotonic() - rig.start))
        else:
            rig.control_reads += 1
        await asyncio.sleep(0.005)
        return web.Response(
            text=(
                f"<root><CurrentVolume>{rig.volume}</CurrentVolume>"
                f"<TrackURI>{rig.track_uri}</TrackURI>"
                "<CurrentTransportState>PLAYING</CurrentTransportState>"
                "</root>"
            ),
            content_type="text/xml",
        )

    app = web.Application()
    app.router.add_get("/description.xml", description)
    app.router.add_post("/control", control)
    async with TestServer(app) as server:
        client = DLNAClient("127.0.0.1", server.port, str(server.make_url("/description.xml")))
        await client.connect()
        backend = DLNABackend("127.0.0.1")
        backend._client = client
        backend._is_sonos = True
        backend._current_proxy_url = TRACK
        player = QobuzPlayer(MagicMock(), MagicMock(), backend)
        backend.on_external_playback(player.release_external_playback)
        rig.client, rig.backend, rig.player = client, backend, player
        rig.delay = description_delay
        rig.descriptions = 0
        rig.start = time.monotonic()
        try:
            yield rig
        finally:
            backend._current_proxy_url = None
            await backend.disconnect()


async def gesture(rig, values, interval=0.03, relative=False):
    tasks = []
    apply = rig.player.set_volume_delta if relative else rig.player.set_volume
    for value in values:
        tasks.append(asyncio.create_task(apply(value)))
        await asyncio.sleep(interval)
    last_input = time.monotonic() - rig.start - interval
    await asyncio.gather(*tasks)
    # Also settle the old implementation's detached delayed send for comparison.
    trailing = getattr(rig.client, "_volume_debounce_task", None)
    if trailing:
        await trailing
    return last_input


async def test_slow_identity_checks_do_not_queue_obsolete_slider_positions():
    async with volume_rig(description_delay=0.25) as rig:
        await gesture(rig, [10, 20, 30, 40, 50, 60])
        assert rig.volume == 60
        assert len(rig.writes) <= 2
        assert rig.descriptions <= 2  # Still a fresh identity check before each write.


async def test_rapid_relative_steps_accumulate_while_the_first_write_is_pending():
    async with volume_rig(description_delay=0.1, volume=30) as rig:
        await gesture(rig, [1, 1, 1], interval=0.01, relative=True)
        assert rig.volume == 33
        assert rig.player._volume == 33


async def test_relative_steps_clamp_each_step_and_refresh_after_an_external_adjustment():
    async with volume_rig(volume=98) as rig:
        await gesture(rig, [10, -5], interval=0.001, relative=True)
        assert rig.volume == 95
        rig.volume = 17  # A physical knob or another controller changed it while idle.
        await rig.player.set_volume_delta(1)
        assert rig.volume == 18


async def test_completion_of_an_old_write_cannot_roll_back_the_app_volume():
    backend = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def apply(level):
        if level == 10:
            entered.set()
            await release.wait()

    backend.set_volume = AsyncMock(side_effect=apply)
    player = QobuzPlayer(MagicMock(), MagicMock(), backend)
    reports = []
    player.set_volume_report_callback(AsyncMock(side_effect=reports.append))
    old = asyncio.create_task(player.set_volume(10))
    await entered.wait()
    await player.set_volume(20)
    release.set()
    await old
    assert player._volume == 20
    assert reports[-1] == 20


async def test_volume_protocol_commands_keep_only_the_latest_pending_position():
    async with volume_rig(description_delay=0.1) as rig:
        handler = VolumeCommandHandler(rig.player)
        tasks = []
        for level in [10, 20, 30, 40, 50, 60]:
            message = pb.QConnectMessage()
            message.srvrRndrSetVolume.volume = level
            tasks.append(asyncio.create_task(handler.handle_message(42, message)))
            await asyncio.sleep(0.005)
        await asyncio.gather(*tasks)
        assert rig.volume == 60
        assert len(rig.writes) <= 2


async def test_a_late_volume_read_does_not_erase_a_new_slider_position():
    backend = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return 30

    backend.get_volume = AsyncMock(side_effect=read)
    backend.set_volume = AsyncMock()
    player = QobuzPlayer(MagicMock(), MagicMock(), backend)
    stale = asyncio.create_task(player.get_volume())
    await entered.wait()
    await player.set_volume(55)
    release.set()
    assert await stale == 55
    assert player._volume == 55


async def test_disconnect_cancels_a_delayed_volume_without_sending_it():
    async with volume_rig() as rig:
        rig.backend._last_volume_send_at = time.monotonic() + 60
        command = asyncio.create_task(rig.player.set_volume(70))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rig.backend._current_proxy_url = None
        await rig.backend.disconnect()
        result = await asyncio.gather(command, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert rig.writes == []
        assert rig.backend._pending_volume is None


async def test_session_revocation_drops_old_volume_but_allows_a_fresh_selection():
    async with volume_rig() as rig:
        rig.backend._last_volume_send_at = time.monotonic() + 60
        stale = asyncio.create_task(rig.player.set_volume(70))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rig.player.invalidate_pending_commands()
        rig.backend._last_volume_send_at = 0
        await rig.player.set_volume(25)
        result = await asyncio.gather(stale, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert [v for v, _time in rig.writes] == [25]


async def test_old_backend_read_does_not_replace_its_new_pending_target():
    backend = DLNABackend("192.0.2.1")
    backend._client = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        entered.set()
        await release.wait()
        return 30

    backend._client.get_volume.side_effect = read
    stale = asyncio.create_task(backend.get_volume())
    await entered.wait()
    await backend.set_volume(55)
    release.set()
    assert await stale == 55


async def test_delayed_volume_does_not_change_a_source_that_took_over(monkeypatch):
    monkeypatch.setattr("qobuz_proxy.backends.dlna.backend.VOLUME_MIN_INTERVAL_SECONDS", 0.03)
    async with volume_rig() as rig:
        rig.backend._last_volume_send_at = time.monotonic()
        command = asyncio.create_task(rig.player.set_volume(70))
        await asyncio.sleep(0)
        rig.track_uri = "x-sonos-spotify:example"
        await command
        assert rig.writes == []
        assert rig.backend._external_playback


async def test_a_slow_room_does_not_block_another_room():
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_volume(_level):
        entered.set()
        await release.wait()
        return True

    slow, fast = DLNABackend("192.0.2.1"), DLNABackend("192.0.2.2")
    slow._client = AsyncMock()
    fast._client = AsyncMock()
    slow._client.set_volume.side_effect = slow_volume
    fast._client.set_volume.return_value = True
    first = asyncio.create_task(slow.set_volume(20))
    try:
        await entered.wait()
        await asyncio.wait_for(fast.set_volume(25), 1)
        fast._client.set_volume.assert_awaited_once_with(25)
        assert not first.done()
    finally:
        release.set()
        await first


@pytest.mark.parametrize("fixed", [True, False])
async def test_fixed_volume_and_out_of_range_commands(fixed):
    async with volume_rig() as rig:
        rig.player.set_fixed_volume_mode(fixed)
        rig.backend._fixed_volume = fixed
        await rig.player.set_volume(150)
        await rig.player.set_volume(-20)
        if fixed:
            assert rig.writes == []
            assert await rig.player.get_volume() == 100
        else:
            assert [v for v, _time in rig.writes] == [100, 0]


async def test_failed_send_does_not_disable_the_next_gesture():
    async with volume_rig() as rig:
        send = rig.client.set_volume
        rig.client.set_volume = AsyncMock(return_value=False)
        await rig.player.set_volume(40)
        assert rig.writes == []
        rig.client.set_volume = send
        await rig.player.set_volume(45)
        assert rig.volume == 45
