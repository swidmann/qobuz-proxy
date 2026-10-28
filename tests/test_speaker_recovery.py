"""Recover edited offline speakers without orphaning previous startup tasks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config, SpeakerConfig


def make_speaker(name="Office", starts=True):
    speaker = MagicMock()
    speaker.name = name
    speaker.start = AsyncMock(return_value=starts)
    speaker.stop = AsyncMock()
    speaker.get_status.return_value = {"id": name.lower(), "status": "idle"}
    return speaker


@pytest.fixture
def proxy():
    config = Config(
        speakers=[
            SpeakerConfig(
                name="Office",
                uuid="stable-uuid",
                dlna_ip="192.0.2.10",
                http_port=8690,
                proxy_port=7120,
            )
        ]
    )
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    return app


async def test_edited_offline_speaker_recovers_with_same_identity(proxy):
    old = make_speaker()
    proxy._speakers = [old]
    offline = make_speaker(starts=False)
    recovered = make_speaker(name="Study")
    with (
        patch("qobuz_proxy.app.Speaker", side_effect=[offline, recovered]) as factory,
        patch("qobuz_proxy.app.SPEAKER_RETRY_DELAYS_SECONDS", (0.001,)),
    ):
        try:
            status = await proxy._on_edit_speaker("office", {"name": "Study", "max_quality": 6})
            assert proxy._speakers == []
            assert status["status"] == "starting"
            await asyncio.wait_for(proxy._speaker_retry_tasks["study"], 1)
            assert proxy._speakers == [recovered]
            assert proxy._speaker_retry_tasks == {}
            assert all(c.kwargs["config"].uuid == "stable-uuid" for c in factory.call_args_list)
            assert all(c.kwargs["config"].max_quality == 6 for c in factory.call_args_list)
            old.stop.assert_awaited_once()
        finally:
            await proxy._stop_speakers()


async def test_edit_retries_startup_exceptions_too(proxy):
    offline = make_speaker()
    offline.start.side_effect = OSError("temporary port conflict")
    recovered = make_speaker()
    with (
        patch("qobuz_proxy.app.Speaker", side_effect=[offline, recovered]),
        patch("qobuz_proxy.app.SPEAKER_RETRY_DELAYS_SECONDS", (0.001,)),
    ):
        try:
            status = await proxy._on_edit_speaker("office", {"max_quality": 6})
            assert "warning" in status
            offline.stop.assert_awaited()
            await asyncio.wait_for(proxy._speaker_retry_tasks["office"], 1)
            assert proxy._speakers == [recovered]
        finally:
            await proxy._stop_speakers()


async def test_edit_awaits_previous_retry_cleanup_before_reusing_ports(proxy):
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()

    async def previous_retry():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()

    old_retry = asyncio.create_task(previous_retry())
    proxy._speaker_retry_tasks["office"] = old_retry
    await asyncio.sleep(0)
    replacement = make_speaker()
    edit = None
    with patch("qobuz_proxy.app.Speaker", return_value=replacement):
        try:
            edit = asyncio.create_task(proxy._on_edit_speaker("office", {"max_quality": 6}))
            await asyncio.wait_for(cleanup_entered.wait(), 1)
            replacement.start.assert_not_awaited()
            release_cleanup.set()
            await asyncio.wait_for(edit, 1)
            assert old_retry.done()
            assert proxy._speakers == [replacement]
        finally:
            release_cleanup.set()
            if edit:
                await asyncio.gather(edit, return_exceptions=True)
            await asyncio.gather(old_retry, return_exceptions=True)
            await proxy._stop_speakers()


async def test_shutdown_awaits_retry_cleanup(proxy):
    entered, released = asyncio.Event(), asyncio.Event()

    async def slow_start():
        entered.set()
        await asyncio.Event().wait()

    async def slow_stop():
        await asyncio.sleep(0.01)
        released.set()

    speaker = make_speaker()
    speaker.start.side_effect = slow_start
    speaker.stop.side_effect = slow_stop
    with (
        patch("qobuz_proxy.app.Speaker", return_value=speaker),
        patch("qobuz_proxy.app.SPEAKER_RETRY_DELAYS_SECONDS", (0.001,)),
    ):
        proxy._schedule_speaker_retry(proxy._config.speakers[0])
        await asyncio.wait_for(entered.wait(), 1)
        await proxy._stop_speakers()
        assert released.is_set()
        assert proxy._speaker_retry_tasks == {}
        assert proxy._speakers == []


async def test_retired_retry_does_not_start_obsolete_configuration(proxy):
    obsolete = proxy._config.speakers[0]
    proxy._config.speakers = []
    with (
        patch("qobuz_proxy.app.Speaker") as factory,
        patch("qobuz_proxy.app.SPEAKER_RETRY_DELAYS_SECONDS", (0.001,)),
    ):
        proxy._schedule_speaker_retry(obsolete)
        await asyncio.wait_for(proxy._speaker_retry_tasks["office"], 1)
        factory.assert_not_called()


async def test_overlapping_edits_leave_only_latest_speaker_running(proxy):
    entered, release = asyncio.Event(), asyncio.Event()
    first = make_speaker()
    second = make_speaker()
    old = make_speaker()
    proxy._speakers = [old]

    async def first_start():
        entered.set()
        await release.wait()
        return True

    first.start.side_effect = first_start
    with patch("qobuz_proxy.app.Speaker", side_effect=[first, second]):
        edit1 = asyncio.create_task(proxy._on_edit_speaker("office", {"max_quality": 6}))
        await asyncio.wait_for(entered.wait(), 1)
        edit2 = asyncio.create_task(proxy._on_edit_speaker("office", {"max_quality": 7}))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(edit1, edit2)
    first.stop.assert_awaited_once()
    assert proxy._speakers == [second]
    assert proxy._config.speakers[0].max_quality == 7
