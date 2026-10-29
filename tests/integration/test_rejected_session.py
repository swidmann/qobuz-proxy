"""An application-level session refusal must end automatic reconnection."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.config import Config, SpeakerConfig
from qobuz_proxy.connect.discovery import DiscoveryService
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType, ProtocolCodec
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.connect.ws_manager import SessionRejected, TokenRefreshRequired, WsManager
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker


def tokens(endpoint, session=None):
    return ConnectTokens(
        session_id=session or str(uuid.uuid4()),
        ws_token=JWTConnectToken(jwt="fake-token", exp=9999999999, endpoint=endpoint),
    )


def error_frame(message="sessionUuid mismatch", code="1001"):
    batch = pb.QConnectBatch()
    message_pb = batch.messages.add(messageType=1)
    message_pb.error.code = code
    message_pb.error.message = message
    return ProtocolCodec(uuid.uuid4().bytes).encode_payload(batch.SerializeToString())


async def reject(manager):
    await manager._handle_message(error_frame())


async def test_real_socket_stops_after_qobuz_rejects_the_session():
    attempts = []
    rejected = asyncio.Event()

    async def server(socket):
        attempts.append(1)
        for _ in range(3):  # AUTHENTICATE, SUBSCRIBE, JOIN_SESSION
            await socket.recv()
        await socket.send(error_frame())
        rejected.set()
        await socket.close()

    async with websockets.serve(server, "127.0.0.1", 0, subprotocols=["qws"]) as service:
        endpoint = f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"
        manager = WsManager(Config())
        manager.set_tokens(tokens(endpoint), activate=True)
        try:
            await manager.start()
            await asyncio.wait_for(rejected.wait(), 1)
            await asyncio.wait_for(asyncio.shield(manager._receive_task), 0.3)
            assert attempts == [1]
            assert not manager.is_connected
            assert not manager.is_renderer_active
            assert not manager._should_run
        finally:
            await manager.stop()


async def test_new_session_reconnects_but_replaying_the_rejected_one_cannot():
    attempts = []

    async def server(socket):
        attempts.append(1)
        for _ in range(3):
            await socket.recv()
        if len(attempts) == 1:
            await socket.send(error_frame())
            await socket.close()
        else:
            batch = pb.QConnectBatch()
            batch.messages.add(messageType=43).srvrRndrSetActive.active = True
            await socket.send(
                ProtocolCodec(uuid.uuid4().bytes).encode_payload(batch.SerializeToString())
            )
            await socket.wait_closed()

    async with websockets.serve(server, "127.0.0.1", 0, subprotocols=["qws"]) as service:
        endpoint = f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"
        manager = WsManager(Config())
        old = tokens(endpoint)
        manager.set_tokens(old, activate=True)
        try:
            await manager.start()
            await asyncio.wait_for(asyncio.shield(manager._receive_task), 1)
            assert manager.set_tokens(tokens(endpoint, old.session_id), activate=True) is False
            refresher = AsyncMock()
            manager.set_token_refresher(refresher)
            await manager.start()
            assert not manager._should_run
            refresher.assert_not_awaited()
            fresh = tokens(endpoint)
            assert manager.set_tokens(fresh, activate=True) is True
            await manager.start()

            async def ready():
                while not manager.is_renderer_active:
                    await asyncio.sleep(0)

            await asyncio.wait_for(ready(), 1)
            assert attempts == [1, 1]
            assert manager.set_tokens(old, activate=True) is False
            assert manager.is_renderer_active
            assert manager._session_uuid == uuid.UUID(fresh.session_id).bytes
        finally:
            await manager.stop()


async def test_rejection_clears_only_matching_discovery_and_leaves_other_rooms_active():
    old = tokens("ws://fake.invalid")
    manager = WsManager(Config())
    manager.set_tokens(old, activate=True)
    manager._is_connected = True
    discovery = DiscoveryService(Config(), "fake-app")
    discovery.set_session(old)
    manager.on_session_rejected(discovery.clear_session)
    other = WsManager(Config())
    other.set_tokens(old, activate=True)
    other._is_connected = True
    other._ws = AsyncMock()
    with pytest.raises(SessionRejected):
        await reject(manager)
    assert discovery.get_received_tokens() is None
    assert discovery._current_session_id == ""
    assert not await manager.send_volume_changed(20)
    assert await other.send_volume_changed(20)
    fresh = tokens("ws://fake.invalid")
    discovery.set_session(fresh)
    discovery.clear_session(uuid.UUID(old.session_id).bytes)
    assert discovery.get_received_tokens() is fresh


async def test_discovery_refuses_expired_session_and_preserves_a_fresh_selection():
    old = tokens("ws://fake.invalid")
    manager = WsManager(Config())
    manager.set_tokens(old, activate=True)
    manager._is_connected = True
    speaker = Speaker(SpeakerConfig(name="Living Room"), MagicMock(), "fake-app")
    speaker._ws_manager = manager
    speaker._setup_websocket = AsyncMock()
    discovery = DiscoveryService(Config(), "fake-app", on_connect=speaker._on_app_connected)
    discovery.set_session(old)
    manager.on_session_rejected(discovery.clear_session)
    with pytest.raises(SessionRejected):
        await reject(manager)

    app = web.Application()
    app.router.add_post("/connect", discovery._handle_connect)
    app.router.add_get("/info", discovery._handle_connect_info)

    def connect_body(value):
        return {
            "session_id": value.session_id,
            "jwt_qconnect": {
                "jwt": value.ws_token.jwt,
                "exp": value.ws_token.exp,
                "endpoint": value.ws_token.endpoint,
            },
        }

    async with TestClient(TestServer(app)) as client:
        refused = await client.post("/connect", json=connect_body(old))
        assert refused.status == 409
        assert (await (await client.get("/info")).json())["current_session_id"] == ""
        speaker._setup_websocket.assert_not_awaited()

        fresh = tokens("ws://fake.invalid")
        accepted = await client.post("/connect", json=connect_body(fresh))
        assert accepted.status == 200
        await asyncio.sleep(0)
        speaker._setup_websocket.assert_awaited_once()
        assert speaker._setup_websocket.call_args.args[0].session_id == fresh.session_id

        refused = await client.post("/connect", json=connect_body(old))
        assert refused.status == 409
        assert (await (await client.get("/info")).json())["current_session_id"] == fresh.session_id
        speaker._setup_websocket.assert_awaited_once()


async def test_setup_already_queued_before_refusal_cannot_restore_the_expired_session():
    manager = WsManager(Config())
    old = tokens("ws://fake.invalid")
    manager.set_tokens(old)
    manager._is_connected = True
    with pytest.raises(SessionRejected):
        await reject(manager)
    manager.start = AsyncMock()
    speaker = Speaker(SpeakerConfig(name="Living Room"), MagicMock(), "fake-app")
    speaker._ws_manager = manager
    speaker._queue = MagicMock()
    speaker._player = MagicMock()
    speaker._discovery = DiscoveryService(Config(), "fake-app")
    fresh = tokens("ws://fake.invalid")
    speaker._discovery.set_session(fresh)
    await speaker._setup_websocket(old)
    manager.start.assert_not_awaited()
    assert speaker._discovery.get_received_tokens() is fresh
    assert not manager._should_run


async def test_top_level_session_refusal_is_handled_too(monkeypatch):
    manager = WsManager(Config())
    old = tokens("ws://fake.invalid")
    manager.set_tokens(old)
    monkeypatch.setattr(
        manager._codec,
        "decode_frame",
        lambda _data: DecodedMessage(
            msg_type=MessageType.ERROR, error_code=1001, error_message="sessionUuid mismatch"
        ),
    )
    with pytest.raises(SessionRejected):
        await manager._handle_message(b"fake-frame")
    assert not manager.accepts_session(old.session_id)


@pytest.mark.parametrize(
    "code,message", [("1001", "Invalid volume"), ("1003", "Message gap too large")]
)
async def test_other_protocol_errors_do_not_invalidate_a_valid_session(code, message):
    manager = WsManager(Config())
    current = tokens("ws://fake.invalid")
    manager.set_tokens(current)
    manager._is_connected = True
    manager._should_run = True
    handler = MagicMock()
    manager.register_handler(1, handler)
    await manager._handle_message(error_frame(message, code))
    handler.assert_called_once()
    assert manager._should_run
    assert manager.accepts_session(current.session_id)


def test_late_refusal_for_an_old_socket_does_not_invalidate_the_new_session():
    manager = WsManager(Config())
    old = tokens("ws://fake.invalid")
    manager.set_tokens(old)
    manager._connection_session_uuid = uuid.UUID(old.session_id).bytes
    fresh = tokens("ws://fake.invalid")
    manager.set_tokens(fresh, activate=True)
    with pytest.raises(TokenRefreshRequired):
        manager._check_session_rejection("1001", "sessionUuid mismatch")
    assert manager.accepts_session(fresh.session_id)
    assert manager._session_uuid == uuid.UUID(fresh.session_id).bytes


async def test_short_lived_connections_cannot_reset_backoff(monkeypatch):
    monkeypatch.setattr("qobuz_proxy.connect.ws_manager.INITIAL_RECONNECT_DELAY", 0.005)
    monkeypatch.setattr("qobuz_proxy.connect.ws_manager.MAX_RECONNECT_DELAY", 0.02)
    fourth = asyncio.Event()
    attempts = []

    async def server(socket):
        attempts.append(1)
        for _ in range(3):
            await socket.recv()
        if len(attempts) >= 4:
            fourth.set()
        await socket.close()

    async with websockets.serve(server, "127.0.0.1", 0, subprotocols=["qws"]) as service:
        manager = WsManager(Config())
        manager.set_tokens(tokens(f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"))
        try:
            await manager.start()
            await asyncio.wait_for(fourth.wait(), 1)
            assert manager._reconnect_delay == 0.02
        finally:
            await manager.stop()


async def test_stable_connection_resets_backoff(monkeypatch):
    monkeypatch.setattr("qobuz_proxy.connect.ws_manager.STABLE_CONNECTION_SECONDS", 0)

    async def server(socket):
        for _ in range(3):
            await socket.recv()
        await socket.close()

    async with websockets.serve(server, "127.0.0.1", 0, subprotocols=["qws"]) as service:
        manager = WsManager(Config())
        manager.set_tokens(tokens(f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"))
        manager._reconnect_delay = 60
        manager._should_run = True
        await manager._connect_and_run()
        assert manager._reconnect_delay == 1
        await manager.stop()


async def test_fresh_app_selection_wakes_a_long_backoff():
    entered = asyncio.Event()

    class ObservedEvent(asyncio.Event):
        async def wait(self):
            entered.set()
            return await super().wait()

    manager = WsManager(Config())
    manager._token_update_event = ObservedEvent()
    manager.set_tokens(tokens("ws://fake.invalid"))
    manager._should_run = True
    manager._reconnect_delay = 60
    calls = []

    async def connection():
        calls.append(1)
        if len(calls) == 2:
            manager._should_run = False

    manager._connect_and_run = AsyncMock(side_effect=connection)
    task = asyncio.create_task(manager._connection_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        manager.set_tokens(tokens("ws://fake.invalid"), activate=True)
        await asyncio.wait_for(task, 0.5)
        assert len(calls) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
