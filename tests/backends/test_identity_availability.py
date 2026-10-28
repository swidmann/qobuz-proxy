"""Transient failures must not disable controls or multiply polling traffic."""

from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from qobuz_proxy.backends.dlna.client import DLNAClient


async def test_polling_is_cached_and_a_transient_check_failure_recovers():
    counts = {"descriptions": 0, "soap": 0}
    fail = [False]

    async def description(_request):
        counts["descriptions"] += 1
        if fail[0]:
            fail[0] = False
            return web.Response(status=503)
        return web.Response(
            text=(
                "<root><device><UDN>uuid:original</UDN><serviceList><service>"
                "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
                "<controlURL>/control</controlURL></service></serviceList></device></root>"
            ),
            content_type="text/xml",
        )

    async def control(_request):
        counts["soap"] += 1
        return web.Response(
            text="<root><CurrentTransportState>PLAYING</CurrentTransportState></root>",
            content_type="text/xml",
        )

    app = web.Application()
    app.router.add_get("/description.xml", description)
    app.router.add_post("/control", control)
    async with TestServer(app) as server:
        client = DLNAClient("127.0.0.1", server.port, str(server.make_url("/description.xml")))
        try:
            await client.connect()
            counts.update(descriptions=0, soap=0)
            for _ in range(10):
                assert await client.get_transport_info() == "PLAYING"
            assert counts == {"descriptions": 0, "soap": 10}
            client._identity_verified_at = 0
            fail[0] = True
            with (
                patch.object(
                    client, "_rediscover_identity", new=AsyncMock(return_value=None)
                ) as scan,
                patch("qobuz_proxy.backends.dlna.client.RETRY_DELAY_SECONDS", 0),
            ):
                assert await client.get_transport_info() == "PLAYING"
                assert counts == {"descriptions": 2, "soap": 11}
                scan.assert_awaited_once()
                assert await client.pause()
                assert counts["descriptions"] == 3  # Writes never trust a stale IP.
        finally:
            await client.disconnect()
