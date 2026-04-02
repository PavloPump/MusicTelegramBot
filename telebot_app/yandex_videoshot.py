import logging
import tempfile
from typing import Optional

import requests

from .config import YANDEX_MUSIC_TOKEN

_client = None


def _get_client():
    global _client
    if not YANDEX_MUSIC_TOKEN:
        return None
    if _client is None:
        try:
            import yandex_music
            _client = yandex_music.Client(YANDEX_MUSIC_TOKEN).init()
        except Exception as exc:
            logging.warning("Yandex Music client init failed: %s", exc)
            return None
    return _client


def get_videoshot_path(title: str, artist: str) -> Optional[str]:
    """Try to find and download a videoshot from Yandex Music for a track."""
    client = _get_client()
    if not client:
        return None

    try:
        query = f"{artist} {title}".strip()
        if not query:
            return None

        search_result = client.search(query, type_="all")
        if not search_result or not search_result.tracks or not search_result.tracks.results:
            return None

        track = search_result.tracks.results[0]

        video_url = None
        video_shots = getattr(track, "video_shots", None)
        background_video_uri = getattr(track, "background_video_uri", None)

        if isinstance(video_shots, (list, tuple)) and video_shots:
            first = video_shots[0]
            if isinstance(first, dict):
                video_url = first.get("url") or first.get("path")
            elif hasattr(first, "download_url"):
                video_url = first.download_url
            elif hasattr(first, "url"):
                video_url = first.url

        if not video_url:
            video_url = background_video_uri

        if not video_url:
            return None

        resp = requests.get(video_url, stream=True, timeout=15)
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in resp.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp.close()
        return tmp.name

    except Exception as exc:
        logging.warning("Yandex videoshot error for '%s - %s': %s", artist, title, exc)
        return None
