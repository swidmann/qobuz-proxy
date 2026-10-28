"""Bound registrations while preserving playback, prefetch, and recent seeks."""

import time
from unittest.mock import MagicMock


from qobuz_proxy.backends.dlna.proxy_server import AudioProxyServer


def test_registry_is_bounded_and_keeps_playing_and_prefetched_tracks():
    proxy = AudioProxyServer(MagicMock())
    proxy.set_retained_keys_provider(lambda: {"playing", "next"})
    for key in ("playing", "next"):
        proxy.register_track(key, "https://cdn.example/audio.flac")
    proxy._tracks["playing"].last_accessed = time.monotonic() - 7200
    for i in range(5000):
        proxy.register_track(str(i), "https://cdn.example/audio.flac")
    assert len(proxy._tracks) <= 128
    assert {"playing", "next", "4999"} <= proxy._tracks.keys()
    assert "0" not in proxy._tracks


def test_inactive_expired_tracks_are_removed_after_they_are_released():
    proxy = AudioProxyServer(MagicMock())
    retained = {"old"}
    proxy.set_retained_keys_provider(lambda: retained)
    proxy.register_track("old", "https://cdn.example/audio.flac")
    proxy._tracks["old"].last_accessed = time.monotonic() - 7200
    proxy.register_track("fresh", "https://cdn.example/audio.flac")
    assert "old" in proxy._tracks
    retained.clear()
    proxy.register_track("latest", "https://cdn.example/audio.flac")
    assert "old" not in proxy._tracks
