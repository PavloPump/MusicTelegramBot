"""
Orchestrator module that delegates to Spotify (search), YouTube (download),
Yandex Music (videoshot only), and Genius (lyrics).
"""
import logging
from typing import Optional, List
from . import (
    cache,
    database,
    logger,
    spotify_service,
    youtube_service,
    genius_service,
)
import yandex_music
from .spotify_service import SpotifyTrack


def search_music(query: str, limit: int = 50) -> Optional[List[SpotifyTrack]]:
    return spotify_service.search_tracks(query, limit=limit)


def get_track_info(track_id: str) -> Optional[SpotifyTrack]:
    """Get track info, checking local cache first (for Yandex tracks)."""
    if not track_id:
        return None
    cached = cache.cache_get(f"sp_track:{track_id}", 600)
    if cached is not None and cached is not cache.CACHE_NONE:
        return cached
    return spotify_service.get_track_by_id(track_id)


def get_tracks_by_ids(track_ids: list) -> List[SpotifyTrack]:
    """Get tracks by IDs, checking local cache first (for Yandex tracks)."""
    if not track_ids:
        return []
    result = []
    missing_ids = []
    for tid in track_ids:
        cached = cache.cache_get(f"sp_track:{tid}", 600)
        if cached is not None and cached is not cache.CACHE_NONE:
            result.append(cached)
        else:
            missing_ids.append(tid)
    if missing_ids:
        fetched = spotify_service.get_tracks_by_ids(missing_ids)
        result.extend(fetched)
    order = {str(tid): idx for idx, tid in enumerate(track_ids)}
    result.sort(key=lambda t: order.get(str(t.id), 10**9))
    return result


def get_artist_tracks(artist_name: str) -> Optional[List[SpotifyTrack]]:
    return spotify_service.get_artist_top_tracks(artist_name)


def get_artist_full_info(artist_name: str) -> Optional[dict]:
    return spotify_service.get_artist_info(artist_name)


def get_album_tracks_from_service(album_id: str) -> Optional[List[SpotifyTrack]]:
    return spotify_service.get_album_tracks(album_id)


def download_track(track: SpotifyTrack, codec: str = "mp3") -> Optional[str]:
    """Download audio via YouTube Music."""
    title = track.title if track else ""
    artist = track.artist_name() if track else ""
    return youtube_service.download_audio(title, artist, codec=codec)


def download_clip(track: SpotifyTrack) -> Optional[str]:
    """Download music video clip via YouTube."""
    title = track.title if track else ""
    artist = track.artist_name() if track else ""
    return youtube_service.download_video_clip(title, artist)


def get_track_lyrics(track: SpotifyTrack) -> Optional[str]:
    """Get lyrics via Genius API."""
    if not track:
        return None
    title = track.title or ""
    artist = track.artist_name() or ""
    return genius_service.get_lyrics(title, artist)


def get_track_lyrics_by_id(track_id: str) -> Optional[str]:
    """Get lyrics by track ID (fetches track info first)."""
    track = get_track_info(track_id)
    if not track:
        return None
    return get_track_lyrics(track)


def as_track(obj):
    """Compatibility wrapper."""
    return obj
