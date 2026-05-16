"""
Orchestrator module that uses YouTube Music for search/metadata and yt-dlp for downloads.
"""
import logging
from typing import Optional, List
from . import (
    cache,
    database,
    logger,
    ytmusic_service,
    youtube_service,
    genius_service,
)
from .ytmusic_service import Track


def search_music(query: str, limit: int = 20) -> Optional[List[Track]]:
    """Search for music using YouTube Music API."""
    return ytmusic_service.search_tracks(query, limit=limit)


def get_track_info(track_id: str) -> Optional[Track]:
    """Get track info by video ID."""
    if not track_id:
        return None
    return ytmusic_service.get_track_by_id(track_id)


def get_tracks_by_ids(track_ids: list) -> List[Track]:
    """Get tracks by IDs."""
    if not track_ids:
        return []
    return ytmusic_service.get_tracks_by_ids(track_ids)


def get_artist_tracks(artist_name: str) -> Optional[List[Track]]:
    """Get artist's top tracks."""
    return ytmusic_service.get_artist_top_tracks(artist_name)


def get_artist_full_info(artist_name: str) -> Optional[dict]:
    """Get artist information."""
    return ytmusic_service.get_artist_info(artist_name)


def get_album_tracks_from_service(album_id: str) -> Optional[List[Track]]:
    """Get tracks from an album/playlist."""
    return ytmusic_service.get_album_tracks(album_id)


def download_track(track: Track, codec: str = "mp3") -> Optional[str]:
    """Download audio using yt-dlp from YouTube."""
    if not track or not track.id:
        return None
    
    url = f"https://www.youtube.com/watch?v={track.id}"
    return youtube_service.download_audio_by_url(url, codec=codec)


def download_clip(track: Track) -> Optional[str]:
    """Download music video clip via YouTube."""
    if not track or not track.id:
        return None
    
    url = f"https://www.youtube.com/watch?v={track.id}"
    return youtube_service.download_video_by_url(url)


def get_track_lyrics(track: Track) -> Optional[str]:
    """Get lyrics via Genius API (optional)."""
    if not track:
        return None
    title = track.title or ""
    artist = track.artist_name() or ""
    try:
        return genius_service.get_lyrics(title, artist)
    except Exception as e:
        logging.warning(f"Failed to get lyrics: {e}")
        return None


def get_track_lyrics_by_id(track_id: str) -> Optional[str]:
    """Get lyrics by track ID (fetches track info first)."""
    track = get_track_info(track_id)
    if not track:
        return None
    return get_track_lyrics(track)


def as_track(obj):
    """Compatibility wrapper."""
    return obj
