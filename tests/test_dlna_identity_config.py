"""Verified identities survive restarts and targeted endpoint updates."""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.backends.dlna.client import RendererLocation
from qobuz_proxy.config import (
    Config,
    SpeakerConfig,
    _parse_yaml_speakers,
    speaker_config_to_dict,
)
from qobuz_proxy.speaker import Speaker
from qobuz_proxy.webui.config_writer import persist_speaker_location


@pytest.fixture
def configured(tmp_path):
    config = Config()
    config.config_path = tmp_path / "config.yaml"
    speaker = SpeakerConfig(name="Room", uuid="qobuz-identity", dlna_ip="192.0.2.10")
    config.speakers = [speaker]
    data = {
        "server": {"http_port": 8689},
        "custom": {"preserve": True},
        "speakers": [dict(speaker_config_to_dict(speaker), http_port=8690)],
    }
    config.config_path.write_text(yaml.safe_dump(data))
    previous = {
        key: getattr(speaker, key)
        for key in ("dlna_ip", "dlna_port", "dlna_description_url", "dlna_udn")
    }
    return config, speaker, previous, data


def apply_new_location(speaker):
    speaker.dlna_ip = "192.0.2.20"
    speaker.dlna_port = 1500
    speaker.dlna_description_url = "http://192.0.2.20:1500/description.xml"
    speaker.dlna_udn = "uuid:renderer-original"


def test_identity_round_trips_through_yaml_and_component_config(configured):
    config, sc, _, _ = configured
    apply_new_location(sc)
    encoded = speaker_config_to_dict(sc)
    loaded = _parse_yaml_speakers([encoded], config)[0]
    assert loaded.dlna_udn == "uuid:renderer-original"
    speaker = Speaker(loaded, MagicMock(), "test")
    assert speaker._build_component_config().backend.dlna.udn == loaded.dlna_udn


def test_endpoint_update_preserves_ports_identity_and_other_settings(configured):
    config, sc, previous, original = configured
    apply_new_location(sc)
    assert persist_speaker_location(config, config.config_path, sc, previous)
    data = yaml.safe_load(config.config_path.read_text())
    assert data["custom"] == original["custom"]
    saved = data["speakers"][0]
    assert saved["uuid"] == "qobuz-identity"
    assert saved["http_port"] == 8690
    assert saved["dlna_ip"] == sc.dlna_ip
    assert saved["dlna_port"] == sc.dlna_port
    assert saved["dlna_description_url"] == sc.dlna_description_url
    assert saved["dlna_udn"] == sc.dlna_udn


@pytest.mark.parametrize("key,value", [("dlna_ip", "192.0.2.99"), ("dlna_udn", "uuid:replacement")])
def test_endpoint_update_does_not_overwrite_an_external_edit(configured, key, value):
    config, sc, previous, data = configured
    data["speakers"][0][key] = value
    config.config_path.write_text(yaml.safe_dump(data))
    before = config.config_path.read_bytes()
    apply_new_location(sc)
    assert not persist_speaker_location(config, config.config_path, sc, previous)
    assert config.config_path.read_bytes() == before


def test_flat_config_is_preserved_as_flat_config(configured):
    config, sc, previous, _ = configured
    config.config_path.write_text(
        yaml.safe_dump(
            {
                "device": {"uuid": "qobuz-identity"},
                "custom": {"preserve": True},
                "backend": {"type": "dlna", "dlna": {"ip": sc.dlna_ip}},
            }
        )
    )
    apply_new_location(sc)
    assert persist_speaker_location(config, config.config_path, sc, previous)
    data = yaml.safe_load(config.config_path.read_text())
    assert "speakers" not in data
    assert data["backend"]["dlna"]["udn"] == sc.dlna_udn
    assert data["backend"]["dlna"]["ip"] == sc.dlna_ip
    assert data["custom"]["preserve"]


def test_environment_only_config_does_not_gain_a_yaml_override(configured):
    config, sc, previous, _ = configured
    config.config_path.write_text("server: {http_port: 8689}\n")
    before = config.config_path.read_bytes()
    apply_new_location(sc)
    assert not persist_speaker_location(config, config.config_path, sc, previous)
    assert config.config_path.read_bytes() == before


def test_speaker_callback_persists_learned_identity_without_changing_qobuz_uuid(configured):
    config, sc, _, _ = configured
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    speaker = app._make_speaker(sc)
    speaker._on_dlna_location_changed(
        RendererLocation(
            "192.0.2.20", 1500, "http://192.0.2.20:1500/description.xml", "uuid:renderer-original"
        )
    )
    assert yaml.safe_load(config.config_path.read_text())["speakers"][0]["dlna_udn"] == sc.dlna_udn
    assert sc.uuid == "qobuz-identity"


def test_speaker_callback_rolls_back_for_a_retry_if_the_file_cannot_be_written(configured):
    config, sc, previous, _ = configured
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    speaker = app._make_speaker(sc)
    location = RendererLocation(
        "192.0.2.20", 1500, "http://192.0.2.20:1500/description.xml", "uuid:original"
    )
    with patch(
        "qobuz_proxy.webui.config_writer._write_yaml", side_effect=PermissionError("read only")
    ):
        with pytest.raises(PermissionError):
            speaker._on_dlna_location_changed(location)
    assert {key: getattr(sc, key) for key in previous} == previous
    speaker._on_dlna_location_changed(location)
    assert sc.dlna_udn == location.udn


def test_removed_speaker_cannot_update_another_configuration(configured):
    config, sc, previous, _ = configured
    app = QobuzProxy(config)
    config.speakers = [SpeakerConfig(name="Other room")]
    with patch("qobuz_proxy.app.persist_speaker_location") as persist:
        app._on_speaker_config_changed(sc, previous)
    persist.assert_not_called()


def test_identity_only_flat_config_is_valid(configured):
    from qobuz_proxy.config import load_config

    config, _, _, _ = configured
    config.config_path.write_text('backend:\n  type: dlna\n  dlna:\n    udn: "uuid:original"\n')
    loaded = load_config(config.config_path)
    assert len(loaded.speakers) == 1
    assert loaded.speakers[0].dlna_udn == "uuid:original"
    assert loaded.speakers[0].dlna_ip == ""


def test_environment_supports_one_identity_per_speaker(monkeypatch):
    from qobuz_proxy.config import _parse_env_speakers

    monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "First,Second")
    monkeypatch.setenv("QOBUZPROXY_DLNA_UDN", "uuid:first,uuid:second")
    speakers = _parse_env_speakers(Config())
    assert [s.dlna_udn for s in speakers] == ["uuid:first", "uuid:second"]
