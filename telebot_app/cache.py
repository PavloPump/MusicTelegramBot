import time
from typing import Any, Dict, Optional

SETTINGS_CACHE_TTL_SEC = 60
_ym_cache: Dict[str, Any] = {}
_settings_cache: Dict[int, Any] = {}
CACHE_NONE = object()


def cache_get(key: str, ttl_sec: int):
    item = _ym_cache.get(key)
    if not item:
        return None
    ts, val = item
    if (time.time() - float(ts)) > float(ttl_sec):
        _ym_cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: Any) -> None:
    if len(_ym_cache) > 2000:
        _ym_cache.clear()
    _ym_cache[key] = (time.time(), CACHE_NONE if val is None else val)


def get_cached_settings(user_id: int) -> Optional[Dict[str, Any]]:
    item = _settings_cache.get(int(user_id))
    if not item:
        return None
    ts, val = item
    if (time.time() - float(ts)) > SETTINGS_CACHE_TTL_SEC:
        _settings_cache.pop(int(user_id), None)
        return None
    if isinstance(val, dict):
        return dict(val)
    return None


def set_cached_settings(user_id: int, settings: Dict[str, Any]) -> None:
    _settings_cache[int(user_id)] = (time.time(), dict(settings))


def invalidate_cached_settings(user_id: int) -> None:
    _settings_cache.pop(int(user_id), None)
