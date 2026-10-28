"""Use returned file resolution, not the nominal Qobuz tier, for compatibility."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.capabilities import apply_device_overrides, parse_protocol_info_sink
from qobuz_proxy.backends.dlna.client import DLNAClient
from qobuz_proxy.playback.metadata import MetadataService, TrackMetadata


@pytest.mark.parametrize("generation,depth,quality", [(0, 16, 6), (1, 16, 6), (2, 24, 7)])
def test_sonos_limits_follow_explicit_software_generation(generation, depth, quality):
    caps = parse_protocol_info_sink("http-get:*:audio/flac:*")
    apply_device_overrides(caps, "Sonos, Inc.", "Sonos Port", generation)
    assert caps.max_sample_rate == 48000
    assert caps.max_bit_depth == depth
    assert caps.max_quality == quality


def test_sonos_generation_is_read_from_description():
    info = DLNAClient("192.0.2.1")._parse_device_description(
        "<root><device><swGen>2</swGen></device></root>", "http://192.0.2.1:1400"
    )
    assert info.software_generation == 2


@pytest.mark.parametrize("rate", [44.1, 48])
async def test_sonos_accepts_compatible_24_bit_files(rate):
    api = MagicMock()
    api.get_track_url = AsyncMock(
        return_value={
            "url": "https://cdn.example/compatible.flac",
            "format_id": 7,
            "sampling_rate": rate,
            "bit_depth": 24,
        }
    )
    service = MetadataService(api, 7, max_sample_rate=48000, max_bit_depth=24)
    track = TrackMetadata(track_id="fake-track")
    await service._fetch_streaming_url(track)
    assert track.streaming_url.endswith("compatible.flac")
    assert track.sample_rate == int(rate * 1000)
    assert track.bit_depth == 24
    assert "192" not in track.to_dict()["quality_name"]
    assert "96" not in track.to_dict()["quality_name"]


@pytest.mark.parametrize(
    "hires",
    [
        {"sampling_rate": 96, "bit_depth": 24},
        {"sampling_rate": 192, "bit_depth": 24},
        {"sampling_rate": 48, "bit_depth": 32},
        {},
    ],
)
async def test_incompatible_or_unknown_hires_falls_back_before_delivery(hires):
    api = MagicMock()
    api.get_track_url = AsyncMock(
        side_effect=[
            {"url": "https://cdn.example/unsupported.flac", "format_id": 7, **hires},
            {
                "url": "https://cdn.example/cd.flac",
                "format_id": 6,
                "sampling_rate": 44.1,
                "bit_depth": 16,
            },
        ]
    )
    service = MetadataService(api, 7, max_sample_rate=48000, max_bit_depth=24)
    track = TrackMetadata(track_id="fake-track")
    await service._fetch_streaming_url(track)
    assert track.streaming_url == "https://cdn.example/cd.flac"
    assert track.actual_quality == 6
    assert [c.args[1] for c in api.get_track_url.call_args_list] == [7, 6]


async def test_s1_never_receives_24_bit_media():
    api = MagicMock()
    api.get_track_url = AsyncMock(
        side_effect=[
            {
                "url": "https://cdn.example/24bit.flac",
                "format_id": 7,
                "sampling_rate": 44.1,
                "bit_depth": 24,
            },
            {
                "url": "https://cdn.example/cd.flac",
                "format_id": 6,
                "sampling_rate": 44.1,
                "bit_depth": 16,
            },
        ]
    )
    service = MetadataService(api, 7, max_sample_rate=48000, max_bit_depth=16)
    track = TrackMetadata(track_id="fake-track")
    await service._fetch_streaming_url(track)
    assert track.streaming_url.endswith("/cd.flac")


def test_quality_label_and_payload_report_actual_file():
    track = TrackMetadata(actual_quality=27, sample_rate=44100, bit_depth=24)
    data = track.to_dict()
    assert data["quality_name"] == "FLAC 24-bit/44.1kHz"
    assert data["sample_rate"] == 44100 and data["bit_depth"] == 24
