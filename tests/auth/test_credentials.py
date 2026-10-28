import json
import os
import stat
from unittest.mock import patch

from qobuz_proxy.auth.credentials import load_user_token, save_user_token


class TestUserTokenPersistence:
    def test_default_umask_keeps_new_cache_private(self, tmp_path):
        cache_dir = tmp_path / "private-cache"
        cache_file = cache_dir / "credentials.json"
        previous = os.umask(0o022)
        try:
            with (
                patch("qobuz_proxy.auth.credentials.CACHE_DIR", cache_dir),
                patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file),
            ):
                assert save_user_token("fake-user", "fake-token", "")
        finally:
            os.umask(previous)
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600

    def test_save_and_load_user_token(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "secret"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            with patch("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path):
                save_user_token("999", "tok_abc", "test@example.com")
                result = load_user_token()
        assert result is not None
        assert result["user_id"] == "999"
        assert result["user_auth_token"] == "tok_abc"
        assert result["email"] == "test@example.com"

    def test_load_user_token_missing_file(self, tmp_path):
        cache_file = tmp_path / "nonexistent.json"
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            result = load_user_token()
        assert result is None

    def test_load_user_token_no_token_in_file(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "s"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            result = load_user_token()
        assert result is None

    def test_save_preserves_app_credentials(self, tmp_path):
        cache_file = tmp_path / "credentials.json"
        cache_file.write_text(json.dumps({"app_id": "123", "app_secret": "secret"}))
        with patch("qobuz_proxy.auth.credentials.CACHE_FILE", cache_file):
            with patch("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path):
                save_user_token("999", "tok", "e@x.com")
        data = json.loads(cache_file.read_text())
        assert data["app_id"] == "123"
        assert data["app_secret"] == "secret"
        assert data["user_id"] == "999"
