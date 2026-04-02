import logging
import time
from typing import Dict, List, Optional

import requests

from .cache import CACHE_NONE, cache_get, cache_set
from .config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

_token: Optional[str] = None
_token_expires: float = 0.0


class SpotifyTrack:
    """Unified track object compatible with the rest of the bot."""

    def __init__(self, data: dict):
        self.id: str = str(data.get("id", ""))
        self.title: str = data.get("name") or data.get("title") or "Без названия"
        self.artists: List["_Artist"] = []
        self.album_name: str = ""
        self.cover_uri: Optional[str] = None
        self.duration_ms: int = data.get("duration_ms", 0)
        self.spotify_uri: str = data.get("uri", "")
        self.preview_url: Optional[str] = data.get("preview_url")
        self.external_url: str = ""
        self.chart_position: str = data.get("chart_position", "")

        artists_raw = data.get("artists") or []
        for a in artists_raw:
            self.artists.append(_Artist(a.get("name", ""), a.get("id", "")))

        album = data.get("album") or {}
        self.album_name = album.get("name", "")
        images = album.get("images") or []
        if images:
            self.cover_uri = images[0].get("url")

        ext = data.get("external_urls") or {}
        self.external_url = ext.get("spotify", "")

    def artist_name(self) -> str:
        if self.artists:
            return self.artists[0].name
        return ""


class _Artist:
    def __init__(self, name: str, artist_id: str = ""):
        self.name = name
        self.id = artist_id


def _get_token() -> Optional[str]:
    global _token, _token_expires
    if _token and time.time() < _token_expires - 60:
        return _token
    try:
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=10,
        )
        if resp.status_code != 200:
            logging.error("Spotify token error %s: %s", resp.status_code, resp.text[:300])
            return None
        body = resp.json()
        _token = body.get("access_token")
        _token_expires = time.time() + body.get("expires_in", 3600)
        return _token
    except Exception as exc:
        logging.error("Spotify token request failed: %s", exc)
        return None


def _headers() -> dict:
    token = _get_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


def search_tracks(query: str, limit: int = 50) -> Optional[List[SpotifyTrack]]:
    if not query or len(query.strip()) < 2:
        return None

    normalized = query.strip().lower()
    cache_key = f"sp_search:{normalized}"
    cached = cache_get(cache_key, 120)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None
        limit_val = min(limit, 10)
        logging.info(f"Spotify search: query='{query}', limit={limit_val}")
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": limit_val},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning("Spotify search failed %s: %s", resp.status_code, resp.text[:200])
            cache_set(cache_key, CACHE_NONE)
            return None

        data = resp.json()
        items = data.get("tracks", {}).get("items") or []
        tracks = [SpotifyTrack(item) for item in items if item.get("id")]
        if not tracks:
            cache_set(cache_key, CACHE_NONE)
            return None
        cache_set(cache_key, tracks)
        return tracks
    except Exception as exc:
        logging.error("Spotify search error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_track_by_id(track_id: str) -> Optional[SpotifyTrack]:
    if not track_id:
        return None

    cache_key = f"sp_track:{track_id}"
    cached = cache_get(cache_key, 600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning("Spotify get track failed %s", resp.status_code)
            cache_set(cache_key, CACHE_NONE)
            return None
        track = SpotifyTrack(resp.json())
        cache_set(cache_key, track)
        return track
    except Exception as exc:
        logging.error("Spotify get track error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_tracks_by_ids(track_ids: list) -> List[SpotifyTrack]:
    if not track_ids:
        return []

    result = []
    missing_ids = []
    for tid in track_ids:
        ck = f"sp_track:{tid}"
        cached = cache_get(ck, 600)
        if cached is not None and cached is not CACHE_NONE:
            result.append(cached)
        else:
            missing_ids.append(tid)

    if missing_ids:
        try:
            hdrs = _headers()
            if hdrs:
                for i in range(0, len(missing_ids), 50):
                    batch = missing_ids[i:i + 50]
                    resp = requests.get(
                        "https://api.spotify.com/v1/tracks",
                        params={"ids": ",".join(batch), "market": "RU"},
                        headers=hdrs,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        tracks_data = resp.json().get("tracks") or []
                        for td in tracks_data:
                            if td and td.get("id"):
                                t = SpotifyTrack(td)
                                cache_set(f"sp_track:{t.id}", t)
                                result.append(t)
        except Exception as exc:
            logging.error("Spotify batch tracks error: %s", exc)

    order = {str(tid): idx for idx, tid in enumerate(track_ids)}
    result.sort(key=lambda t: order.get(str(t.id), 10**9))
    return result


def get_artist_top_tracks(artist_name: str) -> Optional[List[SpotifyTrack]]:
    if not artist_name or len(artist_name.strip()) < 2:
        return None

    normalized = artist_name.strip().lower()
    cache_key = f"sp_artist:{normalized}"
    cached = cache_get(cache_key, 120)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None

        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": artist_name, "type": "artist", "limit": 1},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        artists = resp.json().get("artists", {}).get("items") or []
        if not artists:
            cache_set(cache_key, CACHE_NONE)
            return None

        artist_id = artists[0].get("id")
        resp2 = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
            params={"market": "RU"},
            headers=hdrs,
            timeout=10,
        )
        if resp2.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        items = resp2.json().get("tracks") or []
        tracks = [SpotifyTrack(item) for item in items if item.get("id")]
        if not tracks:
            cache_set(cache_key, CACHE_NONE)
            return None
        cache_set(cache_key, tracks)
        return tracks
    except Exception as exc:
        logging.error("Spotify artist tracks error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_artist_info(artist_name: str) -> Optional[dict]:
    """Get artist information including photo, top tracks, and albums."""
    if not artist_name or len(artist_name.strip()) < 2:
        return None

    normalized = artist_name.strip().lower()
    cache_key = f"sp_artist_info:{normalized}"
    cached = cache_get(cache_key, 300)  # 5 minutes cache
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None

        # Search for artist
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": artist_name, "type": "artist", "limit": 1},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        artists = resp.json().get("artists", {}).get("items") or []
        if not artists:
            cache_set(cache_key, CACHE_NONE)
            return None

        artist = artists[0]
        artist_id = artist.get("id")
        
        # Get artist details
        artist_info = {
            "id": artist_id,
            "name": artist.get("name"),
            "photo_url": None,
            "followers": artist.get("followers", {}).get("total", 0),
            "popularity": artist.get("popularity", 0),
            "genres": artist.get("genres", []),
        }
        
        # Get photo (largest image)
        images = artist.get("images", [])
        if images:
            artist_info["photo_url"] = images[0].get("url")  # First image is usually largest

        # Get top tracks
        resp2 = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
            params={"market": "RU"},
            headers=hdrs,
            timeout=10,
        )
        if resp2.status_code == 200:
            items = resp2.json().get("tracks") or []
            artist_info["top_tracks"] = [SpotifyTrack(item) for item in items if item.get("id")]
        else:
            artist_info["top_tracks"] = []

        # Get albums
        resp3 = requests.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/albums",
            params={"include_groups": "album,single", "limit": 20, "market": "RU"},
            headers=hdrs,
            timeout=10,
        )
        if resp3.status_code == 200:
            albums_data = resp3.json().get("items", [])
            albums = []
            for album in albums_data:
                album_info = {
                    "id": album.get("id"),
                    "name": album.get("name"),
                    "type": album.get("album_type"),  # album, single, compilation
                    "release_date": album.get("release_date"),
                    "total_tracks": album.get("total_tracks", 0),
                    "cover_url": None,
                }
                
                # Get album cover
                album_images = album.get("images", [])
                if album_images:
                    album_info["cover_url"] = album_images[0].get("url")
                
                albums.append(album_info)
            
            # Sort by release date (newest first)
            albums.sort(key=lambda x: x.get("release_date", ""), reverse=True)
            artist_info["albums"] = albums
        else:
            artist_info["albums"] = []

        cache_set(cache_key, artist_info)
        return artist_info

    except Exception as exc:
        logging.error("Spotify artist info error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_album_tracks(album_id: str) -> Optional[List[SpotifyTrack]]:
    """Get all tracks from an album."""
    if not album_id:
        return None

    cache_key = f"sp_album_tracks:{album_id}"
    cached = cache_get(cache_key, 600)  # 10 minutes cache
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None

        resp = requests.get(
            f"https://api.spotify.com/v1/albums/{album_id}/tracks",
            params={"market": "RU", "limit": 50},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        items = resp.json().get("items", [])
        tracks = [SpotifyTrack(item) for item in items if item.get("id")]
        
        cache_set(cache_key, tracks)
        return tracks

    except Exception as exc:
        logging.error("Spotify album tracks error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_recommendations(track_id: str) -> Optional[List[SpotifyTrack]]:
    if not track_id:
        return None

    cache_key = f"sp_rec:{track_id}"
    cached = cache_get(cache_key, 120)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None
        resp = requests.get(
            "https://api.spotify.com/v1/recommendations",
            params={"seed_tracks": track_id, "limit": 20, "market": "RU"},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        items = resp.json().get("tracks") or []
        tracks = [SpotifyTrack(item) for item in items if item.get("id")]
        if not tracks:
            cache_set(cache_key, CACHE_NONE)
            return None
        cache_set(cache_key, tracks)
        return tracks
    except Exception as exc:
        logging.error("Spotify recommendations error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None


def get_new_releases(limit: int = 50) -> Optional[List[SpotifyTrack]]:
    """Get tracks from new album releases (Spotify charts)."""
    cache_key = "sp_new_releases"
    cached = cache_get(cache_key, 300)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        hdrs = _headers()
        if not hdrs:
            return None

        resp = requests.get(
            "https://api.spotify.com/v1/browse/new-releases",
            params={"limit": 20},
            headers=hdrs,
            timeout=10,
        )
        if resp.status_code != 200:
            cache_set(cache_key, CACHE_NONE)
            return None

        albums = resp.json().get("albums", {}).get("items") or []
        all_tracks = []
        for album in albums[:10]:
            album_id = album.get("id")
            if not album_id:
                continue
            resp2 = requests.get(
                f"https://api.spotify.com/v1/albums/{album_id}/tracks",
                headers=hdrs,
                timeout=10,
            )
            if resp2.status_code == 200:
                tracks = resp2.json().get("items", [])
                for track in tracks[:2]:  # 2 tracks per album
                    track_with_album = track.copy()
                    track_with_album["album"] = album
                    all_tracks.append(SpotifyTrack(track_with_album))

        cache_set(cache_key, all_tracks[:50])
        return all_tracks[:50]
    except Exception as exc:
        logging.error("Spotify new releases error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None
