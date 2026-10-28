"""
User token persistence.

Stores and retrieves OAuth tokens from a local cache file.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Cache location
CACHE_DIR = Path(os.environ.get("QOBUZPROXY_DATA_DIR", Path.home() / ".qobuz-proxy"))
CACHE_FILE = CACHE_DIR / "credentials.json"


def _write_credentials(credentials: dict[str, str]) -> None:
    """Replace the cache only after the complete new contents have been written."""
    CACHE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=CACHE_FILE.parent, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(credentials, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CACHE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_user_token() -> Optional[dict[str, str]]:
    """Load user auth token from cache file."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                creds: dict[str, str] = json.load(f)
                if creds.get("user_id") and creds.get("user_auth_token"):
                    return {
                        "user_id": creds["user_id"],
                        "user_auth_token": creds["user_auth_token"],
                        "email": creds.get("email", ""),
                    }
    except Exception as e:
        logger.warning(f"Failed to load user token: {e}")
    return None


def save_user_token(user_id: str, auth_token: str, email: str) -> bool:
    """Save user auth token to cache file."""
    try:
        existing: dict[str, str] = {}
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                existing = json.load(f)
        existing["user_id"] = user_id
        existing["user_auth_token"] = auth_token
        existing["email"] = email
        _write_credentials(existing)
        logger.info("Saved user token to cache")
        return True
    except Exception as e:
        logger.error(f"Failed to save user token: {e}")
        return False


def clear_user_token() -> bool:
    """Remove user auth token from cache file."""
    try:
        if not CACHE_FILE.exists():
            return True
        with open(CACHE_FILE) as f:
            existing: dict[str, str] = json.load(f)
        for key in ("user_id", "user_auth_token", "email"):
            existing.pop(key, None)
        _write_credentials(existing)
        logger.info("Cleared user token from cache")
        return True
    except Exception as e:
        logger.error(f"Failed to clear user token: {e}")
        return False
