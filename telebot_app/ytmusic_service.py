import logging
import re
from typing import List, Optional, Dict, Any
from ytmusicapi import YTMusic

from .cache import CACHE_NONE, cache_get, cache_set


class Track:
    def __init__(self, data: dict):
        self.id             = data.get('videoId', '')
        self.title          = data.get('title', 'Без названия')
        self.artists        = data.get('artists', [])
        self.album_name     = ''
        self.cover_uri      = None
        self.duration_ms    = 0
        self.duration_seconds = data.get('duration_seconds', 0)
        self.preview_url    = None
        self.external_url   = f"https://music.youtube.com/watch?v={self.id}" if self.id else ""
        self.chart_position = data.get('chart_position', '')

        album = data.get('album')
        if album:
            self.album_name = album.get('name', '') if isinstance(album, dict) else str(album)

        thumbnails = data.get('thumbnails', [])
        if thumbnails and isinstance(thumbnails, list):
            url = thumbnails[-1].get('url', '')
            # Upgrade to high quality (544x544)
            if '=w' in url:
                url = re.sub(r'=w\d+-h\d+', '=w544-h544', url)
            elif 'maxresdefault' not in url and 'hqdefault' not in url:
                # For lh3.googleusercontent.com URLs
                url = re.sub(r'=s\d+', '=s544', url) if '=s' in url else url
            self.cover_uri = url

        if self.duration_seconds:
            self.duration_ms = self.duration_seconds * 1000

    def artist_name(self) -> str:
        if self.artists:
            a = self.artists[0]
            return a.get('name', '') if isinstance(a, dict) else str(a)
        return ''


_ytmusic: Optional[YTMusic] = None


def _get_ytmusic() -> YTMusic:
    global _ytmusic
    if _ytmusic is None:
        _ytmusic = YTMusic()
        logging.info("YouTube Music API initialized successfully")
    return _ytmusic


def search_tracks(query: str, limit: int = 20) -> Optional[List[Track]]:
    if not query or len(query.strip()) < 2:
        return None

    key = f"ytm_search:{query.strip().lower()}:{limit}"
    cached = cache_get(key, 300)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        results = _get_ytmusic().search(query, filter='songs', limit=limit)
        if not results:
            cache_set(key, CACHE_NONE)
            return None

        tracks = []
        for item in results:
            try:
                t = Track(item)
                if t.id:
                    tracks.append(t)
            except Exception as e:
                logging.warning(f"parse track error: {e}")

        if tracks:
            cache_set(key, tracks)
            return tracks

        cache_set(key, CACHE_NONE)
        return None
    except Exception as e:
        logging.error(f"YouTube Music search error: {e}")
        return None


def get_track_by_id(track_id: str) -> Optional[Track]:
    if not track_id:
        return None
    key = f"ytm_track:{track_id}"
    cached = cache_get(key, 600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        song = _get_ytmusic().get_song(track_id)
        if not song or 'videoDetails' not in song:
            cache_set(key, CACHE_NONE)
            return None
        vd = song['videoDetails']
        t = Track({
            'videoId': track_id,
            'title':   vd.get('title', 'Без названия'),
            'artists': [{'name': vd.get('author', 'Unknown')}],
            'thumbnails': vd.get('thumbnail', {}).get('thumbnails', []),
            'duration_seconds': int(vd.get('lengthSeconds', 0)),
        })
        cache_set(key, t)
        return t
    except Exception as e:
        logging.error(f"get_track_by_id {track_id}: {e}")
        cache_set(key, CACHE_NONE)
        return None


def get_tracks_by_ids(track_ids: List[str]) -> List[Track]:
    tracks = []
    for tid in track_ids:
        t = get_track_by_id(tid)
        if t:
            tracks.append(t)
    return tracks


def get_artist_top_tracks(artist_name: str, limit: int = 10) -> Optional[List[Track]]:
    if not artist_name:
        return None
    key = f"ytm_artist:{artist_name.lower()}:{limit}"
    cached = cache_get(key, 600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        yt = _get_ytmusic()
        sr = yt.search(artist_name, filter='artists', limit=1)
        if not sr:
            cache_set(key, CACHE_NONE)
            return None
        browse_id = sr[0].get('browseId')
        if not browse_id:
            return search_tracks(artist_name, limit=limit)

        info = yt.get_artist(browse_id)
        if 'songs' in info and 'results' in info['songs']:
            tracks = []
            for s in info['songs']['results'][:limit]:
                try:
                    t = Track(s)
                    if t.id:
                        tracks.append(t)
                except Exception:
                    pass
            if tracks:
                cache_set(key, tracks)
                return tracks

        return search_tracks(artist_name, limit=limit)
    except Exception as e:
        logging.error(f"get_artist_top_tracks: {e}")
        return search_tracks(artist_name, limit=limit)


def get_artist_info(artist_name: str) -> Optional[Dict[str, Any]]:
    if not artist_name:
        return None
    key = f"ytm_artist_info:{artist_name.lower()}"
    cached = cache_get(key, 3600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        yt = _get_ytmusic()
        sr = yt.search(artist_name, filter='artists', limit=1)
        if not sr:
            cache_set(key, CACHE_NONE)
            return None
        browse_id = sr[0].get('browseId')
        if not browse_id:
            cache_set(key, CACHE_NONE)
            return None
        ai = yt.get_artist(browse_id)
        info = {
            'name': ai.get('name', artist_name),
            'description': ai.get('description', ''),
            'subscribers': ai.get('subscribers', ''),
            'thumbnails': ai.get('thumbnails', []),
            'browse_id': browse_id,
        }
        cache_set(key, info)
        return info
    except Exception as e:
        logging.error(f"get_artist_info: {e}")
        cache_set(key, CACHE_NONE)
        return None


def get_album_tracks(album_id: str) -> Optional[List[Track]]:
    if not album_id:
        return None
    key = f"ytm_album:{album_id}"
    cached = cache_get(key, 3600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        album = _get_ytmusic().get_album(album_id)
        if not album or 'tracks' not in album:
            cache_set(key, CACHE_NONE)
            return None
        tracks = []
        for item in album['tracks']:
            try:
                t = Track(item)
                if t.id:
                    tracks.append(t)
            except Exception:
                pass
        if tracks:
            cache_set(key, tracks)
            return tracks
        cache_set(key, CACHE_NONE)
        return None
    except Exception as e:
        logging.error(f"get_album_tracks: {e}")
        cache_set(key, CACHE_NONE)
        return None


def get_album_info(album_id: str) -> Optional[dict]:
    """Get album information including metadata and tracks."""
    if not album_id:
        return None
    key = f"ytm_album_info:{album_id}"
    cached = cache_get(key, 3600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        album = _get_ytmusic().get_album(album_id)
        if not album:
            cache_set(key, CACHE_NONE)
            return None
        
        # Extract album metadata
        info = {
            'title': album.get('title', ''),
            'year': album.get('year'),
            'type': album.get('type'),
            'artists': album.get('artists', []),
            'thumbnails': album.get('thumbnails', []),
        }
        
        # Extract tracks
        tracks = []
        if 'tracks' in album:
            for item in album['tracks']:
                try:
                    t = Track(item)
                    if t.id:
                        tracks.append(t)
                except Exception:
                    pass
        
        info['tracks'] = tracks
        
        cache_set(key, info)
        return info
    except Exception as e:
        logging.error(f"get_album_info: {e}")
        cache_set(key, CACHE_NONE)
        return None
