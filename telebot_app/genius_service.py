import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .cache import CACHE_NONE, cache_get, cache_set
from .config import GENIUS_CLIENT_ID, GENIUS_CLIENT_SECRET, GENIUS_ACCESS_TOKEN

_GENIUS_API_BASE = "https://api.genius.com"


def _is_enabled() -> bool:
    return bool(GENIUS_ACCESS_TOKEN or (GENIUS_CLIENT_ID and GENIUS_CLIENT_SECRET))


def _get_headers() -> dict:
    token = GENIUS_ACCESS_TOKEN or GENIUS_CLIENT_ID
    if not token:
        return {}
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (compatible; Zvonko/1.0)",
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
        logging.error("Genius search error: %s", exc)
    return None


def _clean_lyrics_text(text: str) -> str:
    """Clean and format lyrics text."""
    if not text:
        return ""
    
    import re
    lines = text.split('\n')
    cleaned = []
    
    junk_patterns = [
        'embed', 'Embed', 'you might also like', 'You might also like',
        'see genius', 'See genius', 'contributors', 'Contributors',
        'translations', 'Translations', 'writer', 'Writer',
        'produced by', 'Produced by', 'copyright', 'Copyright',
        'all rights reserved', 'All Rights Reserved',
        'lyrics', 'Lyrics', 'текст песни', 'Текст песни',
        'song info', 'Song Info', 'about', 'About',
    ]
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip junk lines
        if any(junk.lower() in line.lower() for junk in junk_patterns):
            continue
        
        # Remove lines that are just "Lyrics" or song titles
        if line.endswith(' Lyrics') or line.endswith(' lyrics'):
            continue
        
        # Remove ALL bracketed metadata (section markers)
        if line.startswith('[') and line.endswith(']'):
            continue
        
        # Remove lines that are just numbers (timestamps, etc)
        if line.isdigit() and len(line) <= 3:
            continue
        
        cleaned.append(line)
    
    # Remove consecutive empty lines
    result = []
    prev_empty = False
    for line in cleaned:
        if not line:
            if not prev_empty:
                result.append('')
            prev_empty = True
        else:
            result.append(line)
            prev_empty = False
    
    # Remove leading/trailing empty lines
    while result and not result[0]:
        result.pop(0)
    while result and not result[-1]:
        result.pop()
    
    # Remove junk characters at end
    if result and result[-1]:
        result[-1] = result[-1].rstrip(' \uFFFD')
    
    return '\n'.join(result)


def _fetch_lyrics_from_page(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if resp.status_code != 200:
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
            for br in block.find_all("br"):
                br.replace_with("\n")
            text = block.get_text(separator="\n").strip()
            if text:
                parts.append(text)
        lyrics = "\n\n".join(parts).strip()
        if lyrics:
            lyrics = _clean_lyrics_text(lyrics)
        return lyrics or None
    except Exception as exc:
        logging.error("Genius page parse error: %s", exc)
        return None


def get_lyrics(title: str, artist: str) -> Optional[str]:
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
        url  = song.get("url")
        text = _fetch_lyrics_from_page(url)
        if text:
            cache_set(cache_key, text)
            return text
        cache_set(cache_key, CACHE_NONE)
        return None
    except Exception as exc:
        logging.error("Genius lyrics error for '%s': %s", query, exc)
        cache_set(cache_key, CACHE_NONE)
        return None
