import logging
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .cache import CACHE_NONE, cache_get, cache_set
from .config import GENIUS_CLIENT_ID, GENIUS_CLIENT_SECRET

_GENIUS_API_BASE = "https://api.genius.com"
_access_token: Optional[str] = None
_token_expires: float = 0.0


def _get_access_token() -> Optional[str]:
    """Get Genius access token using client credentials."""
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires - 60:
        return _access_token

    if not GENIUS_CLIENT_ID or not GENIUS_CLIENT_SECRET:
        return None

    try:
        resp = requests.post(
            "https://api.genius.com/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": GENIUS_CLIENT_ID,
                "client_secret": GENIUS_CLIENT_SECRET,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            _access_token = body.get("access_token")
            _token_expires = time.time() + 3600
            return _access_token
        else:
            logging.warning("Genius token request failed %s: %s", resp.status_code, resp.text[:200])
            # Fallback: use client_id as token (some Genius setups allow this)
            _access_token = GENIUS_CLIENT_ID
            _token_expires = time.time() + 3600
            return _access_token
    except Exception as exc:
        logging.error("Genius token error: %s", exc)
        return None


def _is_enabled() -> bool:
    return bool(GENIUS_CLIENT_ID and GENIUS_CLIENT_SECRET)


def _get_headers() -> dict:
    token = _get_access_token()
    if not token:
        return {}
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "TeleBot/1.0",
        "Accept": "application/json",
    }


def _search_song(query: str) -> Optional[dict]:
    if not _is_enabled() or not query:
        return None
    try:
        hdrs = _get_headers()
        if not hdrs:
            return None
        resp = requests.get(
            f"{_GENIUS_API_BASE}/search",
            params={"q": query},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning("Genius search failed (%s): %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        hits = data.get("response", {}).get("hits") or []
        for item in hits:
            result = item.get("result")
            if result and result.get("url"):
                return result
    except Exception as exc:
        logging.error("Genius search error: %s", exc, exc_info=True)
    return None


def _fetch_lyrics_from_page(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TeleBot/1.0)"},
        )
        if resp.status_code != 200:
            logging.warning("Genius page fetch failed (%s) for %s", resp.status_code, url)
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        containers = soup.select('div[data-lyrics-container="true"]')
        if not containers:
            legacy = soup.find("div", class_="lyrics")
            if legacy:
                containers = [legacy]
        if not containers:
            return None
        parts = []
        for block in containers:
            text = block.get_text(separator="\n").strip()
            if text:
                parts.append(text)
        lyrics = "\n\n".join(parts).strip()
        return lyrics or None
    except Exception as exc:
        logging.error("Genius page parse error: %s", exc, exc_info=True)
        return None


def get_lyrics(title: str, artist: str) -> Optional[str]:
    """Get lyrics for a track by title and artist name."""
    if not _is_enabled():
        return None

    query = f"{artist} {title}".strip()
    if not query:
        return None

    cache_key = f"lyrics:{query.lower()}"
    cached = cache_get(cache_key, 600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        song = _search_song(query)
        if not song:
            cache_set(cache_key, CACHE_NONE)
            return None
        url = song.get("url")
        text = _fetch_lyrics_from_page(url)
        if text:
            text = text.strip()
            cache_set(cache_key, text)
            return text
        cache_set(cache_key, CACHE_NONE)
        return None
    except Exception as exc:
        logging.error("Genius lyrics error for '%s': %s", query, exc, exc_info=True)
        cache_set(cache_key, CACHE_NONE)
        return None
