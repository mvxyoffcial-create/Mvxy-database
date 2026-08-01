import os
import requests
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from flask import Flask, request, jsonify, render_template, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps, lru_cache
from base64 import b64decode
from flask_cors import CORS
from flask_compress import Compress
from datetime import date, datetime
import re
from threading import Lock

# --- Fast JSON Fallback Engine ---
try:
    import orjson
    def json_dumps(obj):
        return orjson.dumps(obj).decode('utf-8')
    def json_loads(s):
        return orjson.loads(s)
except ImportError:
    try:
        import ujson as json_lib
        json_dumps = json_lib.dumps
        json_loads = json_lib.loads
    except ImportError:
        import json as json_lib
        json_dumps = json_lib.dumps
        json_loads = json_lib.loads

app = Flask(__name__)

# Enable CORS for all routes
CORS(app)

# Enable Gzip compression for all responses
Compress(app)

# Configure Flask for better performance
app.config['JSON_SORT_KEYS'] = False
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year cache for static files

# --- Configuration ---
DATABASE_URL = os.environ.get('DATABASE_URL') or "postgresql://<user>:<password>@<host>:<port>/<dbname>"
TMDB_API_KEY = "52f6a75a38a397d940959b336801e1c3"
ADMIN_USERNAME = "venura"
ADMIN_PASSWORD_HASH = generate_password_hash("venura")

# --- Persistent HTTP Session for API Requests ---
http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

# --- Pre-compiled Regex Patterns ---
YOUTUBE_PATTERNS = [
    re.compile(r'(?:youtube\.com\/watch\?v=)([\w-]{11})'),
    re.compile(r'(?:youtu\.be\/)([\w-]{11})'),
    re.compile(r'(?:youtube\.com\/embed\/)([\w-]{11})'),
    re.compile(r'(?:youtube\.com\/v\/)([\w-]{11})')
]

# Database Connection Pool
connection_pool = None
pool_lock = Lock()

def init_connection_pool():
    global connection_pool
    if connection_pool is None:
        with pool_lock:
            if connection_pool is None:
                try:
                    connection_pool = psycopg2.pool.ThreadedConnectionPool(
                        minconn=2,
                        maxconn=20,
                        dsn=DATABASE_URL,
                        sslmode='require'
                    )
                    print("Connection pool created successfully")
                except Exception as e:
                    print(f"Error creating connection pool: {e}")
                    connection_pool = None

# Initialize pool on startup
init_connection_pool()

# --- Optimized Database Connection ---
def get_db():
    if 'db' not in g:
        try:
            if connection_pool:
                g.db = connection_pool.getconn()
            else:
                g.db = psycopg2.connect(DATABASE_URL, sslmode='require')
        except psycopg2.Error as e:
            return None, str(e)
    return g.db, None

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        if connection_pool:
            connection_pool.putconn(db)
        else:
            db.close()

# --- Basic Authentication ---
def check_auth(username, password):
    return username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password)

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({'message': 'Authorization Required'}), 401, {'WWW-Authenticate': 'Basic realm="Login Required"'}
        try:
            auth_type, credentials = auth_header.split()
            if auth_type.lower() == 'basic':
                decoded_credentials = b64decode(credentials).decode('utf-8')
                username, password = decoded_credentials.split(':', 1)
                if check_auth(username, password):
                    return f(*args, **kwargs)
        except Exception:
            pass
        return jsonify({'message': 'Authorization Failed'}), 401, {'WWW-Authenticate': 'Basic realm="Login Required"'}
    return decorated

# --- Cached TMDB API Helper with Connection Pooling ---
@lru_cache(maxsize=256)
def fetch_tmdb_data(tmdb_id, media_type):
    url = ""
    if media_type == 'movie':
        url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits"
    elif media_type == 'tv':
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits"
    
    if not url:
        return None

    try:
        response = http_session.get(url, timeout=4)
        if response.status_code == 200:
            data = response.json()
            cast = [
                {
                    "name": member.get("name"),
                    "character": member.get("character"),
                    "image": f"https://image.tmdb.org/t/p/original{member.get('profile_path')}" if member.get('profile_path') else None
                }
                for member in data.get('credits', {}).get('cast', [])[:10]
            ]
            
            video_links = {
                'video_720p': "",
                'video_1080p': "",
                'video_2160p': ""
            }

            return {
                'title': data.get('title') if media_type == 'movie' else data.get('name'),
                'description': data.get('overview'),
                'thumbnail': f"https://image.tmdb.org/t/p/original{data.get('poster_path')}" if data.get('poster_path') else None,
                'backdrop': f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}" if data.get('backdrop_path') else None,
                'release_date': data.get('release_date') if media_type == 'movie' else data.get('first_air_date'),
                'language': data.get('original_language'),
                'rating': data.get('vote_average'),
                'cast_members': cast,
                'total_seasons': data.get('number_of_seasons') if media_type == 'tv' else None,
                'genres': [g['name'] for g in data.get('genres', [])],
                'video_links': video_links,
                'file_type': 'webrip',
                'source_type': 'original',
                'youtube_trailer': '',
                'subtitles': {
                    'english': [],
                    'sinhala': []
                }
            }
        return None
    except requests.RequestException:
        return None

@lru_cache(maxsize=10)
def fetch_genres(media_type):
    url = f"https://api.themoviedb.org/3/genre/{media_type}/list?api_key={TMDB_API_KEY}"
    try:
        response = http_session.get(url, timeout=4)
        if response.status_code == 200:
            return tuple((g['name'] for g in response.json().get('genres', [])))
        return tuple()
    except requests.RequestException:
        return tuple()

# --- Optimized Helper Functions ---
def safe_json_loads(data, default=None):
    if data is None:
        return default
    if isinstance(data, (dict, list)):
        return data
    try:
        return json_loads(data) if data else default
    except Exception:
        return default

def clean_value(value):
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value

def format_date_for_input(date_value):
    if not date_value:
        return None
    if isinstance(date_value, str):
        return date_value[:10] if len(date_value) >= 10 else date_value
    elif isinstance(date_value, (date, datetime)):
        return date_value.strftime('%Y-%m-%d')
    return None

def extract_youtube_id(url):
    if not url:
        return None
    for pattern in YOUTUBE_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    if len(url) == 11 and all(c.isalnum() or c in ('-', '_') for c in url):
        return url
    return None

def parse_subtitle_input(subtitle_data):
    if not subtitle_data:
        return []
    if isinstance(subtitle_data, str):
        if subtitle_data.startswith('[') and subtitle_data.endswith(']'):
            return safe_json_loads(subtitle_data, [])
        return [url.strip() for url in subtitle_data.split(',') if url.strip()]
    elif isinstance(subtitle_data, list):
        return subtitle_data
    return []

def prepare_media_data(data):
    genres = data.get('genres', [])
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(',')] if genres else []
    elif genres is None:
        genres = []
    
    source_type = data.get('source_type', 'original')
    valid_source_types = {'original', 'camcopy', 'bluray', 'webrip', 'web-dl', 'hdtv', 'dvdrip', 'brrip'}
    if source_type not in valid_source_types:
        source_type = 'original'
    
    youtube_trailer = clean_value(data.get('youtube_trailer'))
    if youtube_trailer:
        youtube_id = extract_youtube_id(youtube_trailer)
        if youtube_id:
            youtube_trailer = f"https://www.youtube.com/embed/{youtube_id}"
    
    def process_screenshots(key):
        screenshots_input = data.get(key, '')
        if isinstance(screenshots_input, str):
            if screenshots_input.startswith('[') and screenshots_input.endswith(']'):
                return safe_json_loads(screenshots_input, [])
            return [url.strip() for url in screenshots_input.split(',') if url.strip()]
        elif isinstance(screenshots_input, list):
            return screenshots_input
        return []
    
    screenshots_720p = process_screenshots('screenshots_720p')
    screenshots_1080p = process_screenshots('screenshots_1080p')
    screenshots_2160p = process_screenshots('screenshots_2160p')
    screenshots_trailer = process_screenshots('screenshots_trailer')
    
    video_links = {}
    if data.get('video_links'):
        video_links = safe_json_loads(data.get('video_links'), {})
    else:
        video_720p = clean_value(data.get('video_720p')) or clean_value(data.get('tv_video_720p'))
        video_1080p = clean_value(data.get('video_1080p')) or clean_value(data.get('tv_video_1080p'))
        video_2160p = clean_value(data.get('video_2160p')) or clean_value(data.get('tv_video_2160p'))
        if video_720p: video_links['video_720p'] = video_720p
        if video_1080p: video_links['video_1080p'] = video_1080p
        if video_2160p: video_links['video_2160p'] = video_2160p
    
    download_links = {}
    if data.get('download_links'):
        download_links = safe_json_loads(data.get('download_links'), {})
    else:
        download_720p = clean_value(data.get('download_720p'))
        download_1080p = clean_value(data.get('download_1080p'))
        download_2160p = clean_value(data.get('download_2160p'))
        file_type = data.get('file_type', 'webrip')
        if download_720p: download_links['download_720p'] = {'url': download_720p, 'file_type': file_type}
        if download_1080p: download_links['download_1080p'] = {'url': download_1080p, 'file_type': file_type}
        if download_2160p: download_links['download_2160p'] = {'url': download_2160p, 'file_type': file_type}
    
    telegram_links = {}
    if data.get('telegram_links'):
        telegram_links = safe_json_loads(data.get('telegram_links'), {})
    else:
        telegram_720p = clean_value(data.get('telegram_720p'))
        telegram_1080p = clean_value(data.get('telegram_1080p'))
        telegram_2160p = clean_value(data.get('telegram_2160p'))
        if telegram_720p: telegram_links['telegram_720p'] = telegram_720p
        if telegram_1080p: telegram_links['telegram_1080p'] = telegram_1080p
        if telegram_2160p: telegram_links['telegram_2160p'] = telegram_2160p
    
    torrent_links = {}
    if data.get('torrent_links'):
        torrent_links = safe_json_loads(data.get('torrent_links'), {})
    else:
        torrent_720p = clean_value(data.get('torrent_720p'))
        torrent_1080p = clean_value(data.get('torrent_1080p'))
        torrent_2160p = clean_value(data.get('torrent_2160p'))
        if torrent_720p: torrent_links['torrent_720p'] = torrent_720p
        if torrent_1080p: torrent_links['torrent_1080p'] = torrent_1080p
        if torrent_2160p: torrent_links['torrent_2160p'] = torrent_2160p
    
    subtitles = {'english': [], 'sinhala': []}
    if data.get('subtitles'):
        subtitles_data = safe_json_loads(data.get('subtitles'), {})
        if isinstance(subtitles_data, dict):
            subtitles['english'] = parse_subtitle_input(subtitles_data.get('english', []))
            subtitles['sinhala'] = parse_subtitle_input(subtitles_data.get('sinhala', []))
    else:
        subtitles['english'] = parse_subtitle_input(data.get('english_subtitles', ''))
        subtitles['sinhala'] = parse_subtitle_input(data.get('sinhala_subtitles', ''))
    
    rating = data.get('rating')
    try:
        rating = float(rating) if rating not in (None, '') else None
    except (ValueError, TypeError):
        rating = None

    total_seasons = data.get('total_seasons')
    try:
        total_seasons = int(total_seasons) if total_seasons not in (None, '') else None
    except (ValueError, TypeError):
        total_seasons = None

    file_type = data.get('file_type', 'webrip')
    status = clean_value(data.get('status'))
    seasons_data = safe_json_loads(data.get('seasons'), {})
    
    if seasons_data and isinstance(seasons_data, dict):
        for season_info in seasons_data.values():
            if 'episodes' in season_info and isinstance(season_info['episodes'], list):
                for episode in season_info['episodes']:
                    if 'subtitles' not in episode or not isinstance(episode['subtitles'], dict):
                        episode['subtitles'] = {'english': [], 'sinhala': []}
                    else:
                        episode['subtitles'].setdefault('english', [])
                        episode['subtitles'].setdefault('sinhala', [])

    return {
        'type': data.get('type'),
        'title': clean_value(data.get('title', '')),
        'description': clean_value(data.get('description')),
        'thumbnail': clean_value(data.get('thumbnail')),
        'backdrop': clean_value(data.get('backdrop')),
        'release_date': clean_value(data.get('release_date')),
        'language': clean_value(data.get('language')),
        'rating': rating,
        'status': status,
        'cast_members': safe_json_loads(data.get('cast_members'), []),
        'video_links': video_links,
        'download_links': download_links,
        'telegram_links': telegram_links,
        'torrent_links': torrent_links,
        'total_seasons': total_seasons,
        'seasons': seasons_data,
        'genres': genres,
        'file_type': file_type,
        'source_type': source_type,
        'youtube_trailer': youtube_trailer,
        'screenshots_720p': screenshots_720p,
        'screenshots_1080p': screenshots_1080p,
        'screenshots_2160p': screenshots_2160p,
        'screenshots_trailer': screenshots_trailer,
        'subtitles': subtitles
    }

# Optimized function to parse media row
def parse_media_row(row):
    media_dict = dict(row)
    media_dict['release_date'] = format_date_for_input(media_dict.get('release_date'))
    media_dict['cast_members'] = safe_json_loads(media_dict.get('cast_members'), [])
    media_dict['video_links'] = safe_json_loads(media_dict.get('video_links'), {})
    media_dict['download_links'] = safe_json_loads(media_dict.get('download_links'), {})
    media_dict['telegram_links'] = safe_json_loads(media_dict.get('telegram_links'), {})
    media_dict['torrent_links'] = safe_json_loads(media_dict.get('torrent_links'), {})
    media_dict['seasons'] = safe_json_loads(media_dict.get('seasons'))
    media_dict['genres'] = safe_json_loads(media_dict.get('genres'), [])
    media_dict['screenshots_720p'] = safe_json_loads(media_dict.get('screenshots_720p'), [])
    media_dict['screenshots_1080p'] = safe_json_loads(media_dict.get('screenshots_1080p'), [])
    media_dict['screenshots_2160p'] = safe_json_loads(media_dict.get('screenshots_2160p'), [])
    media_dict['screenshots_trailer'] = safe_json_loads(media_dict.get('screenshots_trailer'), [])
    media_dict['subtitles'] = safe_json_loads(media_dict.get('subtitles'), {'english': [], 'sinhala': []})
    
    if media_dict['seasons'] and isinstance(media_dict['seasons'], dict):
        for season_info in media_dict['seasons'].values():
            if 'episodes' in season_info and isinstance(season_info['episodes'], list):
                for episode in season_info['episodes']:
                    if 'subtitles' not in episode:
                        episode['subtitles'] = {'english': [], 'sinhala': []}
    return media_dict

# --- Main Public Routes ---
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/docs")
def api_docs():
    return render_template("api_docs.html")

# --- Admin Panel Routes ---
@app.route("/admin")
@requires_auth
def admin_dashboard():
    return render_template("admin_dashboard.html")

@app.route("/admin/add_movie")
@requires_auth
def add_movie_page():
    return render_template("add_movie.html")

@app.route("/admin/add_tv")
@requires_auth
def add_tv_page():
    return render_template("add_tv.html")

@app.route("/admin/search_and_edit")
@requires_auth
def search_and_edit_page():
    return render_template("search_and_edit.html")

@app.route("/admin/edit")
@requires_auth
def edit_media_page():
    return render_template("edit_media.html")

@app.route("/admin/add_episode")
@requires_auth
def add_episode_page():
    media_id = request.args.get('media_id')
    if not media_id:
        return "Media ID required", 400
    
    conn, error = get_db()
    if error:
        return f"Database error: {error}", 500
    
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT id, title FROM media WHERE id = %s AND type = 'tv';", (media_id,))
    media = cur.fetchone()
    
    if not media:
        return "TV series not found", 404
    
    return render_template("add_episode.html", media=dict(media))

# --- Optimized Public API Endpoints ---
@app.route("/api/media", methods=["GET"])
def get_all_media():
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM media ORDER BY id DESC;")
        media = cur.fetchall()
        
        media_list = [parse_media_row(row) for row in media]
        
        response = jsonify(media_list)
        response.headers['Cache-Control'] = 'public, max-age=60'
        return response
    except psycopg2.Error as e:
        return jsonify({"message": "Database error", "error": str(e)}), 500

@app.route("/api/media/<int:media_id>", methods=["GET"])
def get_single_media(media_id):
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM media WHERE id = %s;", (media_id,))
        media = cur.fetchone()
        
        if media:
            response = jsonify(parse_media_row(media))
            response.headers['Cache-Control'] = 'public, max-age=300'
            return response
        
        return jsonify({"message": "Media not found"}), 404
    except psycopg2.Error as e:
        return jsonify({"message": "Database error", "error": str(e)}), 500

@app.route("/api/genres", methods=["GET"])
def get_all_genres():
    try:
        movie_genres = fetch_genres('movie')
        tv_genres = fetch_genres('tv')
        all_genres = set(movie_genres).union(tv_genres)
        
        response = jsonify(sorted(list(all_genres)))
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response
    except Exception as e:
        return jsonify({"message": "Error fetching genres", "error": str(e)}), 500

# --- Admin API Endpoints ---
@app.route("/api/admin/tmdb_fetch", methods=["POST"])
@requires_auth
def tmdb_fetch_api():
    data = request.json
    tmdb_id = data.get("tmdb_id")
    media_type = data.get("media_type")
    
    if not tmdb_id or not media_type:
        return jsonify({"message": "TMDB ID and media type are required"}), 400
    
    tmdb_data = fetch_tmdb_data(tmdb_id, media_type)
    if tmdb_data:
        return jsonify(tmdb_data), 200
    
    return jsonify({"message": "Failed to fetch data from TMDB"}), 404

@app.route("/api/admin/media", methods=["POST"])
@requires_auth
def add_media():
    data = request.json
    if not data or not data.get('title'):
        return jsonify({"message": "Title is required"}), 400
    
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        media_data = prepare_media_data(data)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO media (
                type, title, description, thumbnail, backdrop, release_date, language, rating, status,
                cast_members, video_links, download_links, telegram_links, torrent_links,
                total_seasons, seasons, genres, file_type, source_type, youtube_trailer,
                screenshots_720p, screenshots_1080p, screenshots_2160p, screenshots_trailer,
                subtitles
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            media_data['type'], 
            media_data['title'], 
            media_data['description'], 
            media_data['thumbnail'],
            media_data['backdrop'],
            media_data['release_date'], 
            media_data['language'], 
            media_data['rating'],
            media_data['status'],
            json_dumps(media_data['cast_members']), 
            json_dumps(media_data['video_links']), 
            json_dumps(media_data['download_links']),
            json_dumps(media_data['telegram_links']),
            json_dumps(media_data['torrent_links']),
            media_data['total_seasons'], 
            json_dumps(media_data['seasons']), 
            json_dumps(media_data['genres']),
            media_data['file_type'],
            media_data['source_type'],
            media_data['youtube_trailer'],
            json_dumps(media_data['screenshots_720p']),
            json_dumps(media_data['screenshots_1080p']),
            json_dumps(media_data['screenshots_2160p']),
            json_dumps(media_data['screenshots_trailer']),
            json_dumps(media_data['subtitles'])
        ))
        
        media_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({"message": "Media added successfully", "id": media_id}), 201
        
    except (psycopg2.DatabaseError, ValueError) as e:
        conn.rollback()
        return jsonify({"message": "Error adding media", "error": str(e)}), 400

@app.route("/api/admin/media/<int:media_id>", methods=["PUT"])
@requires_auth
def update_media(media_id):
    data = request.json
    if not data or not data.get('title'):
        return jsonify({"message": "Title is required"}), 400
    
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        media_data = prepare_media_data(data)
        cur = conn.cursor()
        cur.execute("""
            UPDATE media SET
                type = %s, title = %s, description = %s, thumbnail = %s, backdrop = %s, release_date = %s,
                language = %s, rating = %s, status = %s, cast_members = %s, video_links = %s, 
                download_links = %s, telegram_links = %s, torrent_links = %s, total_seasons = %s, seasons = %s, 
                genres = %s, file_type = %s, source_type = %s, youtube_trailer = %s,
                screenshots_720p = %s, screenshots_1080p = %s, screenshots_2160p = %s, screenshots_trailer = %s,
                subtitles = %s
            WHERE id = %s;
        """, (
            media_data['type'], 
            media_data['title'], 
            media_data['description'], 
            media_data['thumbnail'],
            media_data['backdrop'],
            media_data['release_date'], 
            media_data['language'], 
            media_data['rating'],
            media_data['status'],
            json_dumps(media_data['cast_members']), 
            json_dumps(media_data['video_links']), 
            json_dumps(media_data['download_links']),
            json_dumps(media_data['telegram_links']),
            json_dumps(media_data['torrent_links']),
            media_data['total_seasons'], 
            json_dumps(media_data['seasons']), 
            json_dumps(media_data['genres']),
            media_data['file_type'],
            media_data['source_type'],
            media_data['youtube_trailer'],
            json_dumps(media_data['screenshots_720p']),
            json_dumps(media_data['screenshots_1080p']),
            json_dumps(media_data['screenshots_2160p']),
            json_dumps(media_data['screenshots_trailer']),
            json_dumps(media_data['subtitles']),
            media_id
        ))
        
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"message": "Media not found"}), 404
        
        return jsonify({"message": "Media updated successfully"}), 200
        
    except (psycopg2.DatabaseError, ValueError) as e:
        conn.rollback()
        return jsonify({"message": "Error updating media", "error": str(e)}), 400

@app.route("/api/admin/media/<int:media_id>/episode", methods=["POST"])
@requires_auth
def add_episode(media_id):
    data = request.json
    if not data:
        return jsonify({"message": "Episode data is required"}), 400
    
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT seasons, file_type FROM media WHERE id = %s AND type = 'tv';", (media_id,))
        media = cur.fetchone()
        
        if not media:
            return jsonify({"message": "TV series not found"}), 404
        
        current_seasons = safe_json_loads(media['seasons'], {})
        
        episode_subtitles = {
            'english': parse_subtitle_input(data.get('english_subtitles', '')),
            'sinhala': parse_subtitle_input(data.get('sinhala_subtitles', ''))
        }
        
        season_number = data.get('season_number')
        video_links = data.get('video_links', {})
        download_links = data.get('download_links', {})
        telegram_links = data.get('telegram_links', {})
        torrent_links = data.get('torrent_links', {})

        episode_data = {
            'episode_number': data.get('episode_number'),
            'episode_name': data.get('episode_name'),
            'video_720p': video_links.get('video_720p'),
            'video_1080p': video_links.get('video_1080p'),
            'video_2160p': video_links.get('video_2160p'),
            'download_720p': download_links.get('download_720p'),
            'download_1080p': download_links.get('download_1080p'),
            'download_2160p': download_links.get('download_2160p'),
            'telegram_720p': telegram_links.get('telegram_720p'),
            'telegram_1080p': telegram_links.get('telegram_1080p'),
            'telegram_2160p': telegram_links.get('telegram_2160p'),
            'torrent_720p': torrent_links.get('torrent_720p'),
            'torrent_1080p': torrent_links.get('torrent_1080p'),
            'torrent_2160p': torrent_links.get('torrent_2160p'),
            'subtitles': episode_subtitles
        }
        
        season_key = f'season_{season_number}'
        if season_key not in current_seasons:
            current_seasons[season_key] = {
                'season_number': season_number,
                'total_episodes': 0,
                'episodes': []
            }
        
        current_seasons[season_key]['episodes'].append(episode_data)
        current_seasons[season_key]['total_episodes'] = len(current_seasons[season_key]['episodes'])
        
        cur.execute("""
            UPDATE media SET seasons = %s 
            WHERE id = %s;
        """, (json_dumps(current_seasons), media_id))
        
        conn.commit()
        return jsonify({"message": "Episode added successfully"}), 200
        
    except (psycopg2.DatabaseError, ValueError) as e:
        conn.rollback()
        return jsonify({"message": "Error adding episode", "error": str(e)}), 400

@app.route("/api/admin/media/<int:media_id>", methods=["DELETE"])
@requires_auth
def delete_media(media_id):
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM media WHERE id = %s;", (media_id,))
        conn.commit()
        
        if cur.rowcount == 0:
            return jsonify({"message": "Media not found"}), 404
        
        return jsonify({"message": "Media deleted successfully"}), 200
        
    except psycopg2.DatabaseError as e:
        conn.rollback()
        return jsonify({"message": "Error deleting media", "error": str(e)}), 400

# --- Subtitle Management Endpoints ---
@app.route("/api/admin/media/<int:media_id>/subtitles", methods=["PUT"])
@requires_auth
def update_media_subtitles(media_id):
    data = request.json
    if not data:
        return jsonify({"message": "Subtitle data is required"}), 400
    
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT type, subtitles, seasons FROM media WHERE id = %s;", (media_id,))
        media = cur.fetchone()
        
        if not media:
            return jsonify({"message": "Media not found"}), 404
        
        media_type = media['type']
        current_seasons = safe_json_loads(media['seasons'], {})
        
        if media_type == 'movie':
            new_subtitles = {
                'english': parse_subtitle_input(data.get('english', [])),
                'sinhala': parse_subtitle_input(data.get('sinhala', []))
            }
            cur.execute("""
                UPDATE media SET subtitles = %s 
                WHERE id = %s;
            """, (json_dumps(new_subtitles), media_id))
            
        elif media_type == 'tv':
            season_number = data.get('season_number')
            episode_number = data.get('episode_number')
            
            if season_number is not None and episode_number is not None:
                season_key = f'season_{season_number}'
                if season_key in current_seasons and 'episodes' in current_seasons[season_key]:
                    episodes = current_seasons[season_key]['episodes']
                    for episode in episodes:
                        if episode.get('episode_number') == episode_number:
                            episode['subtitles'] = {
                                'english': parse_subtitle_input(data.get('english', [])),
                                'sinhala': parse_subtitle_input(data.get('sinhala', []))
                            }
                            break
                
                cur.execute("""
                    UPDATE media SET seasons = %s 
                    WHERE id = %s;
                """, (json_dumps(current_seasons), media_id))
            else:
                new_subtitles = {
                    'english': parse_subtitle_input(data.get('english', [])),
                    'sinhala': parse_subtitle_input(data.get('sinhala', []))
                }
                cur.execute("""
                    UPDATE media SET subtitles = %s 
                    WHERE id = %s;
                """, (json_dumps(new_subtitles), media_id))
        
        conn.commit()
        return jsonify({"message": "Subtitles updated successfully"}), 200
        
    except (psycopg2.DatabaseError, ValueError) as e:
        conn.rollback()
        return jsonify({"message": "Error updating subtitles", "error": str(e)}), 400

@app.route("/api/admin/media/<int:media_id>/episode/<int:episode_number>/subtitles", methods=["PUT"])
@requires_auth
def update_episode_subtitles(media_id, episode_number):
    data = request.json
    season_number = request.args.get('season_number', type=int)
    
    if not data or season_number is None:
        return jsonify({"message": "Season number and subtitle data are required"}), 400
    
    conn, error = get_db()
    if error:
        return jsonify({"message": "Database connection error", "error": error}), 500
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT seasons FROM media WHERE id = %s AND type = 'tv';", (media_id,))
        media = cur.fetchone()
        
        if not media:
            return jsonify({"message": "TV series not found"}), 404
        
        current_seasons = safe_json_loads(media['seasons'], {})
        season_key = f'season_{season_number}'
        
        if season_key not in current_seasons or 'episodes' not in current_seasons[season_key]:
            return jsonify({"message": "Season or episode not found"}), 404
        
        episodes = current_seasons[season_key]['episodes']
        episode_found = False
        
        for episode in episodes:
            if episode.get('episode_number') == episode_number:
                episode['subtitles'] = {
                    'english': parse_subtitle_input(data.get('english', [])),
                    'sinhala': parse_subtitle_input(data.get('sinhala', []))
                }
                episode_found = True
                break
        
        if not episode_found:
            return jsonify({"message": "Episode not found"}), 404
        
        cur.execute("""
            UPDATE media SET seasons = %s 
            WHERE id = %s;
        """, (json_dumps(current_seasons), media_id))
        
        conn.commit()
        return jsonify({"message": "Episode subtitles updated successfully"}), 200
        
    except (psycopg2.DatabaseError, ValueError) as e:
        conn.rollback()
        return jsonify({"message": "Error updating episode subtitles", "error": str(e)}), 400

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({"message": "Resource not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"message": "Internal server error"}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False, threaded=True)
