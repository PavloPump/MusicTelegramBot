import csv
import html
import io
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .cache import get_cached_settings, invalidate_cached_settings, set_cached_settings
from .config import DB_PATH, PER_PAGE


def db_connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER,
            track_id TEXT,
            track_title TEXT,
            artist_name TEXT,
            added_date TEXT,
            PRIMARY KEY (user_id, track_id)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            download_quality TEXT DEFAULT 'mp3',
            auto_download BOOLEAN DEFAULT 0,
            show_covers BOOLEAN DEFAULT 1,
            items_per_page INTEGER DEFAULT 8,
            reply_menu BOOLEAN DEFAULT 1,
            search_limit INTEGER DEFAULT 50,
            show_tips BOOLEAN DEFAULT 1,
            show_video_shot BOOLEAN DEFAULT 1,
            show_lyrics_button BOOLEAN DEFAULT 1
        )
        """
    )

    cursor.execute("PRAGMA table_info(user_settings)")
    _cols = {row[1] for row in cursor.fetchall()}
    if "items_per_page" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN items_per_page INTEGER DEFAULT 8")
    if "reply_menu" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN reply_menu BOOLEAN DEFAULT 1")
    if "search_limit" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN search_limit INTEGER DEFAULT 50")
    if "show_tips" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN show_tips BOOLEAN DEFAULT 1")
    if "show_video_shot" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN show_video_shot BOOLEAN DEFAULT 1")
    if "show_lyrics_button" not in _cols:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN show_lyrics_button BOOLEAN DEFAULT 1")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            created_at TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_history_user_time ON search_history(user_id, created_at DESC)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS play_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            track_id TEXT,
            track_title TEXT,
            artist_name TEXT,
            created_at TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_play_history_user_time ON play_history(user_id, created_at DESC)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_last_track (
            user_id INTEGER PRIMARY KEY,
            track_id TEXT,
            updated_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            updated_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tg_audio_cache (
            track_id TEXT,
            codec TEXT,
            file_id TEXT,
            track_title TEXT,
            artist_name TEXT,
            updated_at TEXT,
            PRIMARY KEY (track_id, codec)
        )
        """
    )

    try:
        cursor.execute("PRAGMA table_info(tg_audio_cache)")
        _ac_cols = {row[1] for row in cursor.fetchall()}
        if "track_title" not in _ac_cols:
            cursor.execute("ALTER TABLE tg_audio_cache ADD COLUMN track_title TEXT")
        if "artist_name" not in _ac_cols:
            cursor.execute("ALTER TABLE tg_audio_cache ADD COLUMN artist_name TEXT")
    except Exception:
        pass

    conn.commit()
    conn.close()


def upsert_tg_user(user) -> None:
    if not user:
        return
    uid = getattr(user, "id", None)
    if uid is None:
        return

    username = getattr(user, "username", None) or ""
    first_name = getattr(user, "first_name", None) or ""
    last_name = getattr(user, "last_name", None) or ""

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO tg_users (user_id, username, first_name, last_name, updated_at) VALUES (?, ?, ?, ?, ?)",
        (int(uid), username, first_name, last_name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def log_search_query(user_id: int, query: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO search_history (user_id, query, created_at) VALUES (?, ?, ?)",
        (user_id, query, datetime.now().isoformat()),
    )
    cur.execute(
        """
        DELETE FROM search_history
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id
              FROM search_history
              WHERE user_id = ?
              ORDER BY created_at DESC
              LIMIT 50
          )
        """,
        (user_id, user_id),
    )
    conn.commit()
    conn.close()


def log_play_event(user_id: int, track_id: str, track_title: str, artist_name: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO play_history (user_id, track_id, track_title, artist_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, str(track_id), track_title, artist_name, datetime.now().isoformat()),
    )
    cur.execute(
        "INSERT OR REPLACE INTO user_last_track (user_id, track_id, updated_at) VALUES (?, ?, ?)",
        (user_id, str(track_id), datetime.now().isoformat()),
    )
    cur.execute(
        """
        DELETE FROM play_history
        WHERE user_id = ?
          AND id NOT IN (
              SELECT id
              FROM play_history
              WHERE user_id = ?
              ORDER BY created_at DESC
              LIMIT 200
          )
        """,
        (user_id, user_id),
    )
    conn.commit()
    conn.close()


def get_search_history(user_id: int, limit: int = 50) -> List[Tuple[str, str]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT query, created_at FROM search_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_last_track_id(user_id: int) -> Optional[str]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT track_id FROM user_last_track WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_user_stats(user_id: int) -> Dict[str, Optional[str]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,))
    fav_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM search_history WHERE user_id = ?", (user_id,))
    search_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ?", (user_id,))
    play_count = cur.fetchone()[0]
    cur.execute("SELECT track_title, artist_name FROM play_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
    last_row = cur.fetchone()
    conn.close()

    last_track = None
    if last_row and (last_row[0] or last_row[1]):
        title = (last_row[0] or "").strip()
        artist = (last_row[1] or "").strip()
        last_track = f"{title} — {artist}".strip(" —")

    return {
        "favorites": fav_count,
        "searches": search_count,
        "plays": play_count,
        "last_track": last_track,
    }


def get_user_settings(user_id: int) -> Dict[str, object]:
    cached = get_cached_settings(user_id)
    if cached is not None:
        return cached

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT download_quality, auto_download, show_covers, items_per_page, reply_menu, search_limit, show_tips, show_video_shot, show_lyrics_button
        FROM user_settings WHERE user_id = ?
        """,
        (user_id,),
    )
    settings = cursor.fetchone()
    conn.close()

    if settings:
        result = {
            "download_quality": settings[0],
            "auto_download": bool(settings[1]),
            "show_covers": bool(settings[2]),
            "items_per_page": int(settings[3]),
            "reply_menu": bool(settings[4]) if settings[4] is not None else True,
            "search_limit": int(settings[5]),
            "show_tips": bool(settings[6]) if settings[6] is not None else True,
            "show_video_shot": bool(settings[7]) if len(settings) > 7 and settings[7] is not None else True,
            "show_lyrics_button": bool(settings[8]) if len(settings) > 8 and settings[8] is not None else True,
        }
        set_cached_settings(user_id, result)
        return result

    result = {
        "download_quality": "mp3",
        "auto_download": False,
        "show_covers": True,
        "items_per_page": PER_PAGE,
        "reply_menu": True,
        "search_limit": 50,
        "show_tips": True,
        "show_video_shot": True,
        "show_lyrics_button": True,
    }
    set_cached_settings(user_id, result)
    return result


def update_user_settings(user_id: int, settings: Dict[str, object]) -> None:
    invalidate_cached_settings(user_id)
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO user_settings
        (user_id, download_quality, auto_download, show_covers, items_per_page, reply_menu, search_limit, show_tips, show_video_shot, show_lyrics_button)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            settings.get("download_quality", "mp3"),
            int(settings.get("auto_download", False)),
            int(settings.get("show_covers", True)),
            int(settings.get("items_per_page", 8)),
            int(bool(settings.get("reply_menu", True))),
            int(settings.get("search_limit", 50)),
            int(bool(settings.get("show_tips", True))),
            int(bool(settings.get("show_video_shot", True))),
            int(bool(settings.get("show_lyrics_button", True))),
        ),
    )
    conn.commit()
    conn.close()


def add_to_favorites(user_id: int, track_id: str, track_title: str, artist_name: str) -> bool:
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO favorites (user_id, track_id, track_title, artist_name, added_date)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, track_id, track_title, artist_name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return True


def remove_from_favorites(user_id: int, track_id: str) -> bool:
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM favorites WHERE user_id = ? AND track_id = ?", (user_id, track_id))
    conn.commit()
    conn.close()
    return True


def get_user_favorites(user_id: int) -> List[Tuple[str, str, str, str]]:
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT track_id, track_title, artist_name, added_date FROM favorites WHERE user_id = ? ORDER BY added_date DESC",
        (user_id,),
    )
    favorites = cursor.fetchall()
    conn.close()
    return favorites


def is_favorite(user_id: int, track_id: str) -> bool:
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM favorites WHERE user_id = ? AND track_id = ? LIMIT 1", (user_id, track_id))
    row = cursor.fetchone()
    conn.close()
    return bool(row)


def set_tg_audio_file_id(track_id: str, codec: str, file_id: str, track_title: Optional[str], artist_name: Optional[str]) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO tg_audio_cache (track_id, codec, file_id, track_title, artist_name, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (track_id, codec, file_id, track_title or "", artist_name or "", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_tg_audio_cache(track_id: str, codec: str) -> Optional[Dict[str, str]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT file_id, track_title, artist_name FROM tg_audio_cache WHERE track_id = ? AND codec = ? LIMIT 1",
        (track_id, codec),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    file_id, track_title, artist_name = row
    file_id = (file_id or "").strip()
    if not file_id:
        return None
    return {
        "file_id": file_id,
        "track_title": (track_title or "").strip(),
        "artist_name": (artist_name or "").strip(),
    }


def delete_tg_audio_file_id(track_id: str, codec: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM tg_audio_cache WHERE track_id = ? AND codec = ?", (track_id, codec))
    conn.commit()
    conn.close()


def admin_user_label_html(user_id: int) -> str:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT username, first_name, last_name FROM tg_users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    label = str(user_id)
    if row:
        username, first_name, last_name = row
        username = (username or "").strip()
        first_name = (first_name or "").strip()
        last_name = (last_name or "").strip()
        if username:
            label = f"@{username}"
        else:
            name = (first_name + " " + last_name).strip()
            if name:
                label = name
    safe_label = html.escape(label)
    return f'<a href="tg://user?id={user_id}">{safe_label}</a> (<code>{user_id}</code>)'


def admin_db_stats_text() -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM user_settings")
    users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM favorites")
    favs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM search_history")
    searches = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM play_history")
    plays = cur.fetchone()[0]
    conn.close()
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return (
        "📊 Статистика БД:\n"
        f"Пользователей: {users}\n"
        f"Избранных треков: {favs}\n"
        f"Запросов поиска: {searches}\n"
        f"Прослушиваний: {plays}\n"
        f"Размер DB: {size}B"
    )


def admin_top_users_text(limit: int = 10) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, COUNT(*) c FROM play_history GROUP BY user_id ORDER BY c DESC LIMIT ?", (int(limit),))
    top_plays = cur.fetchall()
    cur.execute("SELECT user_id, COUNT(*) c FROM favorites GROUP BY user_id ORDER BY c DESC LIMIT ?", (int(limit),))
    top_favs = cur.fetchall()
    conn.close()

    lines = ["🧾 Топ пользователей:", "", "По прослушиваниям:"]
    if top_plays:
        for uid, c in top_plays:
            lines.append(f"• {admin_user_label_html(uid)}: {c}")
    else:
        lines.append("• -")

    lines.append("")
    lines.append("По избранному:")
    if top_favs:
        for uid, c in top_favs:
            lines.append(f"• {admin_user_label_html(uid)}: {c}")
    else:
        lines.append("• -")
    return "\n".join(lines)


def admin_recent_text(limit: int = 10) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, query, created_at FROM search_history ORDER BY created_at DESC LIMIT ?", (int(limit),))
    searches = cur.fetchall()
    cur.execute("SELECT user_id, track_title, artist_name, created_at FROM play_history ORDER BY created_at DESC LIMIT ?", (int(limit),))
    plays = cur.fetchall()
    conn.close()

    lines = ["🕘 Последние события:", "", "Поиски:"]
    if searches:
        for uid, q, dt in searches:
            lines.append(f"• {admin_user_label_html(uid)}: {q} ({dt})")
    else:
        lines.append("• -")

    lines.append("")
    lines.append("Прослушивания:")
    if plays:
        for uid, title, artist, dt in plays:
            t = (title or "").strip()
            a = (artist or "").strip()
            name = f"{t} — {a}".strip(" —")
            lines.append(f"• {admin_user_label_html(uid)}: {name} ({dt})")
    else:
        lines.append("• -")
    return "\n".join(lines)


def admin_user_overview_text(user_id: int, settings: Dict[str, object]) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,))
    favs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM search_history WHERE user_id = ?", (user_id,))
    searches = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ?", (user_id,))
    plays = cur.fetchone()[0]
    cur.execute("SELECT track_title, artist_name FROM play_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
    last_row = cur.fetchone()
    conn.close()

    last_track_name = "-"
    if last_row and (last_row[0] or last_row[1]):
        t = (last_row[0] or "").strip()
        a = (last_row[1] or "").strip()
        last_track_name = f"{t} — {a}".strip(" —")

    return (
        f"👤 Пользователь: {admin_user_label_html(user_id)}\n\n"
        f"Избранное: {favs}\n"
        f"Поиски: {searches}\n"
        f"Прослушивания: {plays}\n"
        f"Последний трек: {last_track_name}\n\n"
        f"Качество: <code>{settings.get('download_quality', 'mp3')}</code>\n"
        f"Reply-меню: <code>{'ON' if settings.get('reply_menu', True) else 'OFF'}</code>\n"
        f"Автоскачивание: <code>{'ON' if settings.get('auto_download') else 'OFF'}</code>\n"
        f"Обложки: <code>{'ON' if settings.get('show_covers') else 'OFF'}</code>\n"
        f"Подсказки: <code>{'ON' if settings.get('show_tips', True) else 'OFF'}</code>\n"
        f"На странице: <code>{int(settings.get('items_per_page', 8))}</code>\n"
        f"Лимит поиска: <code>{int(settings.get('search_limit', 50))}</code>"
    )


def admin_user_recent_text(user_id: int, limit: int = 10) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT query, created_at FROM search_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, int(limit)))
    searches = cur.fetchall()
    cur.execute(
        "SELECT track_title, artist_name, created_at FROM play_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, int(limit)),
    )
    plays = cur.fetchall()
    cur.execute(
        "SELECT track_title, artist_name, added_date FROM favorites WHERE user_id = ? ORDER BY added_date DESC LIMIT ?",
        (user_id, int(limit)),
    )
    favs = cur.fetchall()
    conn.close()

    lines = [f"🕘 Последние действия пользователя {admin_user_label_html(user_id)}:", "", "Поиски:"]
    if searches:
        for q, dt in searches:
            lines.append(f"• {q} ({dt})")
    else:
        lines.append("• -")

    lines.append("")
    lines.append("Прослушивания:")
    if plays:
        for title, artist, dt in plays:
            t = (title or "").strip()
            a = (artist or "").strip()
            name = f"{t} — {a}".strip(" —")
            lines.append(f"• {name} ({dt})")
    else:
        lines.append("• -")

    lines.append("")
    lines.append("Избранное:")
    if favs:
        for title, artist, dt in favs:
            t = (title or "").strip()
            a = (artist or "").strip()
            name = f"{t} — {a}".strip(" —")
            lines.append(f"• {name} ({dt})")
    else:
        lines.append("• -")
    return "\n".join(lines)


def admin_get_user_data_for_export(user_id: int) -> Tuple[List[Tuple], List[Tuple], List[Tuple]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id, track_title, artist_name, added_date FROM favorites WHERE user_id = ? ORDER BY added_date DESC",
        (user_id,),
    )
    fav_rows = cur.fetchall()
    cur.execute(
        "SELECT query, created_at FROM search_history WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    search_rows = cur.fetchall()
    cur.execute(
        "SELECT track_id, track_title, artist_name, created_at FROM play_history WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    play_rows = cur.fetchall()
    conn.close()
    return fav_rows, search_rows, play_rows


def admin_backup_db_path() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(os.path.dirname(DB_PATH), f"bot_data_backup_{ts}.db")
    shutil.copy2(DB_PATH, dst)
    return dst


def admin_clear_user_data(user_id: int, what: str) -> None:
    conn = db_connect()
    cur = conn.cursor()
    if what == "fav":
        cur.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))
    elif what == "hist":
        cur.execute("DELETE FROM search_history WHERE user_id = ?", (user_id,))
    elif what == "plays":
        cur.execute("DELETE FROM play_history WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM user_last_track WHERE user_id = ?", (user_id,))
    else:
        conn.close()
        return
    conn.commit()
    conn.close()


def admin_send_csv(bot, chat_id: int, filename: str, header: Sequence[str], rows: Sequence[Sequence]) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    data = buf.getvalue().encode("utf-8")
    file = io.BytesIO(data)
    file.name = filename
    bot.send_document(chat_id, file)
