"""Config serialization and atomic YAML writer."""

import os
import tempfile
from pathlib import Path

import yaml

from qobuz_proxy.config import Config, SpeakerConfig, speaker_config_to_dict


def config_to_dict(config: Config) -> dict:
    """Serialize a Config object to a YAML-ready dict.

    Only persists server settings, logging, and speakers. Auth credentials
    are excluded (managed separately). Speaker UUIDs are included so Qobuz
    Connect identity survives restarts.
    """
    return {
        "server": {
            "http_port": config.server.http_port,
            "bind_address": config.server.bind_address,
        },
        "logging": {
            "level": config.logging.level,
        },
        "speakers": [speaker_config_to_dict(s) for s in config.speakers],
    }


def save_config(config: Config, path: Path) -> None:
    """Persist web UI changes while keeping unrelated settings and explicit ports."""
    data: dict = {}
    if path.exists():
        try:
            with path.open() as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError("Expected a configuration mapping")
        except (yaml.YAMLError, ValueError) as e:
            raise OSError("Cannot read saved configuration") from e

    serialized = config_to_dict(config)
    previous = data.get("speakers") or []
    # These optional fields can disappear from the serializer when reset.
    managed = {"device_type", "dlna_description_url", "dlna_udn"}
    for speaker in serialized["speakers"]:
        matches = [
            old
            for old in previous
            if (speaker.get("uuid") and old.get("uuid") == speaker["uuid"])
            or (not old.get("uuid") and old.get("name") == speaker["name"])
        ]
        if len(matches) == 1:
            retained = {k: v for k, v in matches[0].items() if k not in managed}
            retained.update(speaker)
            speaker.clear()
            speaker.update(retained)
    for section in ("server", "logging"):
        data.setdefault(section, {}).update(serialized[section])
    data["speakers"] = serialized["speakers"]
    _write_yaml(data, path)


def persist_speaker_uuids(config: Config, path: Path) -> None:
    """Fill missing UUIDs in existing YAML speaker entries at startup.

    Preserve other settings, including credentials and explicit ports which
    the web UI serializer omits. Do not turn environment-only configuration
    into a YAML speakers list, since that would override later env changes.
    """
    if not config.speakers or not path.exists():
        return
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    uuids = {sc.name: sc.uuid for sc in config.speakers}
    changed = False
    for speaker in data.get("speakers") or []:
        speaker_uuid = uuids.get(speaker.get("name", "QobuzProxy"))
        if not speaker.get("uuid") and speaker_uuid:
            speaker["uuid"] = speaker_uuid
            changed = True
    if changed:
        _write_yaml(data, path)


def persist_speaker_location(
    config: Config, path: Path, speaker: SpeakerConfig, previous: dict
) -> bool:
    """Update one verified endpoint without rewriting other settings or external edits."""
    if not path.exists():
        return False
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    keys = {key: key for key in previous}
    if isinstance(data.get("speakers"), list):
        matches = [s for s in data["speakers"] if s.get("uuid") == speaker.uuid]
        if len(matches) != 1:
            return False
        target = matches[0]
        if target.get("backend", "dlna") != "dlna":
            return False
    elif len(config.speakers) == 1:
        # Keep flat YAML flat, and do not turn environment-only settings into YAML.
        backend = data.get("backend", {})
        if backend.get("type", "dlna") != "dlna":
            return False
        target = backend.get("dlna")
        if not isinstance(target, dict):
            return False
        keys = {key: key.removeprefix("dlna_") for key in previous}
    else:
        return False
    current = {
        key: int(target.get(raw_key, 1400))
        if key == "dlna_port"
        else str(target.get(raw_key) or "").strip()
        for key, raw_key in keys.items()
    }
    if current != previous:
        return False
    for key, raw_key in keys.items():
        target[raw_key] = getattr(speaker, key)
    _write_yaml(data, path)
    return True


def _write_yaml(data: dict, path: Path) -> None:
    """Write config to a YAML file using an atomic replace.

    Writes to a temporary file in the same directory, then uses os.replace()
    so readers never see a partial write.
    """
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
