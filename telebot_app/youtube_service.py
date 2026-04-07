import logging
import os
import shutil
import tempfile
from typing import Optional, Tuple

import yt_dlp

from .cache import CACHE_NONE, cache_get, cache_set

_COMMON_OPTS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 30,
    "retries": 3,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    },
}


def _build_search_query(title: str, artist: str) -> str:
    parts = []
    if artist:
        parts.append(artist.strip())
    if title:
        parts.append(title.strip())
    return " - ".join(parts) if parts else ""


def _find_output_file(directory: str, prefer_ext: str = "") -> Optional[str]:
    """Find the first non-empty file in directory, preferring given extension."""
    if not os.path.isdir(directory):
        return None
    files = []
    for fname in os.listdir(directory):
        fpath = os.path.join(directory, fname)
        if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
            files.append(fpath)
    if not files:
        return None
    if prefer_ext:
        for f in files:
            if f.endswith(f".{prefer_ext}"):
                return f
    return files[0]


def _move_to_tempfile(src: str, ext: str) -> Optional[str]:
    """Move file to a proper temp location and return path."""
    if not ext.startswith("."):
        ext = f".{ext}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    dst = tmp.name
    tmp.close()
    try:
        shutil.move(src, dst)
        return dst
    except Exception:
        if os.path.exists(dst):
            os.remove(dst)
        return src


def download_audio(title: str, artist: str, codec: str = "mp3") -> Optional[str]:
    """Download audio from YouTube Music by searching for artist - title."""
    # Try multiple search query variations
    queries = []
    
    # Original order: artist - title
    query1 = _build_search_query(title, artist)
    if query1:
        queries.append(query1)
    
    # Reversed order: title - artist
    query2 = _build_search_query(artist, title)
    if query2 and query2 != query1:
        queries.append(query2)
    
    # Just title
    if title and title.strip():
        queries.append(title.strip())
    
    # Just artist
    if artist and artist.strip():
        queries.append(artist.strip())
    
    if not queries:
        return None

    tmpdir = tempfile.mkdtemp(prefix="ytdl_audio_")
    filename_format = "%(title)s.%(ext)s"
    output_template = os.path.join(tmpdir, filename_format)

    try:
        # Use the same approach as telego-yt-dlp
        ydl_opts = {
            **_COMMON_OPTS,
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                }
            ],
            "outtmpl": output_template,
            "default_search": "ytsearch1",
        }

        # Try each query until we find a working one
        for i, query in enumerate(queries):
            try:
                logging.info(f"Trying audio download with query {i+1}/{len(queries)}: {query}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([f"ytsearch1:{query}"])
                
                # If we got here, download was successful
                break
            except Exception as e:
                logging.warning(f"Query {i+1} failed: {e}")
                if i == len(queries) - 1:  # Last query
                    raise
                continue

        # Find the .mp3 file as done in telego-yt-dlp
        files = os.listdir(tmpdir)
        filename = None
        for file in files:
            if os.path.isfile(os.path.join(tmpdir, file)) and file.endswith(".mp3"):
                filename = file
                break

        if not filename:
            logging.warning("No .mp3 file found in %s for query: %s", tmpdir, query)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        source_path = os.path.join(tmpdir, filename)
        final = _move_to_tempfile(source_path, ".mp3")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return final

    except Exception as exc:
        logging.error("YouTube audio download error for '%s': %s", query, exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def download_video_clip(title: str, artist: str) -> Optional[str]:
    """Download a music video clip from YouTube by searching for artist - title official music video."""
    # Try multiple search query variations
    queries = []
    
    # Original order: artist - title
    query1 = _build_search_query(title, artist)
    if query1:
        queries.append(query1)
    
    # Reversed order: title - artist
    query2 = _build_search_query(artist, title)
    if query2 and query2 != query1:
        queries.append(query2)
    
    # Just title
    if title and title.strip():
        queries.append(title.strip())
    
    if not queries:
        return None

    tmpdir = tempfile.mkdtemp(prefix="ytdl_video_")
    filename_format = "%(title)s.%(ext)s"
    output_template = os.path.join(tmpdir, filename_format)

    try:
        ydl_opts = {
            **_COMMON_OPTS,
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "default_search": "ytsearch1",
        }

        # Try each query until we find a working one
        for i, query in enumerate(queries):
            query_with_mv = f"{query} official music video"
            try:
                logging.info(f"Trying video download with query {i+1}/{len(queries)}: {query_with_mv}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([f"ytsearch1:{query_with_mv}"])
                
                # If we got here, download was successful
                break
            except Exception as e:
                logging.warning(f"Video query {i+1} failed: {e}")
                if i == len(queries) - 1:  # Last query
                    raise
                continue

        # Find the video file
        files = os.listdir(tmpdir)
        filename = None
        for file in files:
            if os.path.isfile(os.path.join(tmpdir, file)):
                if file.endswith(".mp4") or file.endswith(".webm") or file.endswith(".mkv"):
                    filename = file
                    break

        if not filename:
            logging.warning("No video file found in %s for query: %s", tmpdir, query_with_mv)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        source_path = os.path.join(tmpdir, filename)
        final = _move_to_tempfile(source_path, ".mp4")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return final

    except Exception as exc:
        logging.error("YouTube video download error for '%s': %s", query_with_mv, exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def check_video_clip_exists(title: str, artist: str) -> bool:
    """Check if a music video clip exists for the given track."""
    query = _build_search_query(title, artist)
    if not query:
        return False

    query_with_mv = f"{query} official music video"
    cache_key = f"yt_clip_exists:{query.lower()}"
    cached = cache_get(cache_key, 300)
    if cached is not None:
        return cached

    try:
        ydl_opts = {
            **_COMMON_OPTS,
            "default_search": "ytsearch1",
            "extract_flat": False,
            "skip_download": True,
            "quiet": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query_with_mv}", download=False)
            if info and "entries" in info:
                entries = info["entries"]
                if entries and len(entries) > 0:
                    video = entries[0]
                    # Check if it's actually a music video (not just audio)
                    duration = video.get("duration", 0)
                    if duration > 30:  # Longer than 30 seconds likely has video
                        cache_set(cache_key, True)
                        return True
            elif info:
                duration = info.get("duration", 0)
                if duration > 30:
                    cache_set(cache_key, True)
                    return True

        cache_set(cache_key, False)
        return False
    except Exception as exc:
        logging.error("YouTube clip check error: %s", exc)
        cache_set(cache_key, False)
        return False


def get_youtube_video_url(title: str, artist: str) -> Optional[str]:
    """Get YouTube video URL without downloading (for info/sharing)."""
    query = _build_search_query(title, artist)
    if not query:
        return None

    cache_key = f"yt_url:{query.lower()}"
    cached = cache_get(cache_key, 600)
    if cached is not None:
        return None if cached is CACHE_NONE else cached

    try:
        ydl_opts = {
            **_COMMON_OPTS,
            "default_search": "ytsearch1",
            "extract_flat": False,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if info and "entries" in info:
                entries = info["entries"]
                if entries:
                    url = entries[0].get("webpage_url") or entries[0].get("url")
                    if url:
                        cache_set(cache_key, url)
                        return url
            elif info:
                url = info.get("webpage_url") or info.get("url")
                if url:
                    cache_set(cache_key, url)
                    return url

        cache_set(cache_key, CACHE_NONE)
        return None
    except Exception as exc:
        logging.error("YouTube URL lookup error: %s", exc)
        cache_set(cache_key, CACHE_NONE)
        return None
