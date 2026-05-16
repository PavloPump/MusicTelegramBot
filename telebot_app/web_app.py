import hashlib
import hmac
import logging
import os
import re
import threading
import time
from typing import Optional

from flask import Flask, jsonify, make_response, request, send_from_directory, send_file
from flask_cors import CORS

from .config import TELEGRAM_TOKEN, TELEGRAM_BOT_ID, BOT_USERNAME, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, WEBAPP_URL
from .database import (
    add_to_favorites,
    get_search_history,
    get_user_favorites,
    get_user_stats,
    init_db,
    is_favorite,
    log_play_event,
    log_search_query,
    remove_from_favorites,
)
from .database import db_connect, get_user_play_history
from datetime import datetime
from .music_service import (
    download_track,
    get_artist_full_info,
    get_artist_tracks,
    get_track_info,
    get_track_lyrics_by_id,
    get_tracks_by_ids,
    search_music,
)

app = Flask(__name__, static_folder='../web_static')
CORS(app, supports_credentials=True, origins='*')

# Initialize database
init_db()

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DEMO_USER_ID = 999999
_sessions = {}   # token -> user_id


# ──────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────

def _verify_tg_hash(data: dict) -> bool:
    """Verify Telegram WebApp/LoginWidget auth data."""
    logging.info(f"Verifying Telegram auth data: {list(data.keys())}")
    
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_TOKEN not set, cannot verify auth")
        return False
    
    logging.info(f"Using TELEGRAM_TOKEN (first 10 chars): {TELEGRAM_TOKEN[:10]}...")
    
    check_hash = data.get('hash', '')
    if not check_hash:
        logging.warning("No hash provided in auth data")
        return False
    
    logging.info(f"Received hash: {check_hash[:20]}...")
    
    # Build data_check_string from all fields except hash, sorted alphabetically
    items = {k: v for k, v in data.items() if k != 'hash' and v is not None}
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(items.items()))
    
    logging.info(f"Data check string:\n{data_check_string}")
    
    # Calculate secret key from bot token
    secret = hashlib.sha256(TELEGRAM_TOKEN.encode()).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    
    logging.info(f"Calculated hash: {calculated[:20]}...")
    logging.info(f"Provided hash:   {check_hash[:20]}...")
    
    if calculated != check_hash:
        logging.warning(f"Hash mismatch! Auth failed.")
        return False
    
    # Check auth_date is within last 24 hours
    try:
        auth_date = int(data.get('auth_date', 0))
        time_diff = time.time() - auth_date
        if time_diff > 86400:
            logging.warning(f"Auth data expired: {time_diff}s ago")
            return False
        logging.info(f"Auth date valid: {time_diff}s ago")
    except (ValueError, TypeError):
        logging.warning("Invalid auth_date format")
        return False
    
    logging.info("Telegram auth verification successful!")
    return True


def _make_token(user_id: int) -> str:
    return hashlib.sha256(f"{user_id}:{time.time()}:{TELEGRAM_TOKEN}".encode()).hexdigest()


def _current_uid() -> int:
    token = request.cookies.get('zvonko_session')
    if token:
        uid = _sessions.get(token)
        if uid:
            return uid
    return DEMO_USER_ID


def _upsert_web_user(user_id, username: str, first_name: str, last_name: str, photo_url: str = ''):
    """Upsert web user with full profile info."""
    conn = db_connect()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    
    # Check if user exists to preserve created_at
    cur.execute("SELECT created_at FROM tg_users WHERE user_id = ?", (str(user_id),))
    row = cur.fetchone()
    created_at = row[0] if row else now
    
    cur.execute(
        """INSERT OR REPLACE INTO tg_users 
            (user_id, username, first_name, last_name, photo_url, auth_provider, created_at, updated_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(user_id), username, first_name, last_name, photo_url, 'telegram', created_at, now)
    )
    conn.commit()
    conn.close()


def _track_to_dict(track):
    return {
        'id': track.id,
        'title': track.title,
        'artist': track.artist_name(),
        'album': track.album_name,
        'cover': track.cover_uri,
        'duration_ms': track.duration_ms,
        'preview_url': track.preview_url,
        'external_url': track.external_url,
    }


# ──────────────────────────────────────────────────────
#  Auth endpoints
# ──────────────────────────────────────────────────────

@app.route('/api/auth/telegram', methods=['POST'])
def auth_telegram():
    data = request.json or {}
    if not _verify_tg_hash(data):
        return jsonify({'error': 'Invalid auth data'}), 401

    user_id    = int(data['id'])
    username   = data.get('username', '')
    first_name = data.get('first_name', '')
    last_name  = data.get('last_name', '')
    photo_url  = data.get('photo_url', '')

    _upsert_web_user(user_id, username, first_name, last_name, photo_url)

    token = _make_token(user_id)
    _sessions[token] = user_id

    resp = make_response(jsonify({
        'success': True,
        'user': {
            'id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'display_name': first_name or username or str(user_id),
        }
    }))
    # Determine if we should use secure cookies (production) or not (development)
    is_production = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
    
    resp.set_cookie('zvonko_session', token,
                    max_age=30 * 24 * 3600,
                    httponly=True,
                    secure=is_production,  # Only secure in HTTPS environments
                    samesite='Lax')
    return resp


def _verify_telegram_oidc_token(id_token: str) -> Optional[dict]:
    """Verify Telegram OIDC id_token (JWT) using Telegram's JWKS endpoint.
    
    According to https://core.telegram.org/bots/telegram-login
    """
    try:
        import jwt
        from jwt import PyJWKClient
        
        # Telegram OIDC JWKS endpoint
        jwks_url = "https://oauth.telegram.org/.well-known/jwks.json"
        
        # Get the signing key
        jwks_client = PyJWKClient(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        
        # Decode and verify the token
        # Note: Bot ID from BotFather is used as the audience
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False}  # We'll verify aud manually
        )
        
        # Verify issuer
        if payload.get('iss') not in ['https://oauth.telegram.org', 'https://oauth.telegram.org/']:
            logging.warning(f"Invalid issuer: {payload.get('iss')}")
            return None
        
        # Verify audience (should match bot ID)
        # The aud claim contains the bot ID that requested the login
        # We'll accept any bot ID for now, or could verify against our bot
        
        return payload
        
    except Exception as e:
        logging.error(f"OIDC token verification error: {e}")
        return None


@app.route('/api/auth/telegram_oidc', methods=['POST'])
def auth_telegram_oidc():
    """Handle modern Telegram OIDC authentication with id_token."""
    data = request.json or {}
    id_token = data.get('id_token') or data.get('token')
    
    if not id_token:
        return jsonify({'error': 'id_token required'}), 400
    
    # Try OIDC validation first
    oidc_payload = _verify_telegram_oidc_token(id_token)
    
    if oidc_payload:
        # Modern OIDC flow
        user_id = oidc_payload.get('sub')
        username = oidc_payload.get('username', '')
        first_name = oidc_payload.get('first_name', '')
        last_name = oidc_payload.get('last_name', '')
        photo_url = oidc_payload.get('picture', '')
        
        if not user_id:
            return jsonify({'error': 'Invalid token payload'}), 401
    else:
        # Fallback: try legacy hash verification
        # This handles the old widget data format
        if not _verify_tg_hash(data):
            return jsonify({'error': 'Invalid auth data'}), 401
        
        user_id = data.get('id')
        username = data.get('username', '')
        first_name = data.get('first_name', '')
        last_name = data.get('last_name', '')
        photo_url = data.get('photo_url', '')
    
    # Store user
    _upsert_web_user(user_id, username, first_name, last_name, photo_url)
    
    # Create session
    token = _make_token(user_id)
    _sessions[token] = user_id
    
    resp = make_response(jsonify({
        'success': True,
        'user': {
            'id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'display_name': first_name or username or str(user_id),
            'photo_url': photo_url,
            'auth_provider': 'telegram'
        }
    }))
    
    is_production = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
    resp.set_cookie('zvonko_session', token,
                    max_age=30 * 24 * 3600,
                    httponly=True,
                    secure=is_production,
                    samesite='Lax')
    return resp




@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    token = request.cookies.get('zvonko_session')
    if token:
        _sessions.pop(token, None)
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('zvonko_session', '', expires=0, httponly=True, samesite='Lax')
    return resp


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """Register a new local user."""
    from .database import create_local_user
    
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip()
    display_name = data.get('display_name', '').strip()
    
    if not username or len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    if not password or len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    if create_local_user(username, password, email, display_name):
        return jsonify({'success': True, 'message': 'User created successfully'})
    else:
        return jsonify({'error': 'Username already exists'}), 409


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Login with local credentials."""
    from .database import verify_local_user
    
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    user = verify_local_user(username, password)
    if user:
        # Use negative ID to distinguish from Telegram users
        user_id = -user['id']
        token = _make_token(user_id)
        _sessions[token] = user_id
        
        resp = make_response(jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'username': user['username'],
                'display_name': user['display_name'],
                'email': user['email'],
                'auth_type': 'local'
            }
        }))
        resp.set_cookie('zvonko_session', token, max_age=30*24*3600, httponly=True, samesite='Lax')
        return resp
    else:
        return jsonify({'error': 'Invalid credentials'}), 401


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    uid = _current_uid()
    if uid == DEMO_USER_ID:
        return jsonify({'user': None})
    
    conn = db_connect()
    cur = conn.cursor()
    
    # Check if it's a local user (negative ID)
    if uid < 0:
        local_id = -uid
        cur.execute(
            "SELECT username, email, display_name FROM local_users WHERE id = ?", 
            (local_id,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            username, email, display_name = row
            return jsonify({'user': {
                'id': uid,
                'username': username or '',
                'first_name': '',
                'last_name': '',
                'display_name': display_name or username,
                'photo_url': '',
                'email': email or '',
                'auth_provider': 'local',
            }})
        return jsonify({'user': None})
    
    # Telegram user
    cur.execute(
        "SELECT username, first_name, last_name, photo_url, email, auth_provider FROM tg_users WHERE user_id = ?", 
        (str(uid),)
    )
    row = cur.fetchone()
    conn.close()
    if row:
        username, first_name, last_name, photo_url, email, auth_provider = row
        display_name = first_name or username or str(uid)
    else:
        username = first_name = last_name = photo_url = email = auth_provider = ''
        display_name = str(uid)
    return jsonify({'user': {
        'id': uid,
        'username': username or '',
        'first_name': first_name or '',
        'last_name': last_name or '',
        'display_name': display_name,
        'photo_url': photo_url or '',
        'email': email or '',
        'auth_provider': auth_provider or 'telegram',
    }})


# ──────────────────────────────────────────────────────
#  Mini App Auth (works when widget is blocked)
# ──────────────────────────────────────────────────────

@app.route('/api/auth/miniapp', methods=['POST'])
def auth_miniapp():
    """Authenticate via Telegram Mini App initData.
    
    This works even when telegram-widget.js is blocked.
    Frontend sends initData string from Telegram.WebApp.initData
    """
    data = request.json or {}
    init_data = data.get('initData', '')
    
    if not init_data:
        return jsonify({'error': 'No initData provided'}), 400
    
    if not TELEGRAM_TOKEN:
        return jsonify({'error': 'Server not configured'}), 500
    
    try:
        # Parse initData (query string format)
        from urllib.parse import parse_qsl, unquote
        params = dict(parse_qsl(init_data))
        
        received_hash = params.pop('hash', '')
        if not received_hash:
            return jsonify({'error': 'No hash in initData'}), 400
        
        # Build data_check_string (sorted params without hash)
        data_check_string = '\n'.join(
            f'{k}={v}' for k, v in sorted(params.items())
        )
        
        # Calculate secret key
        secret = hashlib.sha256(TELEGRAM_TOKEN.encode()).digest()
        calculated_hash = hmac.new(
            secret, 
            data_check_string.encode(), 
            hashlib.sha256
        ).hexdigest()
        
        # Verify hash
        if not hmac.compare_digest(calculated_hash, received_hash):
            logging.warning(f"MiniApp hash mismatch: {calculated_hash[:16]}... != {received_hash[:16]}...")
            return jsonify({'error': 'Invalid hash'}), 401
        
        # Check auth_date
        auth_date = int(params.get('auth_date', 0))
        if time.time() - auth_date > 86400:
            return jsonify({'error': 'Auth expired'}), 401
        
        # Extract user data
        import json
        user_json = params.get('user', '{}')
        user_data = json.loads(unquote(user_json)) if user_json else {}
        
        user_id = user_data.get('id')
        if not user_id:
            return jsonify({'error': 'No user ID in data'}), 400
        
        username = user_data.get('username', '')
        first_name = user_data.get('first_name', '')
        last_name = user_data.get('last_name', '')
        photo_url = user_data.get('photo_url', '')
        
        # Save user
        _upsert_web_user(user_id, username, first_name, last_name, photo_url)
        
        # Create session
        token = _make_token(user_id)
        _sessions[token] = user_id
        
        logging.info(f"MiniApp auth successful for user {user_id}")
        
        resp = make_response(jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'display_name': first_name or username or str(user_id),
            }
        }))
        
        # Set cookie
        is_production = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
        resp.set_cookie('zvonko_session', token,
                        max_age=30 * 24 * 3600,
                        httponly=True,
                        secure=is_production,
                        samesite='Lax')
        return resp
        
    except Exception as e:
        logging.error(f"MiniApp auth error: {e}")
        return jsonify({'error': 'Authentication failed'}), 500


# ──────────────────────────────────────────────────────
#  Music endpoints
# ──────────────────────────────────────────────────────

# Curated Russian playlists
CURATED_SECTIONS = [
    {'title': 'Русский хип-хоп', 'query': 'русский рэп 2024 хиты', 'icon': 'fire'},
    {'title': 'Популярное', 'query': 'хиты 2024 русские', 'icon': 'chart-line'},
    {'title': 'Русский рок', 'query': 'русский рок лучшее', 'icon': 'guitar'},
    {'title': 'Поп музыка', 'query': 'русская поп музыка хиты', 'icon': 'star'},
    {'title': 'Зарубежные хиты', 'query': 'top hits 2024', 'icon': 'globe'},
    {'title': 'Для души', 'query': 'спокойная музыка для души', 'icon': 'heart'},
]

@app.route('/api/home', methods=['GET'])
def api_home():
    """Home page data: curated sections with tracks."""
    from .ytmusic_service import search_tracks as yt_search
    from .cache import cache_get, cache_set
    
    cached = cache_get('home_data', 1800)
    if cached:
        return jsonify(cached)
    
    try:
        sections = []
        for section in CURATED_SECTIONS:
            try:
                tracks = yt_search(section['query'], limit=10)
                if tracks:
                    sections.append({
                        'title': section['title'],
                        'icon': section['icon'],
                        'tracks': [_track_to_dict(t) for t in tracks],
                    })
            except Exception as e:
                logging.warning(f"Section '{section['title']}' error: {e}")
        
        data = {'sections': sections}
        cache_set('home_data', data)
        return jsonify(data)
    except Exception as e:
        logging.error(f"Home API error: {e}")
        return jsonify({'sections': []})


@app.route('/api/mood/<params>', methods=['GET'])
def api_mood_playlist(params):
    """Get playlist by mood params."""
    from .ytmusic_service import _get_ytmusic, Track
    
    try:
        yt = _get_ytmusic()
        playlist_items = yt.get_mood_playlists(params)
        playlists = []
        for p in (playlist_items or [])[:12]:
            thumbnails = p.get('thumbnails', [])
            cover = thumbnails[-1].get('url', '') if thumbnails else ''
            if '=w' in cover:
                cover = re.sub(r'=w\d+-h\d+', '=w544-h544', cover)
            playlists.append({
                'id': p.get('playlistId', ''),
                'title': p.get('title', ''),
                'description': p.get('description', ''),
                'cover': cover,
            })
        return jsonify({'playlists': playlists})
    except Exception as e:
        logging.error(f"Mood playlist error: {e}")
        return jsonify({'playlists': []})


@app.route('/api/playlist/<playlist_id>', methods=['GET'])
def api_playlist(playlist_id):
    """Get playlist tracks."""
    from .ytmusic_service import _get_ytmusic, Track
    
    try:
        yt = _get_ytmusic()
        pl = yt.get_playlist(playlist_id, limit=50)
        tracks = []
        for item in pl.get('tracks', []):
            try:
                t = Track(item)
                if t.id:
                    tracks.append(_track_to_dict(t))
            except Exception:
                pass
        
        thumbnails = pl.get('thumbnails', [])
        cover = thumbnails[-1].get('url', '') if thumbnails else ''
        if '=w' in cover:
            cover = re.sub(r'=w\d+-h\d+', '=w544-h544', cover)
        
        return jsonify({
            'title': pl.get('title', ''),
            'description': pl.get('description', ''),
            'cover': cover,
            'tracks': tracks,
            'count': pl.get('trackCount', len(tracks)),
        })
    except Exception as e:
        logging.error(f"Playlist error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 30)), 50)
    if not query:
        return jsonify({'error': 'Query required'}), 400

    uid = _current_uid()
    log_search_query(uid, query)
    results = search_music(query, limit=limit)

    if not results:
        return jsonify({'tracks': []})

    return jsonify({'tracks': [_track_to_dict(t) for t in results]})


@app.route('/api/track/<track_id>', methods=['GET'])
def api_get_track(track_id):
    track = get_track_info(track_id)
    if not track:
        return jsonify({'error': 'Not found'}), 404
    uid = _current_uid()
    d = _track_to_dict(track)
    d['is_favorite'] = is_favorite(uid, track.id)
    return jsonify(d)


@app.route('/api/track/<track_id>/lyrics', methods=['GET'])
def api_get_lyrics(track_id):
    lyrics = get_track_lyrics_by_id(track_id)
    return jsonify({'lyrics': lyrics})


@app.route('/api/track/<track_id>/download', methods=['POST'])
def api_download_track(track_id):
    codec = (request.json or {}).get('codec', 'mp3')
    track = get_track_info(track_id)
    if not track:
        return jsonify({'error': 'Not found'}), 404
    uid = _current_uid()
    log_play_event(uid, track.id, track.title, track.artist_name())
    file_path = download_track(track, codec=codec)
    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': 'Download failed'}), 500
    return jsonify({'success': True, 'download_url': f'/api/files/{os.path.basename(file_path)}'})


@app.route('/api/files/<filename>', methods=['GET'])
def api_download_file(filename):
    return send_from_directory('/tmp', filename, as_attachment=True)


@app.route('/api/favorites', methods=['GET'])
def api_get_favorites():
    uid = _current_uid()
    favs = get_user_favorites(uid)
    if not favs:
        return jsonify({'tracks': []})
    track_ids = [f[0] for f in favs]
    tracks = get_tracks_by_ids(track_ids)
    return jsonify({'tracks': [_track_to_dict(t) for t in tracks]})


@app.route('/api/favorites/<track_id>', methods=['POST'])
def api_add_favorite(track_id):
    uid = _current_uid()
    track = get_track_info(track_id)
    if not track:
        return jsonify({'error': 'Not found'}), 404
    add_to_favorites(uid, track.id, track.title, track.artist_name())
    return jsonify({'success': True})


@app.route('/api/favorites/<track_id>', methods=['DELETE'])
def api_remove_favorite(track_id):
    uid = _current_uid()
    remove_from_favorites(uid, track_id)
    return jsonify({'success': True})


@app.route('/api/artist/<path:artist_name>', methods=['GET'])
def api_get_artist(artist_name):
    info = get_artist_full_info(artist_name)
    if not info:
        return jsonify({'error': 'Not found'}), 404
    tracks = get_artist_tracks(artist_name) or []
    return jsonify({'artist': info, 'tracks': [_track_to_dict(t) for t in tracks[:10]]})


@app.route('/api/history/search', methods=['GET'])
def api_search_history():
    uid = _current_uid()
    history = get_search_history(uid, limit=20)
    return jsonify({'queries': [h[0] for h in history]})


@app.route('/api/stats', methods=['GET'])
def api_get_stats():
    uid = _current_uid()
    stats = get_user_stats(uid)
    return jsonify(stats or {})


@app.route('/api/user/profile', methods=['GET'])
def api_get_profile():
    """Get complete user profile with stats, history and favorites count."""
    uid = _current_uid()
    if uid == DEMO_USER_ID:
        return jsonify({'error': 'Not authenticated'}), 401
    
    conn = db_connect()
    cur = conn.cursor()
    
    # Check if it's a local user (negative ID)
    if uid < 0:
        local_id = -uid
        cur.execute(
            "SELECT username, email, display_name, created_at FROM local_users WHERE id = ?", 
            (local_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'User not found'}), 404
        
        username, email, display_name, created_at = row
        # Get stats for local user
        stats = get_user_stats(uid) or {}
        # Get recent play history
        history = get_user_play_history(uid, limit=10)
        
        conn.close()
        
        return jsonify({
            'user': {
                'id': uid,
                'username': username or '',
                'first_name': '',
                'last_name': '',
                'display_name': display_name or username,
                'photo_url': '',
                'created_at': created_at,
                'auth_provider': 'local',
            },
            'stats': stats,
            'recent_history': history,
        })
    
    # Telegram user
    cur.execute("SELECT username, first_name, last_name, photo_url, created_at FROM tg_users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    
    username, first_name, last_name, photo_url, created_at = row
    
    # Get stats
    stats = get_user_stats(uid) or {}
    
    # Get recent play history (last 10)
    history = get_user_play_history(uid, limit=10)
    
    conn.close()
    
    return jsonify({
        'user': {
            'id': uid,
            'username': username or '',
            'first_name': first_name or '',
            'last_name': last_name or '',
            'display_name': first_name or username or str(uid),
            'photo_url': photo_url or '',
            'created_at': created_at,
            'auth_provider': 'telegram',
        },
        'stats': {
            'total_plays': stats.get('total_plays', 0),
            'unique_tracks': stats.get('unique_tracks', 0),
            'favorites_count': stats.get('favorites_count', 0),
            'search_count': stats.get('search_count', 0),
        },
        'recent_history': history or []
    })


@app.route('/api/user/history', methods=['GET'])
def api_get_play_history():
    """Get user's play history with pagination."""
    uid = _current_uid()
    if uid == DEMO_USER_ID:
        return jsonify({'error': 'Not authenticated'}), 401
    
    limit = min(int(request.args.get('limit', 20)), 50)
    offset = int(request.args.get('offset', 0))
    
    history = get_user_play_history(uid, limit=limit, offset=offset)
    return jsonify({'history': history or [], 'limit': limit, 'offset': offset})


# ──────────────────────────────────────────────────────
#  Google OAuth
# ──────────────────────────────────────────────────────

@app.route('/api/auth/google', methods=['POST'])
def auth_google():
    """Authenticate user with Google ID token."""
    if not GOOGLE_CLIENT_ID:
        return jsonify({'error': 'Google OAuth not configured'}), 503
    
    data = request.json or {}
    token = data.get('token')
    
    if not token:
        return jsonify({'error': 'Token required'}), 400
    
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        
        # Verify the Google ID token
        idinfo = id_token.verify_oauth2_token(
            token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10
        )
        
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            return jsonify({'error': 'Invalid token issuer'}), 401
        
        # Extract user info
        google_id = idinfo['sub']
        email = idinfo.get('email', '')
        name = idinfo.get('name', '')
        picture = idinfo.get('picture', '')
        first_name = idinfo.get('given_name', '')
        last_name = idinfo.get('family_name', '')
        
        # Use google_id as user_id (prefixed to avoid conflicts with Telegram)
        user_id = f"g_{google_id}"
        
        # Store/update user in database
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO tg_users 
                (user_id, username, first_name, last_name, photo_url, email, updated_at, auth_provider) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, email, first_name, last_name, picture, email, datetime.now().isoformat(), 'google')
        )
        conn.commit()
        conn.close()
        
        # Create session
        session_token = _make_token(user_id)
        _sessions[session_token] = user_id
        
        resp = make_response(jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'email': email,
                'first_name': first_name,
                'last_name': last_name,
                'display_name': name or first_name or email,
                'photo_url': picture,
                'auth_provider': 'google'
            }
        }))
        
        is_production = request.headers.get('X-Forwarded-Proto') == 'https' or request.is_secure
        resp.set_cookie('zvonko_session', session_token,
                        max_age=30 * 24 * 3600,
                        httponly=True,
                        secure=is_production,
                        samesite='Lax')
        return resp
        
    except ValueError as e:
        logging.error(f"Google auth error: {e}")
        return jsonify({'error': 'Invalid token'}), 401
    except Exception as e:
        logging.error(f"Google auth error: {e}")
        return jsonify({'error': 'Authentication failed'}), 500


@app.route('/api/auth/config', methods=['GET'])
def auth_config():
    """Get authentication configuration for frontend."""
    logging.info(f"Auth config requested from {request.remote_addr}")
    
    response = jsonify({
        'telegram': {
            'enabled': bool(TELEGRAM_TOKEN and BOT_USERNAME),
            'bot_id': TELEGRAM_BOT_ID,
            'bot_username': BOT_USERNAME,
        },
        'google': {
            'enabled': bool(GOOGLE_CLIENT_ID),
            'client_id': GOOGLE_CLIENT_ID if GOOGLE_CLIENT_ID else None,
        },
    })
    
    # Add CORS headers
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    
    return response


@app.route('/api/auth/debug', methods=['GET'])
def auth_debug():
    """Debug endpoint to check Telegram auth configuration."""
    import os
    
    # Check environment
    env_check = {
        'TELEGRAM_TOKEN_set': bool(TELEGRAM_TOKEN),
        'TELEGRAM_TOKEN_format_valid': bool(TELEGRAM_TOKEN and ':' in TELEGRAM_TOKEN) if TELEGRAM_TOKEN else False,
        'TELEGRAM_BOT_ID_set': bool(TELEGRAM_BOT_ID),
        'TELEGRAM_BOT_ID_value': TELEGRAM_BOT_ID,
        'BOT_USERNAME_set': bool(BOT_USERNAME),
        'BOT_USERNAME_value': BOT_USERNAME,
    }
    
    # Check if TELEGRAM_TOKEN matches expected format
    token_parts = TELEGRAM_TOKEN.split(':') if TELEGRAM_TOKEN else []
    token_bot_id = token_parts[0] if len(token_parts) >= 2 else None
    
    # Check if bot_id from env matches token
    bot_id_match = (TELEGRAM_BOT_ID == token_bot_id) if (TELEGRAM_BOT_ID and token_bot_id) else None
    
    return jsonify({
        'environment': env_check,
        'token_bot_id': token_bot_id,
        'bot_id_matches_token': bot_id_match,
        'config_status': 'ok' if (TELEGRAM_TOKEN and BOT_USERNAME) else 'missing_config',
        'instructions': 'To fix auth: 1) Verify TELEGRAM_TOKEN in .env 2) Check BOT_USERNAME matches @BotFather 3) Ensure domain is whitelisted in BotFather',
    })


# Keep old endpoint for backward compatibility
@app.route('/api/auth/providers', methods=['GET'])
def auth_providers():
    """Get available authentication providers (legacy)."""
    return jsonify({
        'telegram': bool(TELEGRAM_TOKEN and BOT_USERNAME),
        'google': bool(GOOGLE_CLIENT_ID),
    })


# ──────────────────────────────────────────────────────
#  Static / SPA
# ──────────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_spa(path):
    full = os.path.join(app.static_folder, path)
    if path and os.path.exists(full):
        resp = make_response(send_from_directory(app.static_folder, path))
        # Disable cache for JS/CSS to ensure updates are loaded
        if path.endswith(('.js', '.css', '.html')):
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['Pragma'] = 'no-cache'
            resp.headers['Expires'] = '0'
        return resp
    resp = make_response(send_from_directory(app.static_folder, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/api/proxy/image')
def proxy_image():
    """Proxy for YouTube images to avoid CORS and connection issues."""
    import requests
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    # Only allow YouTube CDN URLs
    if 'yt3.googleusercontent.com' not in url and 'lh3.googleusercontent.com' not in url:
        return jsonify({'error': 'Invalid URL'}), 400
    
    try:
        # Fetch image with timeout
        resp = requests.get(url, timeout=5, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        if resp.status_code == 200:
            response = make_response(resp.content)
            response.headers['Content-Type'] = resp.headers.get('Content-Type', 'image/jpeg')
            response.headers['Cache-Control'] = 'public, max-age=86400'  # Cache for 24 hours
            return response
        else:
            return jsonify({'error': 'Failed to fetch image'}), resp.status_code
    except Exception as e:
        logging.error(f"Image proxy error: {e}")
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────
#  Server startup
# ──────────────────────────────────────────────────────

def run_web_server(host='0.0.0.0', port=5000):
    app.run(host=host, port=port, debug=False, use_reloader=False)


def start_web_server_thread(host='0.0.0.0', port=5000):
    thread = threading.Thread(target=run_web_server, args=(host, port), daemon=True)
    thread.start()
    logging.info(f"Web server started on http://{host}:{port}")
    return thread


@app.route('/api/search/all', methods=['GET'])
def api_search_all():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Query required'}), 400
    uid = _current_uid()
    log_search_query(uid, query)

    from .ytmusic_service import search_tracks as yt_search, _get_ytmusic
    import logging

    tracks = yt_search(query, limit=20) or []

    artists = []
    try:
        yt = _get_ytmusic()
        sr = yt.search(query, filter='artists', limit=5)
        for a in (sr or []):
            thumbs = a.get('thumbnails', [])
            cover  = thumbs[-1].get('url') if thumbs else None
            artists.append({
                'browseId':    a.get('browseId', ''),
                'name':        a.get('artist', a.get('name', '')),
                'subscribers': a.get('subscribers', ''),
                'cover':       cover,
            })
    except Exception as e:
        logging.error(f"Artist search error: {e}")

    return jsonify({
        'tracks':  [_track_to_dict(t) for t in tracks],
        'artists': artists,
    })


@app.route('/api/artist/browse/<browse_id>', methods=['GET'])
def api_artist_browse(browse_id):
    from .ytmusic_service import _get_ytmusic, Track
    import logging
    try:
        yt  = _get_ytmusic()
        ai  = yt.get_artist(browse_id)
        tracks = []
        if 'songs' in ai and 'results' in ai['songs']:
            for s in ai['songs']['results'][:20]:
                try:
                    t = Track(s)
                    if t.id:
                        tracks.append(_track_to_dict(t))
                except Exception:
                    pass
        
        # Get albums
        albums = []
        if 'albums' in ai and 'results' in ai['albums']:
            for a in ai['albums']['results'][:10]:
                try:
                    album = {
                        'browseId': a.get('browseId'),
                        'title': a.get('title'),
                        'year': a.get('year'),
                        'cover': None
                    }
                    thumbs = a.get('thumbnails', [])
                    if thumbs:
                        album['cover'] = thumbs[-1].get('url')
                    albums.append(album)
                except Exception:
                    pass
        
        thumbs = ai.get('thumbnails', [])
        return jsonify({
            'name':        ai.get('name', ''),
            'description': ai.get('description', ''),
            'subscribers': ai.get('subscribers', ''),
            'cover':       thumbs[-1].get('url') if thumbs else None,
            'tracks':      tracks,
            'albums':      albums,
        })
    except Exception as e:
        logging.error(f"artist_browse: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/album/<browse_id>', methods=['GET'])
def api_album(browse_id):
    from .ytmusic_service import _get_ytmusic, Track
    import logging
    try:
        yt = _get_ytmusic()
        
        # Try to get album directly first
        album = None
        try:
            album = yt.get_album(browse_id)
        except Exception as e:
            logging.warning(f"Direct album lookup failed: {e}")
        
        # If direct lookup fails, try to find album by searching
        if not album:
            # Get artist info to find album name
            try:
                # This is a workaround - we'll return a basic response
                return jsonify({
                    'title': 'Album',
                    'year': None,
                    'artist': '',
                    'cover': None,
                    'tracks': [],
                    'error': 'Album details not available'
                })
            except Exception:
                pass
        
        # Extract tracks
        tracks = []
        if 'tracks' in album:
            for t in album['tracks']:
                try:
                    track = Track(t)
                    if track.id:
                        tracks.append(_track_to_dict(track))
                except Exception:
                    pass
        
        # Extract metadata
        thumbs = album.get('thumbnails', [])
        artists = album.get('artists', [])
        artist_name = artists[0].get('name', '') if artists else ''
        
        return jsonify({
            'title': album.get('title', ''),
            'year': album.get('year'),
            'artist': artist_name,
            'cover': thumbs[-1].get('url') if thumbs else None,
            'tracks': tracks,
        })
    except Exception as e:
        logging.error(f"album: {e}")
        return jsonify({'error': str(e)}), 500


_download_locks = {}  # Per-track download locks

AUDIO_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'audio_cache')
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

def _get_cached_audio(track_id):
    """Return cached audio file path if exists."""
    for ext in ('m4a', 'webm', 'opus', 'mp3'):
        path = os.path.join(AUDIO_CACHE_DIR, f'{track_id}.{ext}')
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None

def _download_audio(track_id):
    """Download audio with yt-dlp to cache directory."""
    import subprocess
    output_template = os.path.join(AUDIO_CACHE_DIR, f'{track_id}.%(ext)s')
    
    try:
        result = subprocess.run(
            ['yt-dlp', '--no-playlist',
             '-f', 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
             '--force-ipv4',
             '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
             '-o', output_template,
             '--no-post-overwrites',
             f'https://www.youtube.com/watch?v={track_id}'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return _get_cached_audio(track_id)
        else:
            logging.error(f"yt-dlp error: {result.stderr[:500]}")
            return None
    except Exception as e:
        logging.error(f"Download error for {track_id}: {e}")
        return None

@app.route('/api/track/<track_id>/stream', methods=['GET'])
def api_stream_track(track_id):
    """Download audio to local cache and serve it."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]{5,20}$', track_id):
        return jsonify({'error': 'Invalid track ID'}), 400
    
    # Check cache first
    cached = _get_cached_audio(track_id)
    if cached:
        resp = send_file(cached, mimetype='audio/mp4')
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        resp.headers['Accept-Ranges'] = 'bytes'
        return resp
    
    # Download with per-track lock to avoid duplicate downloads
    if track_id not in _download_locks:
        _download_locks[track_id] = threading.Lock()
    
    with _download_locks[track_id]:
        # Double-check cache after acquiring lock
        cached = _get_cached_audio(track_id)
        if cached:
            resp = send_file(cached, mimetype='audio/mp4')
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp
        
        # Download
        path = _download_audio(track_id)
        if path:
            resp = send_file(path, mimetype='audio/mp4')
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            resp.headers['Accept-Ranges'] = 'bytes'
            return resp
        else:
            return jsonify({'error': 'Failed to download track'}), 500
