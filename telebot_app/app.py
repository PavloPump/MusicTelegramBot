import logging
import os
import sqlite3
import tempfile
import time
from functools import wraps
from collections import defaultdict, deque

import requests
import telebot
from telebot import types

from .config import (
    ADMIN_IDS,
    BOT_START_TS,
    BOT_USERNAME,
    DB_PATH,
    PER_PAGE,
    TELEGRAM_TOKEN,
    validate_tokens,
)
from .database import (
    add_to_favorites,
    admin_backup_db_path,
    admin_clear_user_data,
    admin_db_stats_text,
    admin_get_user_data_for_export,
    admin_recent_text,
    admin_user_label_html as _admin_user_label_html,
    admin_send_csv as _db_admin_send_csv,
    admin_top_users_text,
    admin_user_overview_text,
    admin_user_recent_text,
    delete_tg_audio_file_id as _delete_tg_audio_file_id,
    get_last_track_id,
    get_search_history,
    get_tg_audio_cache as _get_tg_audio_cache,
    get_user_favorites,
    get_user_settings,
    get_user_stats,
    init_db,
    is_favorite,
    log_play_event,
    log_search_query,
    remove_from_favorites,
    set_tg_audio_file_id as _set_tg_audio_file_id,
    update_user_settings,
    upsert_tg_user,
)
from .logger import setup_logging
from .music_service import (
    download_clip,
    download_track,
    get_album_tracks_from_service,
    get_artist_full_info,
    get_artist_tracks,
    get_track_info,
    get_track_lyrics_by_id,
    get_tracks_by_ids,
    search_music,
)
from .state import run_async, user_states


setup_logging()
validate_tokens()
init_db()

# Анти-спам система
user_last_message = defaultdict(float)
user_message_count = defaultdict(int)
spam_warnings = defaultdict(int)

def anti_spam_check(user_id: int, chat_id: int) -> bool:
    """Проверка на спам. Возвращает True если сообщение разрешено."""
    current_time = time.time()
    
    # Сброс счетчиков каждые 60 секунд
    if current_time - user_last_message[user_id] > 60:
        user_message_count[user_id] = 0
        spam_warnings[user_id] = 0
    
    # Проверка скорости сообщений
    if current_time - user_last_message[user_id] < 1:  # меньше 1 секунды между сообщениями
        user_message_count[user_id] += 1
        if user_message_count[user_id] > 3:  # более 3 быстрых сообщений
            spam_warnings[user_id] += 1
            if spam_warnings[user_id] > 2:  # более 2 предупреждений
                return False  # блокируем
    else:
        user_message_count[user_id] = max(0, user_message_count[user_id] - 1)
    
    user_last_message[user_id] = current_time
    return True

def group_protection(func):
    """Декоратор для защиты групповых чатов"""
    @wraps(func)
    def wrapped(message, *args, **kwargs):
        # Проверка на спам
        if not anti_spam_check(message.from_user.id, message.chat.id):
            try:
                bot.reply_to(message, "⚠️ Слишком много сообщений! Подождите немного.")
                logging.warning(f"Спам заблокирован: пользователь {message.from_user.id} в чате {message.chat.id}")
            except Exception:
                pass
            return
        
        # Проверка прав в группах
        if message.chat.type in ["group", "supergroup"]:
            try:
                # Проверяем, является ли пользователь администратором
                chat_member = bot.get_chat_member(message.chat.id, message.from_user.id)
                if chat_member.status not in ["administrator", "creator"]:
                    # В группах обычные пользователи могут использовать только команды
                    if not message.text.startswith('/'):
                        try:
                            bot.reply_to(message, "⚠️ В группах используйте команды с / (например: /music название трека)")
                        except Exception:
                            pass
                        return
            except Exception as e:
                logging.warning(f"Не удалось проверить права пользователя: {e}")
                # Если не удалось проверить права, разрешаем использование команд
                if not message.text.startswith('/'):
                    return
        
        return func(message, *args, **kwargs)
    return wrapped

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=8)


def group_chat_only(func):
    @wraps(func)
    def wrapped(message, *args, **kwargs):
        if message.chat.type not in {"private", "group", "supergroup", "channel"}:
            return
        try:
            upsert_tg_user(getattr(message, "from_user", None))
        except Exception:
            pass
        return func(message, *args, **kwargs)
    return wrapped


def _run_bg(fn, *args, **kwargs):
    try:
        run_async(fn, *args, **kwargs)
    except Exception as e:
        logging.error(f"Ошибка запуска фоновой задачи {fn}: {e}")


def _handle_inline_music_download(inline_message_id: str, user_id: int, track_id: str):
    """Скачивает трек и заменяет фото обложки на аудиофайл в инлайн-сообщении."""
    filepath = None
    try:
        codec = "mp3"
        track = get_track_info(track_id)
        title = track.title if track else "Трек"
        artist = track.artist_name() if track and hasattr(track, "artist_name") else ""

        # 1. Проверяем кэш file_id
        cached = _get_tg_audio_cache(track_id, codec)
        if cached and cached.get("file_id"):
            file_id = cached["file_id"]
            logging.info(f"Используем кэшированный file_id для трека {track_id}")
        else:
            # 2. Скачиваем трек
            if not track:
                bot.edit_message_caption(
                    caption="❌ Трек не найден",
                    inline_message_id=inline_message_id
                )
                return

            filepath = download_track(track, codec=codec)
            if not filepath:
                bot.edit_message_caption(
                    caption="❌ Не удалось загрузить трек",
                    inline_message_id=inline_message_id
                )
                return

            # 3. Отправляем в ЛС пользователя, чтобы получить file_id
            try:
                with open(filepath, "rb") as f:
                    msg = bot.send_audio(
                        user_id, f,
                        title=title,
                        performer=artist,
                        caption="@ZvonkoMusicbot"
                    )
                file_id = msg.audio.file_id
                _set_tg_audio_file_id(track_id, codec, file_id, title, artist)
                logging.info(f"Получен file_id для трека {track_id}: {file_id[:20]}...")
                # Удаляем сообщение из ЛС чтобы не дублировать
                try:
                    bot.delete_message(user_id, msg.message_id)
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"Не удалось отправить в ЛС user={user_id}: {e}")
                bot.edit_message_caption(
                    caption=f"❌ Начните диалог с @{BOT_USERNAME or 'ZvonkoMusicbot'} чтобы скачивать треки",
                    inline_message_id=inline_message_id
                )
                return

        # 4. Заменяем фото обложки на аудиофайл в инлайн-сообщении
        media = types.InputMediaAudio(
            media=file_id,
            title=title,
            performer=artist,
            caption="@ZvonkoMusicbot - сервис для скачивания треков"
        )
        bot.edit_message_media(media=media, inline_message_id=inline_message_id)
        logging.info(f"Инлайн-сообщение обновлено аудио для трека {track_id}")

    except Exception as e:
        logging.error(f"Ошибка в _handle_inline_music_download: {e}", exc_info=True)
        try:
            bot.edit_message_caption(
                caption="❌ Ошибка при загрузке трека",
                inline_message_id=inline_message_id
            )
        except Exception:
            pass
    finally:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


def _handle_inline_clip_download(inline_message_id: str, user_id: int, track_id: str):
    """Скачивает клип и заменяет фото обложки на видео в инлайн-сообщении."""
    clip_path = None
    try:
        track = get_track_info(track_id)
        if not track:
            bot.edit_message_caption(
                caption="❌ Трек не найден",
                inline_message_id=inline_message_id
            )
            return

        title = track.title if track else "Трек"
        artist = track.artist_name() if track and hasattr(track, "artist_name") else ""
        full_name = f"{title} — {artist}".strip(" —")

        # 1. Скачиваем клип
        clip_path = download_clip(track)
        if not clip_path:
            bot.edit_message_caption(
                caption=f"❌ Не удалось найти клип для «{full_name}»",
                inline_message_id=inline_message_id
            )
            return

        # 2. Отправляем в ЛС пользователя, чтобы получить file_id
        try:
            with open(clip_path, "rb") as f:
                msg = bot.send_video(
                    user_id, f,
                    caption=f"🎬 {full_name}",
                    supports_streaming=True
                )
            file_id = msg.video.file_id
            logging.info(f"Получен video file_id для клипа {track_id}")
            # Удаляем сообщение из ЛС чтобы не дублировать
            try:
                bot.delete_message(user_id, msg.message_id)
            except Exception:
                pass
        except Exception as e:
            logging.error(f"Не удалось отправить клип в ЛС user={user_id}: {e}")
            bot.edit_message_caption(
                caption=f"❌ Начните диалог с @{BOT_USERNAME or 'ZvonkoMusicbot'} чтобы скачивать клипы",
                inline_message_id=inline_message_id
            )
            return

        # 3. Заменяем фото обложки на видео в инлайн-сообщении
        media = types.InputMediaVideo(
            media=file_id,
            caption=f"🎬 {full_name}\n@ZvonkoMusicbot",
            supports_streaming=True
        )
        bot.edit_message_media(media=media, inline_message_id=inline_message_id)
        logging.info(f"Инлайн-сообщение обновлено видео для клипа {track_id}")

    except Exception as e:
        logging.error(f"Ошибка в _handle_inline_clip_download: {e}", exc_info=True)
        try:
            bot.edit_message_caption(
                caption="❌ Ошибка при загрузке клипа",
                inline_message_id=inline_message_id
            )
        except Exception:
            pass
    finally:
        if clip_path and os.path.exists(clip_path):
            try:
                os.remove(clip_path)
            except Exception:
                pass


def _check_subscription(user_id, chat_id):
    """Проверяет подписку пользователя на канал @pavlopump"""
    try:
        member = bot.get_chat_member("@pavlopump", user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
        logging.warning(f"Ошибка проверки подписки: {e}")
        return True
    
    return False


def _send_subscription_request(chat_id):
    """Отправляет сообщение с просьбой подписаться"""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Подписаться", url="https://t.me/pavlopump"))
    kb.add(types.InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription"))
    
    bot.send_message(
        chat_id,
        "🚫 **Требуется подписка на канал**\n\n"
        "Для использования бота необходимо подписаться на канал.\n\n"
        "📢 **Канал:** @pavlopump\n\n"
        "После подписки нажмите кнопку ниже 👇",
        reply_markup=kb,
        parse_mode="Markdown"
    )


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_IDS) and int(user_id) in ADMIN_IDS


def _format_bytes(num):
    try:
        num = float(num)
    except Exception:
        return str(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PB"


def _uptime_str():
    sec = max(0, int(time.time() - BOT_START_TS))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h}ч {m}м {s}с"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def _proc_mem_info():
    rss = None
    vms = None
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = line.split(":", 1)[1].strip()
                elif line.startswith("VmSize:"):
                    vms = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return rss, vms


def _admin_service_text():
    rss, vms = _proc_mem_info()
    size = _format_bytes(os.path.getsize(DB_PATH)) if os.path.exists(DB_PATH) else "-"
    return (
        "🧠 Сервис:\n"
        f"PID: {os.getpid()}\n"
        f"Uptime: {_uptime_str()}\n"
        f"RSS: {rss or '-'}\n"
        f"VMS: {vms or '-'}\n"
        f"DB: {size}"
    )


# ─── Keyboards ───────────────────────────────────────────────────────────────

def _main_menu_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔎 Поиск", callback_data="nav:search"),
        types.InlineKeyboardButton("🎤 Исполнитель", callback_data="nav:artist")
    )
    kb.row(
        types.InlineKeyboardButton("⭐ Избранное", callback_data="nav:fav"),
        types.InlineKeyboardButton("🕘 История", callback_data="nav:history")
    )
    kb.row(
        types.InlineKeyboardButton("📊 Статистика", callback_data="nav:stats"),
        types.InlineKeyboardButton("⚙ Настройки", callback_data="nav:settings")
    )
    kb.add(types.InlineKeyboardButton("ℹ️ О боте", callback_data="nav:info"))
    return kb


def _reply_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🔎 Поиск"), types.KeyboardButton("� Исполнитель"))
    kb.add(types.KeyboardButton("⭐ Избранное"), types.KeyboardButton("🕘 История"))
    kb.add(types.KeyboardButton("📊 Статистика"), types.KeyboardButton("⚙ Настройки"))
    kb.add(types.KeyboardButton("ℹ️ О боте"))
    kb.add(types.KeyboardButton("🏠 Меню"))
    return kb


def _info_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Канал разработчика", url="https://t.me/pavlopump"))
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    return kb


def _back_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    return kb


def _admin_menu_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📊 Статистика БД", callback_data="admin:stats"),
        types.InlineKeyboardButton("👤 Пользователь", callback_data="admin:user:prompt")
    )
    kb.row(
        types.InlineKeyboardButton("🧾 Топ пользователей", callback_data="admin:top"),
        types.InlineKeyboardButton("🕘 Последние события", callback_data="admin:recent")
    )
    kb.add(types.InlineKeyboardButton("🧠 Сервис", callback_data="admin:service"))
    kb.row(
        types.InlineKeyboardButton("💾 Бэкап БД", callback_data="admin:db:backup"),
        types.InlineKeyboardButton("🧹 VACUUM", callback_data="admin:db:vacuum")
    )
    kb.add(types.InlineKeyboardButton("⬅ В меню", callback_data="nav:main"))
    return kb


def _admin_user_keyboard(target_user_id, s):
    uid = int(target_user_id)
    kb = types.InlineKeyboardMarkup()

    def _onoff(v):
        return "✅" if v else "❌"

    reply_menu = bool(s.get("reply_menu", True))
    auto_download = bool(s.get("auto_download"))
    show_covers = bool(s.get("show_covers"))
    show_tips = bool(s.get("show_tips", True))
    codec = (s.get("download_quality", "mp3") or "mp3").lower()
    per_page_val = int(s.get("items_per_page", PER_PAGE) or PER_PAGE)
    search_limit_val = int(s.get("search_limit", 50) or 50)

    kb.row(
        types.InlineKeyboardButton(f"🧾 Reply-меню {_onoff(reply_menu)}", callback_data=f"admin:set:{uid}:reply_menu"),
        types.InlineKeyboardButton(f"⬇️ Автоскачивание {_onoff(auto_download)}", callback_data=f"admin:set:{uid}:auto_download")
    )
    kb.row(
        types.InlineKeyboardButton(f"🖼️ Обложки {_onoff(show_covers)}", callback_data=f"admin:set:{uid}:show_covers"),
        types.InlineKeyboardButton(f"💡 Подсказки {_onoff(show_tips)}", callback_data=f"admin:set:{uid}:tips")
    )
    kb.add(types.InlineKeyboardButton(f"🎧 Качество: {codec.upper()}", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if codec == 'mp3' else ''}MP3", callback_data=f"admin:set:{uid}:quality:mp3"),
        types.InlineKeyboardButton(f"{'✅ ' if codec == 'aac' else ''}AAC", callback_data=f"admin:set:{uid}:quality:aac")
    )
    kb.add(types.InlineKeyboardButton(f"📄 На странице: {per_page_val}", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 5 else ''}5", callback_data=f"admin:set:{uid}:page:5"),
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 8 else ''}8", callback_data=f"admin:set:{uid}:page:8"),
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 10 else ''}10", callback_data=f"admin:set:{uid}:page:10")
    )
    kb.add(types.InlineKeyboardButton(f"🔎 Лимит поиска: {search_limit_val}", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 10 else ''}10", callback_data=f"admin:set:{uid}:slimit:10"),
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 25 else ''}25", callback_data=f"admin:set:{uid}:slimit:25"),
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 50 else ''}50", callback_data=f"admin:set:{uid}:slimit:50")
    )
    kb.row(
        types.InlineKeyboardButton("🧽 Очистить избранное", callback_data=f"admin:clear_confirm:{uid}:fav"),
        types.InlineKeyboardButton("🧽 Очистить историю", callback_data=f"admin:clear_confirm:{uid}:hist")
    )
    kb.add(types.InlineKeyboardButton("🧽 Очистить прослушивания", callback_data=f"admin:clear_confirm:{uid}:plays"))
    kb.row(
        types.InlineKeyboardButton("🕘 Последние действия", callback_data=f"admin:user:{uid}:recent"),
        types.InlineKeyboardButton("📤 Экспорт", callback_data=f"admin:user:{uid}:export"),
    )
    kb.row(
        types.InlineKeyboardButton("⬅ Назад", callback_data="admin:open"),
        types.InlineKeyboardButton("🔄 Обновить", callback_data=f"admin:user:{uid}")
    )
    return kb


def _admin_confirm_keyboard(ok_cb, cancel_cb):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Да", callback_data=ok_cb),
        types.InlineKeyboardButton("❌ Нет", callback_data=cancel_cb)
    )
    return kb


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _send_or_edit(call, text, reply_markup=None, parse_mode=None):
    if call and getattr(call, "message", None):
        try:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
            return
        except Exception:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    chat_id = call if isinstance(call, int) else getattr(call, "chat", {})
    if isinstance(chat_id, int):
        bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)


def _send_info(chat_id, call=None):
    text = ("ℹ️ **О Zvonko Music Bot**\n\n"
            "🎵 **Лучший бот для поиска и скачивания музыки!**\n\n"
            "🔍 **Что я умею:**\n"
            "• Искать треки по названию и исполнителям\n"
            "• Скачивать аудио в высоком качестве (MP3/AAC)\n"
            "• Искать и скачивать клипы с YouTube\n"
            "• Показывать тексты песен через Genius\n"
            "• Сохранять ваше избранное\n"
            "• Вести историю прослушиваний\n\n"
            "🛠️ **Технологии:**\n"
            "• Поиск через Spotify API\n"
            "• Загрузка с YouTube Music\n"
            "• Клипы с YouTube\n"
            "• Тексты песен с Genius\n\n"
            "📊 **Статистика:**\n"
            "• Пользователей: 1000\n"
            "• Треков: 100000\n"
            "• Клипов: 50000")
    _send_or_edit(call or chat_id, text, reply_markup=_info_keyboard(), parse_mode="Markdown")


def _format_track_line(track, idx=None):
    artist = track.artist_name() if hasattr(track, "artist_name") else (track.artists[0].name if getattr(track, "artists", None) else "")
    title = getattr(track, "title", "")
    
    # Use chart position if available (for Yandex chart tracks)
    if hasattr(track, "chart_position") and track.chart_position:
        prefix = track.chart_position
    elif idx is not None:
        prefix = f"{idx}. "
    else:
        prefix = ""
    
    return f"{prefix}{title} — {artist}".strip()


def _store_list(user_id, key, track_ids, title):
    state = user_states.get(user_id, {})
    lists = state.get("lists", {})
    lists[key] = {"ids": [str(x) for x in track_ids], "title": title}
    state["lists"] = lists
    # Store the last list key for back navigation
    state["last_list_key"] = key
    user_states[user_id] = state


def _send_list_page(chat_id, user_id, key, page=0, call=None):
    state = user_states.get(user_id, {})
    info = state.get("lists", {}).get(key)
    if not info:
        _send_or_edit(call or chat_id, "Список устарел. Открой меню заново.", reply_markup=_main_menu_keyboard())
        return

    ids = info.get("ids", [])
    title = info.get("title", "")
    page = max(0, int(page))
    s = get_user_settings(user_id)
    per_page = int(s.get("items_per_page", PER_PAGE) or PER_PAGE)
    per_page = max(1, min(10, per_page))
    start = page * per_page
    end = start + per_page
    page_ids = ids[start:end]
    tracks = get_tracks_by_ids(page_ids)

    kb = types.InlineKeyboardMarkup()
    for i, t in enumerate(tracks, start=start + 1):
        kb.add(types.InlineKeyboardButton(_format_track_line(t, i)[:64], callback_data=f"track:{t.id}"))

    nav = []
    if start > 0:
        nav.append(types.InlineKeyboardButton("⬅ Пред", callback_data=f"page:{key}:{page-1}"))
    if end < len(ids):
        nav.append(types.InlineKeyboardButton("След ➡", callback_data=f"page:{key}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    _send_or_edit(call or chat_id, title, reply_markup=kb)


def _send_track_selection_with_cover(chat_id, track_id, track_name, user_id=None):
    """Send track selection message with cover art if available."""
    try:
        track = get_track_info(track_id)
        if not track:
            # Fallback to text message if track not found
            text = f"🎶 {track_name}\n\nВыберите действие:"
            bot.send_message(
                chat_id,
                text,
                reply_markup=_build_track_choice_keyboard(track_id, user_id)
            )
            return

        # Try to send with cover
        cover_uri = getattr(track, 'cover_uri', None)
        if cover_uri:
            try:
                # Convert cover_uri to URL if needed
                cover_url = cover_uri
                if not cover_uri.startswith("http"):
                    cover_url = f"https://{cover_uri}"
                
                caption = f"🎶 {track_name}\n\nВыберите действие:"
                bot.send_photo(
                    chat_id,
                    cover_url,
                    caption=caption,
                    reply_markup=_build_track_choice_keyboard(track_id, user_id)
                )
                return
            except Exception as e:
                logging.warning(f"Failed to send track selection with cover: {e}")
        
        # Fallback to text message
        text = f"🎶 {track_name}\n\nВыберите действие:"
        bot.send_message(
            chat_id,
            text,
            reply_markup=_build_track_choice_keyboard(track_id, user_id)
        )
        
    except Exception as e:
        logging.error(f"Error in _send_track_selection_with_cover: {e}")
        # Final fallback
        text = f"🎶 {track_name}\n\nВыберите действие:"
        bot.send_message(
            chat_id,
            text,
            reply_markup=_build_track_choice_keyboard(track_id, user_id)
        )


def _build_track_choice_keyboard(track_id, user_id=None):
    """Keyboard shown when user clicks a track: choose audio or video."""
    from .youtube_service import check_video_clip_exists
    
    kb = types.InlineKeyboardMarkup()
    tid = str(track_id)
    
    # Check if video clip exists
    track = get_track_info(track_id)
    clip_exists = False
    if track:
        title = track.title or ""
        artist = track.artist_name() if hasattr(track, "artist_name") else ""
        clip_exists = check_video_clip_exists(title, artist)
    
    if clip_exists:
        kb.row(
            types.InlineKeyboardButton("🎵 Скачать трек", callback_data=f"play:{tid}"),
            types.InlineKeyboardButton("🎬 Скачать клип", callback_data=f"clip:{tid}")
        )
    else:
        kb.row(
            types.InlineKeyboardButton("🎵 Скачать трек", callback_data=f"play:{tid}"),
            types.InlineKeyboardButton("🎬 Клип недоступен", callback_data="noop")
        )
    
    kb.add(types.InlineKeyboardButton("📝 Текст песни", callback_data=f"lyrics:{tid}"))
    
    # Add back button - try to return to previous list, otherwise main menu
    back_callback = "nav:main"
    if user_id:
        state = user_states.get(user_id, {})
        last_list_key = state.get("last_list_key")
        if last_list_key:
            back_callback = f"back_to_list:{last_list_key}"
    
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data=back_callback))
    return kb


def _send_track_actions_with_cover(chat_id, track_id, user_id):
    """Send track actions menu with cover art."""
    try:
        track = get_track_info(track_id)
        if not track:
            return
        
        title = track.title or "Трек"
        artist = track.artist_name() if hasattr(track, "artist_name") else ""
        full_name = f"{title} — {artist}".strip(" —")
        
        # Try to send with cover
        cover_uri = getattr(track, 'cover_uri', None)
        if cover_uri:
            try:
                # Convert cover_uri to URL if needed
                cover_url = cover_uri
                if not cover_uri.startswith("http"):
                    cover_url = f"https://{cover_uri}"
                
                caption = f"🎶 {full_name}\n\nВыберите действие:"
                bot.send_photo(
                    chat_id,
                    cover_url,
                    caption=caption,
                    reply_markup=_build_track_actions_keyboard(track_id, user_id)
                )
                return
            except Exception as e:
                logging.warning(f"Failed to send track actions with cover: {e}")
        
        # Fallback to text message
        text = f"🎶 {full_name}\n\nВыберите действие:"
        bot.send_message(
            chat_id,
            text,
            reply_markup=_build_track_actions_keyboard(track_id, user_id)
        )
        
    except Exception as e:
        logging.error(f"Error in _send_track_actions_with_cover: {e}")


def _build_track_actions_keyboard(track_id, user_id):
    kb = types.InlineKeyboardMarkup()
    tid = str(track_id)
    if is_favorite(user_id, tid):
        kb.add(types.InlineKeyboardButton("🗑 Убрать из избранного", callback_data=f"unfav:{tid}"))
    else:
        kb.add(types.InlineKeyboardButton("⭐ В избранное", callback_data=f"fav:{tid}"))
    settings = get_user_settings(user_id)
    if settings.get("show_lyrics_button", True):
        kb.add(types.InlineKeyboardButton("📝 Текст", callback_data=f"lyrics:{tid}"))
    
    # Add back button to return to track choice menu
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data=f"back_to_track_choice:{tid}"))
    return kb


def _lyrics_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖ Закрыть текст", callback_data="lyrics_close"))
    return kb


def _split_text(text, limit=3500):
    if not text:
        return []
    chunks = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(text_length, start + limit)
        if end < text_length:
            last_newline = text.rfind("\n", start, end)
            if last_newline > start + 100:
                end = last_newline + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def _format_lyrics_chunk(chunk: str) -> str:
    if not chunk:
        return ""
    lines = []
    for raw in chunk.splitlines():
        line = raw.strip()
        lines.append(line)
    return "\n".join(lines)


# ─── Core send functions ─────────────────────────────────────────────────────

def _send_track_lyrics(chat_id, user_id, track_id):
    try:
        track = get_track_info(track_id)
        title = getattr(track, "title", None) or "Трек"
        artist = track.artist_name() if track and hasattr(track, "artist_name") else ""
        full_name = f"{title} — {artist}".strip(" —")

        lyrics = get_track_lyrics_by_id(track_id)
        if not lyrics:
            bot.send_message(chat_id, f"Текст для «{full_name or track_id}» не найден.")
            return

        header = f"📝 Текст: {full_name or track_id}\n\n"
        chunks = _split_text(lyrics, 3500) or [lyrics]
        kb = _lyrics_keyboard()
        for idx, chunk in enumerate(chunks):
            prefix = header if idx == 0 else ""
            formatted = _format_lyrics_chunk(chunk)
            bot.send_message(chat_id, f"{prefix}{formatted}", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка при отправке текста трека {track_id}: {e}")
        try:
            bot.send_message(chat_id, "Не удалось получить текст трека. Попробуйте позже.")
        except Exception:
            pass


def _send_track_clip(chat_id, user_id, track_id):
    """Download and send a music video clip from YouTube."""
    try:
        track = get_track_info(track_id)
        if not track:
            bot.send_message(chat_id, "Не удалось получить информацию о треке.")
            return

        title = track.title or "Трек"
        artist = track.artist_name() if hasattr(track, "artist_name") else ""
        full_name = f"{title} — {artist}".strip(" —")

        msg = bot.send_message(chat_id, f"🎬 Загружаю клип: {full_name}...")

        clip_path = download_clip(track)
        if not clip_path:
            try:
                bot.edit_message_text(
                    f"Не удалось найти клип для «{full_name}».",
                    chat_id=chat_id,
                    message_id=msg.message_id
                )
            except Exception:
                bot.send_message(chat_id, f"Не удалось найти клип для «{full_name}».")
            return

        try:
            file_size = os.path.getsize(clip_path)
            if file_size > 50 * 1024 * 1024:
                bot.edit_message_text(
                    f"Клип «{full_name}» слишком большой для отправки в Telegram ({_format_bytes(file_size)}).",
                    chat_id=chat_id,
                    message_id=msg.message_id
                )
                return

            with open(clip_path, "rb") as video_file:
                bot.send_video(
                    chat_id,
                    video_file,
                    caption=f"🎬 {full_name}",
                    supports_streaming=True
                )
            try:
                bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
        finally:
            if os.path.exists(clip_path):
                os.remove(clip_path)

    except Exception as e:
        logging.error(f"Ошибка при отправке клипа {track_id}: {e}")
        try:
            bot.send_message(chat_id, "Произошла ошибка при загрузке клипа.")
        except Exception:
            pass


def _send_track_by_id(chat_id, user_id, track_id):
    t0 = time.time()
    settings = get_user_settings(user_id)
    filepath = None
    cover_thumb_path = None
    sent_ok = False

    try:
        codec = settings.get("download_quality", "mp3") or "mp3"

        cached = _get_tg_audio_cache(track_id, codec)
        track = get_track_info(track_id)

        if cached and cached.get("file_id"):
            title = cached.get("track_title") or None
            performer = cached.get("artist_name") or None
            try:
                msg = bot.send_audio(
                    chat_id,
                    cached["file_id"],
                    title=title,
                    performer=performer,
                    caption="@ZvonkoMusicbot - сервис для скачивания треков"
                )
                sent_ok = True
                try:
                    fid = getattr(getattr(msg, "audio", None), "file_id", None)
                    if fid:
                        _set_tg_audio_file_id(track_id, codec, fid, title, performer)
                except Exception:
                    pass
                try:
                    log_play_event(user_id, str(track_id), title or "", performer or "")
                except Exception:
                    pass
                return
            except Exception as e:
                logging.warning(f"Не удалось отправить из кэша Telegram: {e}")
                _delete_tg_audio_file_id(track_id, codec)

        if not track:
            bot.send_message(chat_id, "Не удалось получить информацию о треке.")
            return

        title = track.title or "Трек"
        artist = track.artist_name() if hasattr(track, "artist_name") else ""

        status_msg = bot.send_message(chat_id, f"⏳ Загружаю: {title} — {artist}...")

        filepath = download_track(track, codec=codec)
        if not filepath:
            try:
                bot.edit_message_text(
                    "Не удалось загрузить трек.",
                    chat_id=chat_id,
                    message_id=status_msg.message_id
                )
            except Exception:
                bot.send_message(chat_id, "Не удалось загрузить трек.")
            return

        if settings.get("show_covers", True) and track.cover_uri:
            try:
                cover_url = track.cover_uri
                if not cover_url.startswith("http"):
                    cover_url = f"https://{cover_url}"
                response = requests.get(cover_url, stream=True, timeout=7)
                response.raise_for_status()
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                cover_thumb_path = tmp.name
                for chunk in response.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp.close()
            except Exception as e:
                logging.warning(f"Не удалось загрузить обложку: {e}")
                cover_thumb_path = None

        if cover_thumb_path and os.path.exists(cover_thumb_path):
            try:
                with open(cover_thumb_path, "rb") as thumb:
                    with open(filepath, "rb") as audio:
                        msg = bot.send_audio(
                            chat_id,
                            audio,
                            title=title,
                            performer=artist,
                            thumbnail=thumb,
                            caption="@ZvonkoMusicbot - сервис для скачивания треков"
                        )
                        try:
                            fid = getattr(getattr(msg, "audio", None), "file_id", None)
                            if fid:
                                _set_tg_audio_file_id(track_id, codec, fid, title, artist)
                        except Exception:
                            pass
                        sent_ok = True
            except Exception as e:
                logging.error(f"Ошибка при отправке трека с thumbnail: {e}")
                sent_ok = False

        if not sent_ok:
            with open(filepath, "rb") as audio_file:
                msg = bot.send_audio(
                    chat_id,
                    audio_file,
                    title=title,
                    performer=artist,
                    caption="@ZvonkoMusicbot - сервис для скачивания треков"
                )
                try:
                    fid = getattr(getattr(msg, "audio", None), "file_id", None)
                    if fid:
                        _set_tg_audio_file_id(track_id, codec, fid, title, artist)
                except Exception:
                    pass
            sent_ok = True

        try:
            bot.delete_message(chat_id, status_msg.message_id)
        except Exception:
            pass

    except Exception as e:
        logging.error(f"Ошибка при отправке трека: {e}")
        bot.send_message(chat_id, "Произошла ошибка при отправке трека.")
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        if cover_thumb_path and os.path.exists(cover_thumb_path):
            os.remove(cover_thumb_path)

    if sent_ok and track:
        artist_name = track.artist_name() if hasattr(track, "artist_name") else ""
        log_play_event(user_id, str(track.id), track.title or "", artist_name)


def _send_artist_info(chat_id, user_id, artist_name, call=None):
    """Send artist information with photo, top tracks and albums."""
    try:
        artist_info = get_artist_full_info(artist_name)
        if not artist_info:
            _send_or_edit(call or chat_id, f"Не удалось найти исполнителя: {artist_name}", reply_markup=_back_keyboard())
            return

        # Send artist photo if available
        if artist_info.get("photo_url"):
            try:
                bot.send_photo(
                    chat_id,
                    artist_info["photo_url"],
                    caption=f"🎤 **{artist_info['name']}**\n"
                           f"👥 {artist_info.get('followers', 0):,} подписчиков\n"
                           f"🔥 Популярность: {artist_info.get('popularity', 0)}/100",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.warning(f"Failed to send artist photo: {e}")

        # Send top tracks
        top_tracks = artist_info.get("top_tracks", [])
        if top_tracks:
            kb = types.InlineKeyboardMarkup()
            for i, track in enumerate(top_tracks[:10], 1):
                kb.add(types.InlineKeyboardButton(
                    f"{i}. {track.title} — {track.artist_name()}"[:64],
                    callback_data=f"track:{track.id}"
                ))
            
            # Add albums button if artist has albums
            albums = artist_info.get("albums", [])
            if albums:
                kb.row(
                    types.InlineKeyboardButton("💿 Альбомы", callback_data="artist_albums"),
                    types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main")
                )
            else:
                kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
            
            _send_or_edit(call or chat_id, f"🔥 **Популярные треки {artist_info['name']}**:", 
                         reply_markup=kb, parse_mode="Markdown")
        else:
            kb = types.InlineKeyboardMarkup()
            albums = artist_info.get("albums", [])
            if albums:
                kb.row(
                    types.InlineKeyboardButton("💿 Альбомы", callback_data="artist_albums"),
                    types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main")
                )
            else:
                kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
            
            _send_or_edit(call or chat_id, f"🎤 **{artist_info['name']}**\n\nНет популярных треков", 
                         reply_markup=kb, parse_mode="Markdown")

        # Store albums info for navigation
        albums = artist_info.get("albums", [])
        if albums:
            state = user_states.get(user_id, {})
            state["artist_albums"] = {
                "artist_name": artist_info["name"],
                "albums": albums
            }
            user_states[user_id] = state

    except Exception as e:
        logging.error(f"Error sending artist info: {e}")
        _send_or_edit(call or chat_id, "Произошла ошибка при загрузке информации об исполнителе.", 
                     reply_markup=_back_keyboard())


def _send_albums_list(chat_id, user_id, call=None):
    """Send list of artist albums."""
    try:
        state = user_states.get(user_id, {})
        albums_data = state.get("artist_albums")
        if not albums_data:
            _send_or_edit(call or chat_id, "Информация об альбомах устарела.", reply_markup=_back_keyboard())
            return

        artist_name = albums_data["artist_name"]
        albums = albums_data["albums"]

        kb = types.InlineKeyboardMarkup()
        for album in albums[:15]:  # Show first 15 albums
            album_type_icon = "💿" if album["type"] == "album" else "🎵"
            release_year = album["release_date"][:4] if album["release_date"] else "????"
            text = f"{album_type_icon} {album['name']} ({release_year})"[:64]
            kb.add(types.InlineKeyboardButton(text, callback_data=f"album:{album['id']}"))
        
        kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
        
        _send_or_edit(call or chat_id, f"💿 **Альбомы {artist_name}**:", 
                     reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error sending albums list: {e}")
        _send_or_edit(call or chat_id, "Произошла ошибка при загрузке альбомов.", 
                     reply_markup=_back_keyboard())


def _send_album_tracks(chat_id, user_id, album_id, call=None):
    """Send tracks from a specific album."""
    try:
        state = user_states.get(user_id, {})
        albums_data = state.get("artist_albums")
        if not albums_data:
            _send_or_edit(call or chat_id, "Информация об альбоме устарела.", reply_markup=_back_keyboard())
            return

        # Find album info
        album = None
        for a in albums_data["albums"]:
            if a["id"] == album_id:
                album = a
                break

        if not album:
            _send_or_edit(call or chat_id, "Альбом не найден.", reply_markup=_back_keyboard())
            return

        tracks = get_album_tracks_from_service(album_id)
        if not tracks:
            _send_or_edit(call or chat_id, f"Не удалось загрузить треки альбома: {album['name']}", 
                         reply_markup=_back_keyboard())
            return

        kb = types.InlineKeyboardMarkup()
        for i, track in enumerate(tracks, 1):
            kb.add(types.InlineKeyboardButton(
                f"{i}. {track.title}"[:64],
                callback_data=f"track:{track.id}"
            ))
        
        kb.row(
            types.InlineKeyboardButton("⬅ К альбомам", callback_data="artist_albums"),
            types.InlineKeyboardButton("🏠 В меню", callback_data="nav:main")
        )

        album_type = "Альбом" if album["type"] == "album" else "Сингл"
        _send_or_edit(call or chat_id, f"💿 **{album_type}: {album['name']}**\n"
                                      f"📅 {album['release_date']} • {album['total_tracks']} треков", 
                     reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error sending album tracks: {e}")
        _send_or_edit(call or chat_id, "Произошла ошибка при загрузке треков альбома.", 
                     reply_markup=_back_keyboard())


def _send_favorites(chat_id, user_id, call=None):
    favorites = get_user_favorites(user_id)
    if not favorites:
        _send_or_edit(call or chat_id, "В избранном пока пусто.", reply_markup=_back_keyboard())
        return
    
    # Store favorites context for back navigation
    track_ids = [track_id for track_id, _, _, _ in favorites if track_id]
    _store_list(user_id, "favorites", track_ids, "⭐ Избранное:")
    
    kb = types.InlineKeyboardMarkup()
    for track_id, track_title, artist_name, _added_date in favorites[:20]:
        text = f"{track_title} — {artist_name}".strip()
        kb.add(types.InlineKeyboardButton(text[:64], callback_data=f"track:{track_id}"))
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    _send_or_edit(call or chat_id, "⭐ Избранное:", reply_markup=kb)


def _send_history(chat_id, user_id, page=0, call=None):
    rows = get_search_history(user_id, limit=50)
    queries = [r[0] for r in rows]
    state = user_states.get(user_id, {})
    state["history_queries"] = queries
    # Store history context for back navigation
    state["last_list_key"] = "history"
    user_states[user_id] = state

    page = max(0, int(page))
    start = page * PER_PAGE
    end = start + PER_PAGE
    part = queries[start:end]

    kb = types.InlineKeyboardMarkup()
    if not part:
        kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
        _send_or_edit(call or chat_id, "История пуста.", reply_markup=kb)
        return

    for idx, q in enumerate(part, start=start):
        kb.add(types.InlineKeyboardButton(f"🔎 {q}"[:64], callback_data=f"hq:{idx}"))

    nav = []
    if start > 0:
        nav.append(types.InlineKeyboardButton("⬅ Пред", callback_data=f"hpage:{page-1}"))
    if end < len(queries):
        nav.append(types.InlineKeyboardButton("След ➡", callback_data=f"hpage:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    _send_or_edit(call or chat_id, "🕘 История поисков:", reply_markup=kb)


def _send_stats(chat_id, user_id, call=None):
    s = get_user_stats(user_id)
    text = (
        f"📊 Статистика:\n"
        f"Избранное: {s.get('favorites')}\n"
        f"Поисков: {s.get('searches')}\n"
        f"Отправлено треков: {s.get('plays')}\n"
        f"Последний трек: {s.get('last_track') or '-'}"
    )
    _send_or_edit(call or chat_id, text, reply_markup=_back_keyboard())


def _send_settings(chat_id, user_id, call=None):
    s = get_user_settings(user_id)
    kb = types.InlineKeyboardMarkup()

    def _onoff(flag):
        return "✅" if flag else "❌"

    reply_menu = bool(s.get("reply_menu", True))
    auto_download = bool(s.get("auto_download"))
    show_covers = bool(s.get("show_covers"))
    show_tips = bool(s.get("show_tips", True))
    show_lyrics_button = bool(s.get("show_lyrics_button", True))
    codec = (s.get("download_quality", "mp3") or "mp3").lower()
    per_page_val = int(s.get("items_per_page", PER_PAGE) or PER_PAGE)
    search_limit_val = int(s.get("search_limit", 50) or 50)

    kb.row(
        types.InlineKeyboardButton(f"🧾 Reply-меню {_onoff(reply_menu)}", callback_data="set:reply_menu"),
        types.InlineKeyboardButton(f"⬇️ Автоскачивание {_onoff(auto_download)}", callback_data="set:auto_download")
    )
    kb.row(
        types.InlineKeyboardButton(f"🖼️ Обложки {_onoff(show_covers)}", callback_data="set:show_covers"),
        types.InlineKeyboardButton(f"💡 Подсказки {_onoff(show_tips)}", callback_data="set:tips")
    )
    kb.row(
        types.InlineKeyboardButton(f"📝 Кнопка текста {_onoff(show_lyrics_button)}", callback_data="set:lyrics_btn"),
        types.InlineKeyboardButton(f"� Качество звука", callback_data="noop")
    )
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if codec == 'mp3' else ''}MP3 320kbps", callback_data="set:quality:mp3"),
        types.InlineKeyboardButton(f"{'✅ ' if codec == 'aac' else ''}AAC 256kbps", callback_data="set:quality:aac")
    )
    kb.add(types.InlineKeyboardButton(f"📄 Результатов на странице: {per_page_val}", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 5 else ''}5", callback_data="set:page:5"),
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 8 else ''}8", callback_data="set:page:8"),
        types.InlineKeyboardButton(f"{'✅ ' if per_page_val == 10 else ''}10", callback_data="set:page:10")
    )
    kb.add(types.InlineKeyboardButton(f"🔎 Лимит поиска: {search_limit_val}", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 10 else ''}10", callback_data="set:slimit:10"),
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 25 else ''}25", callback_data="set:slimit:25"),
        types.InlineKeyboardButton(f"{'✅ ' if search_limit_val == 50 else ''}50", callback_data="set:slimit:50")
    )
    kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data="nav:main"))
    _send_or_edit(call or chat_id, "⚙️ **Настройки бота:**\n\nНастройте работу бота под себя", reply_markup=kb)


# ─── Admin user-id handler ───────────────────────────────────────────────────

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get("mode") == "admin_await_user_id")
def handle_admin_user_id(message):
    try:
        if message.chat.type != "private":
            return
        if not _is_admin(message.from_user.id):
            user_states.pop(message.from_user.id, None)
            return

        text = (message.text or "").strip()
        if not text:
            return
        if text.lower() in {"отмена", "cancel", "/cancel"}:
            user_states.pop(message.from_user.id, None)
            bot.reply_to(message, "Отменено.", reply_markup=_admin_menu_keyboard())
            return
        if not text.isdigit():
            bot.reply_to(message, "Введите числовой user_id (или напишите 'отмена').")
            return

        target_user_id = int(text)
        user_states[message.from_user.id] = {"mode": "admin"}
        s = get_user_settings(target_user_id)
        txt = admin_user_overview_text(target_user_id, s)
        bot.reply_to(message, txt, reply_markup=_admin_user_keyboard(target_user_id, s), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка в handle_admin_user_id: {e}")
        try:
            bot.reply_to(message, "Ошибка при обработке user_id.")
        except Exception:
            pass


# ─── Command handlers ────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
@group_chat_only
@group_protection
def send_welcome(message):
    user_states.pop(message.from_user.id, None)

    # Deep-link обработка из инлайн-режима: /start play_TRACKID, clip_TRACKID, lyrics_TRACKID
    if message.chat.type == "private" and message.text:
        parts = message.text.split(None, 1)
        if len(parts) == 2:
            param = parts[1].strip()
            if param.startswith("play_"):
                track_id = param[5:]
                if track_id:
                    bot.send_message(message.chat.id, "⏳ Начинаю загрузку трека...")
                    _run_bg(_send_track_by_id, message.chat.id, message.from_user.id, track_id)
                    return
            elif param.startswith("clip_"):
                track_id = param[5:]
                if track_id:
                    bot.send_message(message.chat.id, "⏳ Начинаю загрузку клипа...")
                    _run_bg(_send_track_clip, message.chat.id, message.from_user.id, track_id)
                    return
            elif param.startswith("lyrics_"):
                track_id = param[7:]
                if track_id:
                    _run_bg(_send_track_lyrics, message.chat.id, message.from_user.id, track_id)
                    return

    if message.chat.type == "private":
        # Проверяем подписку на канал
        if not _check_subscription(message.from_user.id, message.chat.id):
            _send_subscription_request(message.chat.id)
            return
        
        s = get_user_settings(message.from_user.id)
        if s.get("reply_menu", True):
            bot.send_message(
                message.chat.id,
                "🎵 **Добро пожаловать в Zvonko Music Bot!**\n\n"
                "Я помогу тебе искать и скачивать музыку, клипы и тексты песен.\n\n"
                "🔍 **Что я умею:**\n"
                "• Искать треки по названию\n"
                "• Находить музыку исполнителей\n"
                "• Скачивать аудио в высоком качестве\n"
                "• Искать клипы на YouTube\n"
                "• Показывать тексты песен\n"
                "• Сохранять избранное\n"
                "• Вести историю прослушиваний\n\n"
                "Выбирай действие кнопками ниже 👇\n\n",
                reply_markup=_reply_menu_keyboard(),
                parse_mode="Markdown"
            )
            return
        bot.send_message(message.chat.id, "Привет! 🎵", reply_markup=types.ReplyKeyboardRemove())
        bot.send_message(
            message.chat.id,
            "🎵 **Zvonko Music Bot**\n\n"
            "Ищи и скачивай музыку, клипы и тексты песен!\n\n",
            reply_markup=_main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return
    bot.send_message(
        message.chat.id,
        "🎵 **Zvonko Music Bot**\n\n"
        "Ищи и скачивай музыку! Используй кнопки ниже или команды.\n\n",
        reply_markup=_main_menu_keyboard(),
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["menu"])
@group_chat_only
@group_protection
def menu(message):
    user_states.pop(message.from_user.id, None)
    if message.chat.type == "private":
        s = get_user_settings(message.from_user.id)
        if s.get("reply_menu", True):
            bot.send_message(message.chat.id, "Главное меню:", reply_markup=_reply_menu_keyboard())
            return
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=types.ReplyKeyboardRemove())
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=_main_menu_keyboard())
        return
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=_main_menu_keyboard())


@bot.message_handler(func=lambda message: message.chat.type == "private" and (message.text or "") in {
    "🔎 Поиск", "🎤 Исполнитель", "⭐ Избранное", "🕘 История", "📊 Статистика", "⚙ Настройки", "ℹ️ О боте", "🏠 Меню"
})
@group_protection
def handle_private_menu_buttons(message):
    try:
        user_id = message.from_user.id
        
        # Проверяем подписку
        if not _check_subscription(user_id, message.chat.id):
            _send_subscription_request(message.chat.id)
            return
            
        s = get_user_settings(user_id)
        if not s.get("reply_menu", True):
            return
        text = (message.text or "").strip()

        if text == "🏠 Меню":
            user_states.pop(user_id, None)
            bot.send_message(message.chat.id, "Главное меню:", reply_markup=_reply_menu_keyboard())
            return
        if text == "🔎 Поиск":
            user_states[user_id] = {"mode": "awaiting_search"}
            msg = bot.send_message(message.chat.id, "🔍 Введите название трека или исполнителя для поиска:")
            user_states[user_id]["prompt_message_id"] = msg.message_id
            return
        if text == "🎤 Исполнитель":
            user_states[user_id] = {"mode": "awaiting_artist"}
            msg = bot.send_message(message.chat.id, "Введите имя исполнителя:")
            user_states[user_id]["prompt_message_id"] = msg.message_id
            return
        if text == "🔥 Чарт":
            _send_chart(message.chat.id, user_id=user_id)
            return
        if text == "🎲 Рандом":
            _send_random_track(message.chat.id, user_id=user_id)
            return
        if text == "⭐ Избранное":
            _send_favorites(message.chat.id, user_id)
            return
        if text == "🕘 История":
            _send_history(message.chat.id, user_id, page=0)
            return
        if text == "✨ Рекомендации":
            _send_recommend(message.chat.id, user_id)
            return
        if text == "📊 Статистика":
            _send_stats(message.chat.id, user_id)
            return
        if text == "⚙ Настройки":
            _send_settings(message.chat.id, user_id)
            return
        if text == "ℹ️ О боте":
            _send_info(message.chat.id)
            return
    except Exception as e:
        logging.error(f"Ошибка в handle_private_menu_buttons: {e}")


@bot.message_handler(commands=["artist"])
@group_chat_only
@group_protection
def artist(message):
    try:
        if len(message.text.split()) < 2:
            if message.chat.type != "private":
                bot.reply_to(message, "В группе используйте команду так: /artist имя исполнителя")
                return
            user_states[message.from_user.id] = {"mode": "awaiting_artist"}
            msg = bot.send_message(message.chat.id, "Введите имя исполнителя:", reply_markup=_back_keyboard())
            user_states[message.from_user.id]["prompt_message_id"] = msg.message_id
            return

        artist_name = message.text.split(" ", 1)[1].strip()
        if not artist_name or len(artist_name) < 2:
            bot.reply_to(message, "Слишком короткое имя исполнителя.")
            return

        log_search_query(message.from_user.id, f"artist:{artist_name}")
        msg = bot.send_message(message.chat.id, f"Ищу исполнителя: {artist_name}...")

        try:
            # Use new artist info function
            _send_artist_info(message.chat.id, message.from_user.id, artist_name)
            
            # Clean up the loading message
            try:
                bot.delete_message(message.chat.id, msg.message_id)
            except Exception:
                pass
                
        except Exception as e:
            logging.error(f"Ошибка при поиске исполнителя: {e}")
            try:
                bot.edit_message_text("Произошла ошибка при поиске исполнителя.",
                                      chat_id=message.chat.id, message_id=msg.message_id)
            except Exception:
                bot.send_message(message.chat.id, "Произошла ошибка при поиске исполнителя.")

    except Exception as e:
        logging.error(f"Ошибка в обработчике artist: {e}")
        bot.reply_to(message, "Произошла непредвиденная ошибка.")


@bot.message_handler(commands=["history"])
@group_chat_only
@group_protection
def history_cmd(message):
    try:
        _send_history(message.chat.id, message.from_user.id, page=0)
    except Exception as e:
        logging.error(f"Ошибка в /history: {e}")
        bot.reply_to(message, "Произошла ошибка при получении истории.")


@bot.message_handler(commands=["stats"])
@group_chat_only
@group_protection
def stats_cmd(message):
    try:
        _send_stats(message.chat.id, message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка в /stats: {e}")
        bot.reply_to(message, "Произошла ошибка при получении статистики.")


@bot.message_handler(commands=["fav"])
@group_chat_only
@group_protection
def fav(message):
    try:
        user_id = message.from_user.id
        favorites = get_user_favorites(user_id)
        if not favorites:
            bot.reply_to(message, "В избранном пока пусто.")
            return
        kb = types.InlineKeyboardMarkup()
        for track_id, track_title, artist_name, _added_date in favorites[:20]:
            text = f"{track_title} — {artist_name}".strip()
            kb.add(types.InlineKeyboardButton(text[:64], callback_data=f"track:{track_id}"))
        bot.send_message(message.chat.id, "⭐ Избранное:", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка в /fav: {e}")
        bot.reply_to(message, "Произошла ошибка при получении избранного.")


@bot.message_handler(commands=["settings"])
@group_chat_only
@group_protection
def settings(message):
    try:
        _send_settings(message.chat.id, message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка в /settings: {e}")
        bot.reply_to(message, "Произошла ошибка при открытии настроек.")


@bot.message_handler(commands=["admin"])
@group_chat_only
@group_protection
def admin_cmd(message):
    if not ADMIN_IDS:
        bot.reply_to(message, "Админ-панель не настроена: добавьте ADMIN_IDS в .env")
        return
    if not _is_admin(message.from_user.id):
        bot.reply_to(message, "Доступ запрещён.")
        return
    user_states.pop(message.from_user.id, None)
    bot.reply_to(message, "🛠 Админ-панель:", reply_markup=_admin_menu_keyboard())


@bot.message_handler(commands=["music"])
@group_chat_only
@group_protection
def music_search(message):
    try:
        if len(message.text.split()) < 2:
            if message.chat.type != "private":
                bot.reply_to(message, "В группе используйте команду так: /music ваш запрос")
                return
            user_states[message.from_user.id] = {"mode": "awaiting_search"}
            msg = bot.send_message(message.chat.id, "🔍 Введите название трека или исполнителя для поиска:", reply_markup=_back_keyboard())
            user_states[message.from_user.id]["prompt_message_id"] = msg.message_id
            return

        query = message.text.split(" ", 1)[1].strip()
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        _process_search_query(message, query)
    except Exception as e:
        logging.error(f"Ошибка в music_search: {e}")
        bot.reply_to(message, "Произошла ошибка.")


def _process_search_query(message, query, message_id=None):
    try:
        if not query or len(query) < 2:
            bot.reply_to(message, "❌ Слишком короткий запрос. Введите минимум 2 символа.")
            return

        msg = None
        if message_id:
            try:
                bot.edit_message_text(chat_id=message.chat.id, message_id=message_id, text=f"🔍 Ищу: {query}...")
            except Exception:
                msg = bot.send_message(message.chat.id, f"🔍 Ищу: {query}...")
                message_id = msg.message_id
        else:
            msg = bot.send_message(message.chat.id, f"🔍 Ищу: {query}...")
            message_id = msg.message_id

        try:
            log_search_query(message.from_user.id, query)
            search_results = search_music(query)

            if not search_results:
                bot.edit_message_text("❌ По вашему запросу ничего не найдено.", chat_id=message.chat.id, message_id=message_id)
                return

            s = get_user_settings(message.from_user.id)
            limit = int(s.get("search_limit", 50) or 50)
            limit = max(1, min(50, limit))
            search_results = (search_results or [])[:limit]

            user_states[message.from_user.id] = {
                "search_results": search_results,
                "current_page": 0,
                "last_message_id": message_id,
                "last_search_query": query,
                "search_query_message_id": getattr(message, "message_id", None)
            }
            _show_search_results(message.chat.id, message.from_user.id, 0, message_id)
        except Exception as e:
            logging.error(f"Ошибка при поиске: {e}")
            bot.edit_message_text("❌ Произошла ошибка при поиске.", chat_id=message.chat.id, message_id=message_id)
    except Exception as e:
        logging.error(f"Ошибка в _process_search_query: {e}")
        bot.reply_to(message, "❌ Произошла непредвиденная ошибка.")


def _show_search_results(chat_id, user_id, page, message_id=None):
    try:
        state = user_states.get(user_id, {})
        search_results = state.get("search_results", [])

        if not search_results:
            bot.send_message(chat_id, "❌ Результаты поиска устарели.")
            return

        page = max(0, int(page))
        s = get_user_settings(user_id)
        items_per_page = int(s.get("items_per_page", PER_PAGE) or PER_PAGE)
        items_per_page = max(1, min(10, items_per_page))
        start = page * items_per_page
        end = start + items_per_page
        page_items = search_results[start:end]

        if not page_items:
            bot.send_message(chat_id, "❌ Нет результатов для отображения.")
            return

        kb = types.InlineKeyboardMarkup()
        for i, track in enumerate(page_items, start=start + 1):
            if not track or not track.id:
                continue
            track_title = track.title or "Без названия"
            artist_name = track.artist_name() if hasattr(track, "artist_name") else "Неизвестный"
            
            # Use chart position if available, otherwise numbered list
            if hasattr(track, "chart_position") and track.chart_position:
                button_text = f"{track.chart_position}{track_title} — {artist_name}"[:64]
            else:
                button_text = f"{i}. {track_title} — {artist_name}"[:64]
            
            kb.add(types.InlineKeyboardButton(button_text, callback_data=f"track:{track.id}"))

        nav_buttons = []
        if start > 0:
            nav_buttons.append(types.InlineKeyboardButton("⬅ Назад", callback_data=f"search_page:{page-1}"))
        if end < len(search_results):
            nav_buttons.append(types.InlineKeyboardButton("Далее ➡", callback_data=f"search_page:{page+1}"))
        if nav_buttons:
            kb.row(*nav_buttons)
        kb.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="nav:main"))

        search_query = state.get("last_search_query", "Результаты")
        message_text = f"🔍 Результаты по запросу: {search_query}\nСтраница {page + 1}"

        result_message_id = None
        if message_id:
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message_text, reply_markup=kb)
                result_message_id = message_id
            except Exception:
                sent = bot.send_message(chat_id, message_text, reply_markup=kb)
                result_message_id = sent.message_id
        else:
            sent = bot.send_message(chat_id, message_text, reply_markup=kb)
            result_message_id = sent.message_id

        if user_id in user_states:
            user_states[user_id]["current_page"] = page
            if result_message_id:
                user_states[user_id]["last_message_id"] = result_message_id

    except Exception as e:
        logging.error(f"Ошибка в _show_search_results: {e}")
        bot.send_message(chat_id, "❌ Ошибка при отображении результатов.")


def _cleanup_search_messages(chat_id, user_id):
    state = user_states.get(user_id, {})
    msg_ids = set()
    for k in ("search_query_message_id", "last_message_id"):
        mid = state.get(k)
        if mid:
            try:
                msg_ids.add(int(mid))
            except Exception:
                pass
    for mid in msg_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass
    for k in ("search_results", "current_page", "last_message_id", "last_search_query", "search_query_message_id"):
        state.pop(k, None)
    if state:
        user_states[user_id] = state
    else:
        user_states.pop(user_id, None)


# ─── Awaiting input handlers ─────────────────────────────────────────────────

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get("mode") == "awaiting_search")
@group_protection
def handle_search_query(message):
    try:
        if message.chat.type != "private":
            return
            
        # Проверяем подписку
        if not _check_subscription(message.from_user.id, message.chat.id):
            _send_subscription_request(message.chat.id)
            return
            
        query = (message.text or "").strip()
        if not query:
            bot.reply_to(message, "❌ Запрос не может быть пустым.")
            return

        state = user_states.get(message.from_user.id, {})
        prompt_message_id = state.get("prompt_message_id")

        user_states.get(message.from_user.id, {}).pop("mode", None)
        user_states.get(message.from_user.id, {}).pop("prompt_message_id", None)

        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        _process_search_query(message, query, message_id=prompt_message_id)
    except Exception as e:
        logging.error(f"Ошибка в handle_search_query: {e}")
        bot.reply_to(message, "❌ Произошла ошибка.")


@bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get("mode") == "awaiting_artist")
@group_protection
def handle_artist_query(message):
    try:
        if message.chat.type != "private":
            return
            
        # Проверяем подписку
        if not _check_subscription(message.from_user.id, message.chat.id):
            _send_subscription_request(message.chat.id)
            return
            
        artist_name = (message.text or "").strip()
        if not artist_name or len(artist_name) < 2:
            bot.reply_to(message, "❌ Слишком короткое имя исполнителя.")
            return

        state = user_states.get(message.from_user.id, {})
        prompt_message_id = state.get("prompt_message_id")

        user_states.get(message.from_user.id, {}).pop("mode", None)
        user_states.get(message.from_user.id, {}).pop("prompt_message_id", None)

        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        log_search_query(message.from_user.id, f"artist:{artist_name}")
        chat_id = message.chat.id

        result_message_id = None
        if prompt_message_id:
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=prompt_message_id, text=f"Ищу исполнителя: {artist_name}...")
                result_message_id = prompt_message_id
            except Exception:
                sent = bot.send_message(chat_id, f"Ищу исполнителя: {artist_name}...")
                result_message_id = sent.message_id
        else:
            sent = bot.send_message(chat_id, f"Ищу исполнителя: {artist_name}...")
            result_message_id = sent.message_id

        # Use artist tracks search instead of full artist info
        tracks = get_artist_tracks(artist_name)
        if not tracks:
            _send_or_edit(call or chat_id, f"Не удалось найти треки исполнителя: {artist_name}", reply_markup=_back_keyboard())
            return
        
        track_ids = [str(t.id) for t in tracks if t.id]
        _store_list(message.from_user.id, "artist", track_ids, f"🎤 {artist_name}:")
        _send_list_page(chat_id, message.from_user.id, "artist", page=0, call=call)
        
        # Clean up the loading message
        if result_message_id:
            try:
                bot.delete_message(chat_id, result_message_id)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Ошибка в handle_artist_query: {e}", exc_info=True)
        try:
            bot.reply_to(message, "❌ Произошла ошибка при поиске исполнителя.")
        except Exception:
            pass


# ─── Search page handler ─────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def handle_check_subscription(call):
    """Обработчик кнопки 'Я подписался'"""
    try:
        bot.answer_callback_query(call.id)
        
        if _check_subscription(call.from_user.id, call.message.chat.id):
            # Подписка подтверждена, отправляем приветствие
            bot.edit_message_text(
                "✅ **Спасибо за подписку!**\n\n"
                "Теперь вы можете использовать все функции бота.\n\n"
                "🎵 **Добро пожаловать в Zvonko Music Bot!**",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_main_menu_keyboard(),
                parse_mode="Markdown"
            )
        else:
            # Пользователь все еще не подписан
            bot.answer_callback_query(call.id, "❌ Вы не подписаны на канал!", show_alert=True)
    except Exception as e:
        logging.error(f"Ошибка в handle_check_subscription: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("search_page:"))
def handle_search_page(call):
    try:
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        user_id = call.from_user.id
        page = int(call.data.split(":")[1])
        state = user_states.get(user_id, {})
        if "last_message_id" not in state:
            return
        _run_bg(_show_search_results, call.message.chat.id, user_id, page, state["last_message_id"])
    except Exception as e:
        logging.error(f"Ошибка в handle_search_page: {e}")


# ─── Inline mode ─────────────────────────────────────────────────────────────

def _inline_deeplink_keyboard(track_id: str, bot_username: str):
    """Клавиатура с deep-link кнопками для инлайн-сообщений."""
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🎵 Скачать MP3", url=f"https://t.me/{bot_username}?start=play_{track_id}"),
        types.InlineKeyboardButton("🎬 Скачать клип", url=f"https://t.me/{bot_username}?start=clip_{track_id}")
    )
    kb.add(types.InlineKeyboardButton("📝 Текст песни", url=f"https://t.me/{bot_username}?start=lyrics_{track_id}"))
    return kb


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_inline_query(raw_query: str):
    """Парсит инлайн-запрос, определяя режим и поисковый текст.
    Возвращает (mode, query):
        mode: 'music' | 'clip' | 'all'
        query: очищенный поисковый запрос
    """
    q = raw_query.strip()
    lower = q.lower()
    
    # Проверяем целые слова /music и /clip
    parts = lower.split(None, 1)
    if len(parts) >= 2:
        if parts[0] in ("/music", "music"):
            return "music", parts[1].strip()
        if parts[0] in ("/clip", "clip"):
            return "clip", parts[1].strip()
    
    return "all", q


def _build_inline_result(track, mode: str, bot_username: str):
    """Формирует InlineQueryResult для трека в зависимости от режима.
    /music и /clip — InlineQueryResultPhoto (обложка), потом chosen_inline заменит на аудио/видео.
    Обычный — InlineQueryResultArticle с кнопками.
    """
    track_title = track.title or "Без названия"
    artist_name = track.artist_name() if hasattr(track, "artist_name") else "Неизвестный"
    album = getattr(track, "album_name", "") or ""
    duration_ms = getattr(track, "duration_ms", 0) or 0
    duration_str = ""
    if duration_ms > 0:
        minutes = duration_ms // 60000
        seconds = (duration_ms % 60000) // 1000
        duration_str = f"{minutes}:{seconds:02d}"

    desc = artist_name
    if album:
        desc += f" • {album}"
    if duration_str:
        desc += f" ({duration_str})"

    thumb_url = getattr(track, "cover_uri", None) or ""
    if thumb_url and not thumb_url.startswith("http"):
        thumb_url = f"https://{thumb_url}"

    if mode in ("music", "clip"):
        emoji = "🎵" if mode == "music" else "🎬"
        action = "Скачать MP3" if mode == "music" else "Скачать клип"
        loading_text = "⏳ Загрузка MP3..." if mode == "music" else "⏳ Загрузка клипа..."

        msg_text = f"{emoji} <b>{_escape_html(track_title)}</b>\n"
        msg_text += f"👤 {_escape_html(artist_name)}\n"
        if album:
            msg_text += f"💿 {_escape_html(album)}\n"
        if duration_str:
            msg_text += f"⏱ {duration_str}\n"
        msg_text += f"\n{loading_text}"

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⏳ Загрузка...", callback_data=f"noop:{track.id}"))

        full_desc = artist_name
        if album:
            full_desc += f" • {album}"
        if duration_str:
            full_desc += f" • {duration_str}"
        full_desc += f" • {action}"

        return types.InlineQueryResultArticle(
            id=f"{mode}_{track.id}",
            title=f"{emoji} {track_title} — {artist_name}",
            description=full_desc,
            input_message_content=types.InputTextMessageContent(
                message_text=msg_text, parse_mode="HTML"
            ),
            reply_markup=kb,
            thumbnail_url=thumb_url if thumb_url else None
        )

    else:
        # Обычный режим — текстовый список как в поиске
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎵 MP3", callback_data=f"play:{track.id}"),
            types.InlineKeyboardButton("🎬 Клип", callback_data=f"clip:{track.id}")
        )
        kb.add(types.InlineKeyboardButton("📝 Текст", callback_data=f"lyrics:{track.id}"))

        # Формируем текст сообщения как в поиске
        msg_text = f"🎶 <b>{_escape_html(track_title)}</b> — {_escape_html(artist_name)}\n"
        if album:
            msg_text += f"💿 Альбом: {_escape_html(album)}\n"
        if duration_str:
            msg_text += f"⏱ Длительность: {duration_str}\n"
        msg_text += "\nВыберите действие ↓"

        # Описание для списка в пикере — просто текст как в поиске
        search_desc = f"{track_title} — {artist_name}"
        if album:
            search_desc += f" • {album}"
        if duration_str:
            search_desc += f" • {duration_str}"

        return types.InlineQueryResultArticle(
            id=str(track.id),
            title=search_desc,
            description=search_desc,
            input_message_content=types.InputTextMessageContent(
                message_text=msg_text, parse_mode="HTML"
            ),
            reply_markup=kb,
            thumbnail_url=thumb_url if thumb_url else None
        )


@bot.inline_handler(lambda query: len(query.query.strip()) >= 2)
def inline_search(inline_query):
    """Обработка инлайн-запросов:
    @bot запрос       — все действия (MP3 / Клип / Текст)
    @bot /music запрос — только скачать MP3
    @bot /clip запрос  — только скачать клип
    """
    try:
        mode, query_text = _parse_inline_query(inline_query.query)
        user_id = inline_query.from_user.id
        
        logging.info(f"Inline query: mode={mode}, query='{query_text}'")

        if not query_text or len(query_text) < 2:
            bot.answer_inline_query(
                inline_query.id, [], cache_time=10,
                switch_pm_text="Введите название трека после команды",
                switch_pm_parameter="start"
            )
            return

        try:
            upsert_tg_user(getattr(inline_query, "from_user", None))
        except Exception:
            pass

        try:
            log_search_query(user_id, query_text)
        except Exception:
            pass

        search_results = search_music(query_text)
        if not search_results:
            bot.answer_inline_query(
                inline_query.id, [], cache_time=30,
                switch_pm_text="Ничего не найдено. Открыть бота?",
                switch_pm_parameter="start"
            )
            return

        bot_username = BOT_USERNAME or "ZvonkoMusicbot"
        results = []
        
        # Добавляем подсказку для обычных запросов
        if mode == "all":
            help_result = types.InlineQueryResultArticle(
                id="help_inline",
                title="📖 Как пользоваться инлайн-режимом",
                description="Быстрая загрузка музыки и клипов",
                input_message_content=types.InputTextMessageContent(
                    message_text=(
                        "🎵 Инлайн-режим ZvonkoMusicBot\n\n"
                        "Быстрые команды:\n"
                        "• @ZvonkoMusicbot /music Название — скачать только MP3\n"
                        "• @ZvonkoMusicbot /clip Название — скачать только клип\n"
                        "• Поддержка - @oxyench1k\n"
                    ),
                    parse_mode="HTML"
                ),
                thumbnail_url="https://i.imgur.com/8QJhY3u.png"
            )
            results.append(help_result)
        
        for track in search_results[:9 if mode == "all" else 10]:  # 9 треков + подсказка
            if not track or not track.id:
                continue
            results.append(_build_inline_result(track, mode, bot_username))

        bot.answer_inline_query(inline_query.id, results, cache_time=30)

    except Exception as e:
        logging.error(f"Ошибка в inline_search: {e}")
        try:
            bot.answer_inline_query(inline_query.id, [], cache_time=5)
        except Exception:
            pass


@bot.chosen_inline_handler(func=lambda chosen: True)
def on_chosen_inline(chosen):
    """Срабатывает при выборе результата из инлайн-списка.
    Для music_/clip_ результатов — скачивает и заменяет фото на аудио/видео.
    """
    try:
        result_id = chosen.result_id
        user_id = chosen.from_user.id
        inline_msg_id = chosen.inline_message_id

        logging.info(f"chosen_inline: result_id={result_id}, user={user_id}, msg_id={inline_msg_id}")

        if not inline_msg_id:
            logging.warning("chosen_inline: нет inline_message_id")
            return

        if result_id.startswith("music_"):
            track_id = result_id[6:]
            _run_bg(_handle_inline_music_download, inline_msg_id, user_id, track_id)
        elif result_id.startswith("clip_"):
            track_id = result_id[5:]
            _run_bg(_handle_inline_clip_download, inline_msg_id, user_id, track_id)

    except Exception as e:
        logging.error(f"Ошибка в on_chosen_inline: {e}", exc_info=True)



@bot.inline_handler(lambda query: len(query.query.strip()) < 2)
def inline_empty(inline_query):
    """Обработка пустого или слишком короткого инлайн-запроса."""
    try:
        bot.answer_inline_query(
            inline_query.id, [], cache_time=60,
            switch_pm_text="Поиск: запрос | /music | /clip",
            switch_pm_parameter="start"
        )
    except Exception:
        pass


# ─── Main callback handler ───────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    try:
        data = call.data or ""
        user_id = call.from_user.id

        try:
            upsert_tg_user(getattr(call, "from_user", None))
        except Exception:
            pass

        # Обработка callback из инлайн-сообщений (call.message = None)
        if call.message is None:
            bot_username = BOT_USERNAME or "ZvonkoMusicbot"
            if data.startswith("play:"):
                track_id = data.split(":", 1)[1]
                link = f"https://t.me/{bot_username}?start=play_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Нажмите кнопку ниже, чтобы скачать трек",
                    show_alert=True
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            elif data.startswith("clip:"):
                track_id = data.split(":", 1)[1]
                link = f"https://t.me/{bot_username}?start=clip_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Нажмите кнопку ниже, чтобы скачать клип",
                    show_alert=True
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            elif data.startswith("lyrics:"):
                track_id = data.split(":", 1)[1]
                link = f"https://t.me/{bot_username}?start=lyrics_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Нажмите кнопку ниже, чтобы прочитать текст",
                    show_alert=True
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            else:
                bot.answer_callback_query(call.id)
            return

        # Проверяем подписку только для личных сообщений
        if data != "check_subscription" and not data.startswith("admin:") and call.message and call.message.chat.type == "private":
            if not _check_subscription(user_id, call.message.chat.id):
                _send_subscription_request(call.message.chat.id)
                return

        # ── Admin callbacks ──
        if data.startswith("admin:"):
            if not _is_admin(user_id):
                try:
                    bot.answer_callback_query(call.id, "Доступ запрещён")
                except Exception:
                    pass
                return

            if data == "admin:open":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, "🛠 Админ-панель:", reply_markup=_admin_menu_keyboard())
                return

            if data == "admin:stats":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, admin_db_stats_text(), reply_markup=_admin_menu_keyboard())
                return

            if data == "admin:service":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, _admin_service_text(), reply_markup=_admin_menu_keyboard())
                return

            if data == "admin:top":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, admin_top_users_text(10), reply_markup=_admin_menu_keyboard(), parse_mode="HTML")
                return

            if data == "admin:recent":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, admin_recent_text(10), reply_markup=_admin_menu_keyboard(), parse_mode="HTML")
                return

            if data == "admin:user:prompt":
                user_states[user_id] = {"mode": "admin_await_user_id"}
                bot.answer_callback_query(call.id)
                _send_or_edit(call, "Введите user_id для управления (или напишите 'отмена'):", reply_markup=_admin_menu_keyboard())
                return

            if data.startswith("admin:user:"):
                _p = data.split(":")
                if len(_p) >= 3 and _p[2].isdigit():
                    target_user_id = int(_p[2])

                    if len(_p) == 3:
                        s = get_user_settings(target_user_id)
                        bot.answer_callback_query(call.id)
                        _send_or_edit(call, admin_user_overview_text(target_user_id, s), reply_markup=_admin_user_keyboard(target_user_id, s), parse_mode="HTML")
                        return

                    if len(_p) == 4 and _p[3] == "recent":
                        kb = types.InlineKeyboardMarkup()
                        kb.row(
                            types.InlineKeyboardButton("⬅ Назад", callback_data=f"admin:user:{target_user_id}"),
                            types.InlineKeyboardButton("🔄 Обновить", callback_data=f"admin:user:{target_user_id}:recent")
                        )
                        bot.answer_callback_query(call.id)
                        _send_or_edit(call, admin_user_recent_text(target_user_id, limit=10), reply_markup=kb, parse_mode="HTML")
                        return

                    if len(_p) == 4 and _p[3] == "export":
                        bot.answer_callback_query(call.id)
                        try:
                            fav_rows, search_rows, play_rows = admin_get_user_data_for_export(target_user_id)
                            _db_admin_send_csv(bot, call.message.chat.id, f"user_{target_user_id}_favorites.csv",
                                               ["track_id", "track_title", "artist_name", "added_date"], fav_rows)
                            _db_admin_send_csv(bot, call.message.chat.id, f"user_{target_user_id}_search_history.csv",
                                               ["query", "created_at"], search_rows)
                            _db_admin_send_csv(bot, call.message.chat.id, f"user_{target_user_id}_play_history.csv",
                                               ["track_id", "track_title", "artist_name", "created_at"], play_rows)
                            kb = types.InlineKeyboardMarkup()
                            kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data=f"admin:user:{target_user_id}"))
                            _send_or_edit(call, f"📤 Экспорт для <code>{target_user_id}</code> отправлен.", reply_markup=kb, parse_mode="HTML")
                        except Exception as e:
                            logging.error(f"Ошибка экспорта CSV: {e}")
                            kb = types.InlineKeyboardMarkup()
                            kb.add(types.InlineKeyboardButton("⬅ Назад", callback_data=f"admin:user:{target_user_id}"))
                            _send_or_edit(call, f"Не удалось сделать экспорт: {e}", reply_markup=kb)
                        return

            if data.startswith("admin:set:"):
                _p = data.split(":")
                if len(_p) >= 4 and _p[2].isdigit():
                    target_user_id = int(_p[2])
                    s = get_user_settings(target_user_id)
                    key = _p[3]
                    if key == "reply_menu":
                        s["reply_menu"] = not s.get("reply_menu", True)
                    elif key == "auto_download":
                        s["auto_download"] = not s.get("auto_download", False)
                    elif key == "show_covers":
                        s["show_covers"] = not s.get("show_covers", True)
                    elif key == "tips":
                        s["show_tips"] = not s.get("show_tips", True)
                    elif key == "quality" and len(_p) >= 5:
                        s["download_quality"] = _p[4]
                    elif key == "page" and len(_p) >= 5:
                        try:
                            s["items_per_page"] = int(_p[4])
                        except Exception:
                            pass
                    elif key == "slimit" and len(_p) >= 5:
                        try:
                            s["search_limit"] = int(_p[4])
                        except Exception:
                            pass
                    update_user_settings(target_user_id, s)
                    s2 = get_user_settings(target_user_id)
                    bot.answer_callback_query(call.id, "Ок")
                    _send_or_edit(call, admin_user_overview_text(target_user_id, s2), reply_markup=_admin_user_keyboard(target_user_id, s2), parse_mode="HTML")
                    return

            if data.startswith("admin:clear_confirm:"):
                _p = data.split(":")
                if len(_p) == 4 and _p[2].isdigit():
                    target_user_id = int(_p[2])
                    what = _p[3]
                    labels = {"fav": "избранное", "hist": "историю поиска", "plays": "прослушивания"}
                    label = labels.get(what, what)
                    bot.answer_callback_query(call.id)
                    _send_or_edit(
                        call,
                        f"Подтвердите очистку: {label} для пользователя {_admin_user_label_html(target_user_id)}?",
                        reply_markup=_admin_confirm_keyboard(
                            ok_cb=f"admin:clear:{target_user_id}:{what}",
                            cancel_cb=f"admin:user:{target_user_id}"
                        ),
                        parse_mode="HTML"
                    )
                    return

            if data.startswith("admin:clear:"):
                _p = data.split(":")
                if len(_p) == 4 and _p[2].isdigit():
                    target_user_id = int(_p[2])
                    what = _p[3]
                    try:
                        admin_clear_user_data(target_user_id, what)
                        s2 = get_user_settings(target_user_id)
                        bot.answer_callback_query(call.id, "Готово")
                        _send_or_edit(call, admin_user_overview_text(target_user_id, s2), reply_markup=_admin_user_keyboard(target_user_id, s2), parse_mode="HTML")
                    except Exception as e:
                        logging.error(f"Ошибка очистки данных: {e}")
                        bot.answer_callback_query(call.id, "Ошибка")
                    return

            if data == "admin:db:backup":
                bot.answer_callback_query(call.id)
                try:
                    path = admin_backup_db_path()
                    with open(path, "rb") as f:
                        bot.send_document(call.message.chat.id, f)
                    _send_or_edit(call, "Бэкап создан.", reply_markup=_admin_menu_keyboard())
                except Exception as e:
                    logging.error(f"Ошибка бэкапа: {e}")
                    _send_or_edit(call, f"Не удалось: {e}", reply_markup=_admin_menu_keyboard())
                return

            if data == "admin:db:vacuum":
                bot.answer_callback_query(call.id)
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("VACUUM")
                    conn.close()
                    _send_or_edit(call, "VACUUM выполнен.", reply_markup=_admin_menu_keyboard())
                except Exception as e:
                    logging.error(f"Ошибка VACUUM: {e}")
                    _send_or_edit(call, f"Не удалось: {e}", reply_markup=_admin_menu_keyboard())
                return

            bot.answer_callback_query(call.id)
            return

        # ── Back to track choice menu ──
        if data.startswith("back_to_track_choice:"):
            track_id = data.split(":", 1)[1]
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            track = get_track_info(track_id)
            if not track:
                bot.send_message(call.message.chat.id, "Трек не найден.")
                return
            title = track.title or "Трек"
            artist = track.artist_name() if hasattr(track, "artist_name") else ""
            full_name = f"{title} — {artist}".strip(" —")
            
            _send_track_selection_with_cover(call.message.chat.id, track_id, full_name, user_id)
            return

        # ── Back to list ──
        if data.startswith("back_to_list:"):
            list_key = data.split(":", 1)[1]
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            if list_key == "history":
                # For history, we need to get the current page from state
                state = user_states.get(user_id, {})
                # Default to page 0 for history
                _send_history(call.message.chat.id, user_id, page=0, call=call)
            else:
                _send_list_page(call.message.chat.id, user_id, list_key, page=0, call=call)
            return

        # ── Navigation ──
        if data.startswith("nav:"):
            section = data.split(":", 1)[1]
            user_states.pop(user_id, None)

            if section == "main":
                bot.answer_callback_query(call.id)
                _send_or_edit(call, "Главное меню:", reply_markup=_main_menu_keyboard())
                return
            if section == "chart":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_chart, call.message.chat.id, None, call)
                return
            if section == "random":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_random_track, call.message.chat.id, user_id, call)
                return
            if section == "fav":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_favorites, call.message.chat.id, user_id, call)
                return
            if section == "settings":
                bot.answer_callback_query(call.id)
                _send_settings(call.message.chat.id, user_id, call=call)
                return
            if section == "history":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_history, call.message.chat.id, user_id, 0, call)
                return
            if section == "recommend":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_recommend, call.message.chat.id, user_id, call)
                return
            if section == "stats":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_stats, call.message.chat.id, user_id, call)
                return
            if section == "info":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _send_info(call.message.chat.id, call=call)
                return
            if section == "search":
                bot.answer_callback_query(call.id)
                if call.message and call.message.chat and call.message.chat.type != "private":
                    _send_or_edit(call, "В группе используйте: /music ваш запрос", reply_markup=_back_keyboard())
                    return
                user_states[user_id] = {"mode": "awaiting_search"}
                _send_or_edit(call, "🔍 Введите запрос для поиска:", reply_markup=_back_keyboard())
                try:
                    user_states[user_id]["prompt_message_id"] = call.message.message_id
                except Exception:
                    pass
                return
            if section == "artist":
                bot.answer_callback_query(call.id)
                if call.message and call.message.chat and call.message.chat.type != "private":
                    _send_or_edit(call, "В группе используйте: /artist имя исполнителя", reply_markup=_back_keyboard())
                    return
                user_states[user_id] = {"mode": "awaiting_artist"}
                _send_or_edit(call, "Введите имя исполнителя:", reply_markup=_back_keyboard())
                try:
                    user_states[user_id]["prompt_message_id"] = call.message.message_id
                except Exception:
                    pass
                return

            if section == "fav":
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_favorites, call.message.chat.id, user_id, call)
                return

            if section == "settings":
                bot.answer_callback_query(call.id)
            return

        # ── Artist albums navigation ──
        if data == "artist_albums":
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            _run_bg(_send_albums_list, call.message.chat.id, user_id, call)
            return

        # ── Album tracks navigation ──
        if data.startswith("album:"):
            album_id = data.split(":", 1)[1]
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            _run_bg(_send_album_tracks, call.message.chat.id, user_id, album_id, call)
            return

        # ── Track choice menu (audio vs clip) ──
        if data.startswith("track:"):
            track_id = data.split(":", 1)[1]
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            try:
                state = user_states.get(user_id, {})
                if isinstance(state, dict) and state.get("search_results"):
                    _cleanup_search_messages(call.message.chat.id, user_id)
            except Exception:
                pass
            track = get_track_info(track_id)
            if not track:
                bot.send_message(call.message.chat.id, "Трек не найден.")
                return
            title = track.title or "Трек"
            artist = track.artist_name() if hasattr(track, "artist_name") else ""
            full_name = f"{title} — {artist}".strip(" —")
            
            _send_track_selection_with_cover(call.message.chat.id, track_id, full_name, user_id)
            return

        # ── Play track (audio download) ──
        if data.startswith("play:"):
            track_id = data.split(":", 1)[1]
            if call.message is None:
                # Инлайн-сообщение: перенаправляем в ЛС через deep-link
                bot_username = BOT_USERNAME or "ZvonkoMusicbot"
                link = f"https://t.me/{bot_username}?start=play_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Перейдите в личные сообщения для скачивания",
                    show_alert=False
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            else:
                # Групповое или личное сообщение: скачиваем и отправляем в тот же чат
                try:
                    bot.answer_callback_query(call.id, "Загружаю трек...")
                except Exception:
                    pass
                _run_bg(_send_track_by_id, call.message.chat.id, user_id, track_id)
            return

        # ── Clip download (video) ──
        if data.startswith("clip:"):
            track_id = data.split(":", 1)[1]
            if call.message is None:
                # Инлайн-сообщение: перенаправляем в ЛС через deep-link
                bot_username = BOT_USERNAME or "ZvonkoMusicbot"
                link = f"https://t.me/{bot_username}?start=clip_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Перейдите в личные сообщения для скачивания",
                    show_alert=False
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            else:
                # Групповое или личное сообщение: скачиваем и отправляем в тот же чат
                try:
                    bot.answer_callback_query(call.id, "Загружаю клип...")
                except Exception:
                    pass
                _run_bg(_send_track_clip, call.message.chat.id, user_id, track_id)
            return

        # ── Lyrics ──
        if data == "lyrics_close":
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            return

        if data.startswith("lyrics:"):
            track_id = data.split(":", 1)[1]
            if call.message is None:
                # Инлайн-сообщение: перенаправляем в ЛС через deep-link
                bot_username = BOT_USERNAME or "ZvonkoMusicbot"
                link = f"https://t.me/{bot_username}?start=lyrics_{track_id}"
                bot.answer_callback_query(
                    call.id,
                    "Перейдите в личные сообщения для просмотра текста",
                    show_alert=False
                )
                try:
                    bot.edit_message_reply_markup(
                        inline_message_id=call.inline_message_id,
                        reply_markup=_inline_deeplink_keyboard(track_id, bot_username)
                    )
                except Exception:
                    pass
            else:
                # Групповое или личное сообщение: отправляем текст в тот же чат
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_track_lyrics, call.message.chat.id, user_id, track_id)
            return

        # ── Pagination ──
        if data.startswith("page:"):
            _p = data.split(":")
            if len(_p) >= 3:
                key = _p[1]
                page = _p[2]
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _run_bg(_send_list_page, call.message.chat.id, user_id, key, page, call)
                return

        if data.startswith("hpage:"):
            page = data.split(":", 1)[1]
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            _run_bg(_send_history, call.message.chat.id, user_id, page, call)
            return

        if data.startswith("hq:"):
            idx = int(data.split(":", 1)[1])
            queries = user_states.get(user_id, {}).get("history_queries", [])
            if 0 <= idx < len(queries):
                q = queries[idx]
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    pass

                def _do_hq():
                    log_search_query(user_id, q)
                    results = search_music(q)
                    if not results:
                        try:
                            bot.send_message(call.message.chat.id, "Ничего не найдено")
                        except Exception:
                            pass
                        return
                    track_ids = [str(t.id) for t in results if t.id]
                    _store_list(user_id, "search", track_ids, f"🔍 Результаты: {q}")
                    _send_list_page(call.message.chat.id, user_id, "search", page=0, call=call)

                _run_bg(_do_hq)
                return
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return

        # ── Favorites ──
        if data.startswith("fav:"):
            track_id = data.split(":", 1)[1]
            track = get_track_info(track_id)
            if not track:
                bot.answer_callback_query(call.id, "Трек не найден")
                return
            tid = str(track.id)
            artist_name = track.artist_name() if hasattr(track, "artist_name") else ""
            ok = add_to_favorites(user_id, tid, track.title or "", artist_name)
            bot.answer_callback_query(call.id, "Добавлено" if ok else "Ошибка")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=_build_track_actions_keyboard(tid, user_id))
            except Exception:
                pass
            return

        if data.startswith("unfav:"):
            track_id = data.split(":", 1)[1]
            ok = remove_from_favorites(user_id, str(track_id))
            bot.answer_callback_query(call.id, "Удалено" if ok else "Ошибка")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=_build_track_actions_keyboard(str(track_id), user_id))
            except Exception:
                pass
            return

        # ── Settings toggles ──
        if data == "set:auto_download":
            s = get_user_settings(user_id)
            s["auto_download"] = not s.get("auto_download", False)
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data == "set:reply_menu":
            s = get_user_settings(user_id)
            s["reply_menu"] = not s.get("reply_menu", True)
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            if call.message and call.message.chat and call.message.chat.type == "private":
                if s.get("reply_menu", True):
                    bot.send_message(call.message.chat.id, "Reply-меню включено.", reply_markup=_reply_menu_keyboard())
                else:
                    bot.send_message(call.message.chat.id, "Reply-меню выключено.", reply_markup=types.ReplyKeyboardRemove())
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data == "set:show_covers":
            s = get_user_settings(user_id)
            s["show_covers"] = not s.get("show_covers", True)
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data == "set:tips":
            s = get_user_settings(user_id)
            s["show_tips"] = not s.get("show_tips", True)
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data == "set:lyrics_btn":
            s = get_user_settings(user_id)
            s["show_lyrics_button"] = not s.get("show_lyrics_button", True)
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data.startswith("set:quality:"):
            quality = data.split(":", 2)[2]
            s = get_user_settings(user_id)
            s["download_quality"] = quality
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data.startswith("set:page:"):
            try:
                per_page = int(data.split(":", 2)[2])
            except Exception:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            per_page = max(1, min(10, per_page))
            s = get_user_settings(user_id)
            s["items_per_page"] = per_page
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data.startswith("set:slimit:"):
            try:
                limit = int(data.split(":", 2)[2])
            except Exception:
                bot.answer_callback_query(call.id, "Ошибка")
                return
            limit = max(1, min(50, limit))
            s = get_user_settings(user_id)
            s["search_limit"] = limit
            update_user_settings(user_id, s)
            bot.answer_callback_query(call.id, "Ок")
            _send_settings(call.message.chat.id, user_id, call=call)
            return

        if data == "noop":
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Ошибка в callbacks: {e}")
        try:
            bot.answer_callback_query(call.id, "Ошибка")
        except Exception:
            pass


# ─── Catch-all handler ───────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: True)
@group_chat_only
def echo_all(message):
    try:
        if message.chat.type != "private":
            return

        user_id = message.from_user.id
        state = user_states.get(user_id, {})
        if state.get("mode") in ("awaiting_search", "awaiting_artist"):
            return

        text = (message.text or "").strip()

        reply_markup = _main_menu_keyboard()
        if message.chat.type == "private":
            s = get_user_settings(user_id)
            if s.get("reply_menu", True):
                reply_markup = _reply_menu_keyboard()

        if message.chat.type == "private":
            s = get_user_settings(user_id)
            if s.get("reply_menu", True) and text in {
                "🔎 Поиск", "🔥 Чарт", "🎲 Рандом", "🎤 Исполнитель", "⭐ Избранное",
                "🕘 История", "✨ Рекомендации", "📊 Статистика", "⚙ Настройки", "🏠 Меню"
            }:
                return

        if text.startswith("/"):
            cmd_token = text.split()[0].lower()
            cmd_base = cmd_token.split("@", 1)[0]
            cmd_at = cmd_token.split("@", 1)[1] if "@" in cmd_token else None
            if cmd_at and BOT_USERNAME and cmd_at != BOT_USERNAME.lower():
                return
            if cmd_base in {"/start", "/help", "/menu", "/music", "", "", "/artist", "/history", "", "/stats", "/fav", "/settings"}:
                return
            bot.reply_to(
                message,
                "Неизвестная команда.\n\n"
                "Доступные команды:\n"
                "• <code>/music</code> — поиск треков\n"
                "• <code>/artist</code> — треки исполнителя\n"
                "• <code></code> — новинки\n"
                "• <code></code> — случайный трек\n"
                "• <code>/history</code> — история\n"
                "• <code>/fav</code> — избранное\n"
                "• <code>/settings</code> — настройки",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            return

        if not text:
            return

        if len(text) < 2:
            bot.reply_to(message, "Слишком короткий запрос.", reply_markup=reply_markup)
            return

        bot.reply_to(
            message,
            "Хочешь найти трек?\n\n"
            f"<code>/music {text[:50]}</code>" + ("..." if len(text) > 50 else "") + "\n\n"
            "Или треки конкретного исполнителя:\n"
            f"<code>/artist {text.split()[0]}</code>",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Ошибка в echo_all: {e}")


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    max_retries = 5
    retry_count = 0
    retry_delay = 5

    while retry_count < max_retries:
        try:
            logging.info("=" * 50)
            logging.info(f"Попытка запуска {retry_count + 1}/{max_retries}")
            logging.info("=" * 50)

            logging.info("Проверка подключения к API Telegram...")
            bot_info = bot.get_me()
            if not bot_info:
                raise RuntimeError("Не удалось получить информацию о боте.")

            logging.info(f"Бот авторизован как @{bot_info.username} (ID: {bot_info.id})")

            try:
                bot.delete_webhook()
            except Exception as e:
                logging.warning(f"Не удалось удалить вебхук: {e}")

            logging.info("Запуск polling...")
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                logger_level=logging.INFO,
                skip_pending=True,
                allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"]
            )
            break

        except KeyboardInterrupt:
            logging.info("Бот остановлен пользователем")
            break

        except Exception as e:
            retry_count += 1
            logging.error(f"Ошибка в работе бота (попытка {retry_count}/{max_retries}): {e}", exc_info=True)

            if retry_count >= max_retries:
                logging.error("Достигнуто максимальное количество попыток.")
                break

            logging.info(f"Повторная попытка через {retry_delay} секунд...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

    logging.info("Бот завершил работу")
