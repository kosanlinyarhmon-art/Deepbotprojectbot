import os
import asyncio
import threading
import logging
import sys
import secrets
import re
import unicodedata
import time
import collections
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, InputMediaVideo)
from telegram.error import TelegramError, InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler, ChatMemberHandler, ApplicationHandlerStop, TypeHandler
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph

# ---------- Logging ----------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- Flask Server ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "Movie Bot is running!"

@app.route('/health')
def health():
    return "OK", 200


# ---------- Live status (heartbeat) ----------
BOOT_TS = time.time()


class _Ring(logging.Handler):
    """Keep the last N log lines in memory so /status can show them."""

    def __init__(self, cap=250):
        super().__init__()
        self.cap = cap
        self.buf = collections.deque(maxlen=cap)

    def emit(self, record):
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


RING = _Ring()
RING.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(RING)

LIVE = {
    "updates": 0,
    "last_update": 0.0,
    "last_update_kind": "",
    "polling": False,
    "label": "",
    "last_error": "",
    "merge_last": "",
    "loops": 0,
    "bots": {},
}


async def _hb(update, context):
    """Middleware-style counter so /status can prove polling is alive."""
    try:
        LIVE["updates"] += 1
        LIVE["last_update"] = time.time()
        LIVE["last_update_kind"] = (
            "message" if update.message else
            "callback" if update.callback_query else
            "member" if update.chat_member else "other")
        b = LIVE["bots"].get(str(context.bot.id))
        if b is not None:
            b["updates"] = b.get("updates", 0) + 1
            b["last_update"] = LIVE["last_update"]
    except Exception:
        pass
    return None


@app.route('/status')
def status():
    from flask import jsonify
    now = time.time()
    mask = re.compile(r"(bot\d{6,}:)[A-Za-z0-9_-]{30,}")
    logs = [mask.sub(r"\1***", ln) for ln in list(RING.buf)[-40:]]
    out = {
        "ok": True,
        "uptime_sec": int(now - BOOT_TS),
        "pid": os.getpid(),
        "polling": LIVE["polling"],
        "bot_label": LIVE["label"],
        "updates": LIVE["updates"],
        "last_update_age_sec": (int(now - LIVE["last_update"])
                                if LIVE["last_update"] else None),
        "last_update_kind": LIVE["last_update_kind"],
        "last_error": LIVE["last_error"],
        "merge_last": LIVE["merge_last"],
        "polling_loops": LIVE["loops"],
        "bots": LIVE["bots"],
        "threads": [t.name for t in threading.enumerate()],
        "mongo": "?",
        "forwarder": {},
        "logs": logs,
    }
    try:
        db.command("ping")
        out["mongo"] = "ok"
    except Exception as exc:
        out["mongo"] = f"ERR {exc}"
    try:
        import cloud_forwarder as cf
        cfg = cf.config()
        out["forwarder"] = {
            "enabled": bool(cfg.get("enabled")),
            "session_set": bool((cfg.get("session") or "").strip()),
            "thread_alive": bool(cf._task and cf._task.is_alive()),
            "client_connected": bool(cf._client),
            "stop_flag": bool(cf._stop),
            "last_log": getattr(cf, "LAST_LOG", ""),
            "last_log_age_sec": (int(now - cf.LAST_LOG_TS)
                                 if getattr(cf, "LAST_LOG_TS", 0) else None),
        }
    except Exception as exc:
        out["forwarder"] = {"error": str(exc)}
    return jsonify(out)


@app.route('/payload')
def payload_info():
    """Read-only check: how many files a deep-link payload really holds."""
    from flask import jsonify, request
    t = (request.args.get("t") or "").strip()
    if not t:
        return jsonify({"ok": False, "error": "missing t"}), 400
    doc = file_store_collection.find_one({"payload": t}) or {}
    files = doc.get("files") or []
    return jsonify({
        "ok": True,
        "payload": t,
        "count": len(files),
        "names": [f.get("file_name") for f in files],
        "captions": [f.get("file_caption") for f in files],
        "with_db_message": sum(1 for f in files
                               if f.get("channel_message_id")),
    })

# ---------- MongoDB Connection ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI environment variable not set!")
    sys.exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]

def init_stats():
    if stats_collection.count_documents({"_id": "total_requests"}) == 0:
        stats_collection.insert_one({"_id": "total_requests", "count": 0})
init_stats()

def get_total_requests():
    doc = stats_collection.find_one({"_id": "total_requests"})
    return doc["count"] if doc else 0

def increment_requests():
    stats_collection.update_one({"_id": "total_requests"}, {"$inc": {"count": 1}}, upsert=True)

def add_user(user_id):
    if not users_collection.find_one({"user_id": user_id}):
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now()})

def get_all_users():
    return [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]

# ---------- MongoDB structure for batch files ----------
def save_file_info(payload, file_id, file_name, file_caption=None):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        files = doc.get("files", [])
        if not any(f.get("file_id") == file_id for f in files):
            files.append({"file_id": file_id, "file_name": file_name, "file_caption": file_caption})
        file_store_collection.update_one(
            {"payload": payload},
            {"$set": {"files": files}}
        )
    else:
        file_store_collection.insert_one({
            "payload": payload,
            "files": [{"file_id": file_id, "file_name": file_name, "file_caption": file_caption}]
        })

def set_file_channel_message(payload, file_id, message_id):
    """Remember which DATABASE_CHANNEL message holds this file's media.

    Telegram ``file_id``s are bot-specific, so after a token swap the old ids
    cannot be sent any more. Copying from the database channel works for ANY
    bot that is a member there, which makes the files survive a bot change.
    """
    file_store_collection.update_one(
        {"payload": payload, "files.file_id": file_id},
        {"$set": {"files.$.channel_message_id": message_id}},
    )

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    return doc.get("files", []) if doc else []

def save_link_info(payload, url, text=None, label=None):
    """Attach a redirect link (deep link / post link) to a payload.

    Link entries let a post send the user to another bot or channel post; the
    optional ``text`` is shown as the message body above the link button.
    """
    entry = {"url": url, "text": text or "", "label": label or ""}
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        links = doc.get("links", [])
        links.append(entry)
        file_store_collection.update_one({"payload": payload}, {"$set": {"links": links}})
    else:
        file_store_collection.insert_one({
            "payload": payload,
            "files": [],
            "links": [entry],
        })

def get_link_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    return doc.get("links", []) if doc else []

# ---------- Migration for old documents ----------
def migrate_old_documents():
    docs = file_store_collection.find({"files": {"$exists": False}})
    for doc in docs:
        file_id = doc.get("file_id")
        file_name = doc.get("file_name")
        if file_id and file_name:
            file_store_collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"files": [{"file_id": file_id, "file_name": file_name}]}}
            )
        else:
            file_store_collection.delete_one({"_id": doc["_id"]})
    logger.info("Migration completed.")

# ---------- Telegram Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")


def _load_token_pairs():
    """Build the (token, username) list used for auto-failover.

    Primary is TELEGRAM_TOKEN / BOT_USERNAME. Spares are read from
    TELEGRAM_TOKEN_2 / BOT_USERNAME_2, TELEGRAM_TOKEN_3 / BOT_USERNAME_3, ...
    A dead token (revoked / banned / deleted bot) makes the bot fall through to
    the next spare automatically, keeping the same Mongo data and DB channel.
    """
    pairs = []
    if TOKEN and TOKEN.strip():
        pairs.append((TOKEN.strip(), (BOT_USERNAME or "").strip()))
    i = 2
    while True:
        t = os.environ.get(f"TELEGRAM_TOKEN_{i}") or os.environ.get(f"BOT_TOKEN_{i}")
        if not t or not t.strip():
            break
        u = os.environ.get(f"BOT_USERNAME_{i}") or os.environ.get(f"TELEGRAM_USERNAME_{i}")
        pairs.append((t.strip(), (u or "").strip()))
        i += 1
    return pairs


CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
MUSIC_CHANNEL_LINK = os.environ.get("MUSIC_CHANNEL_LINK", "")
OTHER_CHANNELS = [link.strip() for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link.strip()] if os.environ.get("OTHER_CHANNELS") else []
UPLOAD_CREDIT = os.environ.get("UPLOAD_CREDIT", "(Upload By WZN Cinema Hub Movies)")
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

# The channel owner is ALWAYS notified of join/leave events, on top of ADMIN_ID
# (override with the OWNER_ID env var if the owner ever changes).
OWNER_ID = int(os.environ.get("OWNER_ID", "1147922719"))

# Channels where the bot posts forwarded movies itself (never as a forward).
# CHANNEL_IDS = the movie channels; DATABASE_CHANNEL_ID = the private backup channel.

MOVIE_CHANNEL_IDS = [int(id.strip()) for id in os.environ.get("CHANNEL_IDS", "").split(",") if id.strip()] if os.environ.get("CHANNEL_IDS") else ([int(CHANNEL_ID)] if CHANNEL_ID else [])
DATABASE_CHANNEL_ID = os.environ.get("DATABASE_CHANNEL_ID", "")
POST_CHANNEL_IDS = list(MOVIE_CHANNEL_IDS)
if DATABASE_CHANNEL_ID:
    _db_chat = int(DATABASE_CHANNEL_ID.strip())
    if _db_chat not in POST_CHANNEL_IDS:
        POST_CHANNEL_IDS.append(_db_chat)

# Channels whose member join/leave we report to the admins (movie channels).
NOTIFY_CHANNEL_IDS = set(MOVIE_CHANNEL_IDS)

# ---------- Hard channel blocklist ----------
# Channels this bot must NEVER post to, whatever the environment says.
# -1003792838735 = https://t.me/everyboyhobby
# The owner does not want this bot posting there, so the ID is filtered out of
# every target list at startup (CHANNEL_IDS / CHANNEL_ID / DATABASE_CHANNEL_ID).
# Set BLOCKED_CHANNEL_IDS="" to disable the block again.
BLOCKED_CHANNEL_IDS = {
    int(x.strip())
    for x in os.environ.get("BLOCKED_CHANNEL_IDS", "-1003792838735").split(",")
    if x.strip()
}

_movie_before = list(MOVIE_CHANNEL_IDS)
MOVIE_CHANNEL_IDS = [c for c in MOVIE_CHANNEL_IDS if c not in BLOCKED_CHANNEL_IDS]
POST_CHANNEL_IDS = [c for c in POST_CHANNEL_IDS if c not in BLOCKED_CHANNEL_IDS]
NOTIFY_CHANNEL_IDS = {c for c in NOTIFY_CHANNEL_IDS if c not in BLOCKED_CHANNEL_IDS}

if DATABASE_CHANNEL_ID and int(DATABASE_CHANNEL_ID.strip()) in BLOCKED_CHANNEL_IDS:
    logger.warning(
        "DATABASE_CHANNEL_ID %s is blocked - DB channel posting disabled.",
        DATABASE_CHANNEL_ID,
    )
    DATABASE_CHANNEL_ID = ""

if _movie_before != MOVIE_CHANNEL_IDS:
    logger.warning(
        "Blocked channel(s) removed from movie targets: %s",
        sorted(set(_movie_before) - set(MOVIE_CHANNEL_IDS)),
    )
logger.info(
    "Effective movie channels: %s | DB channel: %r | blocked: %s",
    MOVIE_CHANNEL_IDS,
    DATABASE_CHANNEL_ID,
    sorted(BLOCKED_CHANNEL_IDS),
)

# ---------- Source-channel migration ----------
# userbot က အခြား channel တွေက post တွေကို ဒီ bot ဆီ forward လုပ်ပါတယ်။
# bot က အဲ့ဒါတွေကို ပုံ + ဇာတ်ညွှန်း + deeplink button ပုံစံနဲ့
# AUTO_PUBLISH_CHANNEL မှာ post အသစ် ပြန်တင်ပါတယ်။
_DEFAULT_SOURCES = [
    "-1002384798185",
    "-1002463885600",
    "-1002190088840",
    "-1002480433985",
    "-1002477764635",
]
SOURCE_CHANNEL_IDS = {
    int(x.strip())
    for x in (os.environ.get("SOURCE_CHANNEL_IDS")
              or ",".join(_DEFAULT_SOURCES)).split(",")
    if x.strip()
} | {int(x) for x in _DEFAULT_SOURCES}
try:
    AUTO_PUBLISH_CHANNEL = int(
        os.environ.get("AUTO_PUBLISH_CHANNEL", "-1003753299714").strip())
except ValueError:
    AUTO_PUBLISH_CHANNEL = 0
if AUTO_PUBLISH_CHANNEL in BLOCKED_CHANNEL_IDS:
    logger.warning("AUTO_PUBLISH_CHANNEL is blocked - migration disabled")
    AUTO_PUBLISH_CHANNEL = 0
MIGRATE_GAP = float(os.environ.get("MIGRATE_GAP", "3.2"))
migrated_collection = db["migrated_posts"]
if SOURCE_CHANNEL_IDS and AUTO_PUBLISH_CHANNEL:
    logger.info(
        "Migration enabled: %d source channel(s) -> %s",
        len(SOURCE_CHANNEL_IDS), AUTO_PUBLISH_CHANNEL,
    )

_MIG_EXTRA_CACHE = {"ts": 0.0, "ids": set()}


def _mig_extra_sources():
    """Channels the cloud forwarder joined from a link hub (cached 60s)."""
    now = time.time()
    if now - _MIG_EXTRA_CACHE["ts"] < 5:
        return _MIG_EXTRA_CACHE["ids"]
    try:
        doc = db["config"].find_one({"_id": "migration"}) or {}
        ids = {int(x) for x in (doc.get("extra_sources") or [])}
    except Exception:
        ids = set()
    _MIG_EXTRA_CACHE.update(ts=now, ids=ids)
    return ids


def _mig_add_extra_source(channel_id):
    """Remember a channel the admin forwarded from, so its posts publish."""
    try:
        db["config"].update_one(
            {"_id": "migration"},
            {"$addToSet": {"extra_sources": int(channel_id)}}, upsert=True)
        _MIG_EXTRA_CACHE["ts"] = 0.0
    except Exception as exc:
        logger.warning("extra source save failed: %s", exc)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

def generate_payload():
    return secrets.token_urlsafe(16)

# ---------- Telegram link (deep link / post link) parsing ----------
_TG_SCHEME_RE = re.compile(r"tg://[^\s<>\"']+", re.I)
_TG_WEB_RE = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/"
    r"(?:c/[0-9]+|[A-Za-z0-9_]{3,})"      # username, or /c/<internal-id>
    r"(?:/[0-9]+)?"                        # optional post message id
    r"(?:\?[^\s<>\"']*)?",                 # optional ?start=... deep-link query
    re.I,
)


def extract_telegram_link(text):
    """Return the first Telegram deep/post link found in *text*, else None.

    Accepted:
      * deep link  -> t.me/Bot?start=xxxx  (or tg://resolve?domain=Bot&start=...)
      * post link  -> t.me/channel/123  or  t.me/c/1234567890/123
    Anything that is not a real Telegram link (plain words, other URLs) returns
    None, so callers can reject non-link text.
    """
    if not text:
        return None
    m = _TG_SCHEME_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!)]}>\"'")
    m = _TG_WEB_RE.search(text)
    if not m:
        return None
    link = m.group(0).rstrip(".,;:!)]}>\"'")
    if not link.lower().startswith(("http://", "https://")):
        link = "https://" + link
    return link


def link_description(text, link):
    """Message body for a link entry: the admin's text with the URL removed."""
    if not text:
        return ""
    body = text.replace(link, " ")
    body = re.sub(r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/\S+", " ", body, flags=re.I)
    body = _TG_SCHEME_RE.sub(" ", body)
    return re.sub(r"[ \t]+", " ", body).strip()

async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name=BOT_USERNAME or 'MovieBot')
except:
    pass

async def create_telegraph_page(title: str, content_text: str) -> str:
    try:
        html_content = content_text.replace('\n', '<br>')
        response = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html_content}</p>",
            author_name="WZN Cinema Hub Movies"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph error: {e}")
        return None

# ---------- ===================== CRITICAL: UNIFIED FILENAME FUNCTION ===================== ----------
def clean_file_name(name):
    """Remove Telegram copy prefixes like 'Copy of [MCS] 18.' from filenames."""
    if not name:
        return name
    name = re.sub(r'^copy of\s+', '', name, flags=re.I)
    name = re.sub(r'^\[[^\]]*\]\s*\d+\.?\s*', '', name)
    return name.strip()


SEASON_EP_RE = re.compile(
    r'\bS\d{1,3}[\s\-–]?E\s?\d{1,4}\b'
    r'|\bS\s?\d{1,3}[\s\-–]?EP?\s?\d{1,4}\b'
    r'|\bSeason\s*\d{1,3}(?:\s*(?:and|&|,)?\s*Episode\s*\d{1,4})?'
    r'|\bEpisode\s*\d{1,4}\b',
    re.I)


def find_season_episode(text):
    """Return the season/episode marker ('S03E12', 'Season 3 Episode 12') in text, or ''."""
    if not text:
        return ""
    m = SEASON_EP_RE.search(text)
    if not m:
        return ""
    return re.sub(r'\s+', ' ', m.group(0)).strip()


# Real file extensions we must never override with '.mp4'.
# Lets the bot accept and deliver ANY file type, not just movies/videos.
COMMON_FILE_EXTS = {
    # video
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'mpg', 'mpeg',
    'ts', 'm2ts', '3gp', 'vob', 'ogv',
    # audio
    'mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'oga', 'opus', 'wma', 'amr',
    'm4b', 'ac3', 'mka',
    # archive
    'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'iso', 'tgz', 'cab',
    # documents
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'rtf', 'csv',
    'epub', 'mobi', 'odt', 'ods', 'odp', 'pages', 'numbers', 'key',
    # images
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'heic', 'svg', 'tif', 'tiff',
    # apps / other
    'apk', 'xapk', 'ipa', 'exe', 'msi', 'dmg', 'deb', 'rpm', 'bin', 'jar',
    'ttf', 'otf', 'woff', 'woff2', 'srt', 'ass', 'ssa', 'vtt', 'sub',
    'json', 'xml', 'yaml', 'yml', 'html', 'htm', 'js', 'css', 'py', 'sh', 'bat',
}

# Scene/quality tags that look like an extension but are not a file type.
_SCENE_TOKENS = {
    '1080p', '720p', '480p', '2160p', '4k', '8k', 'hd', 'sd', 'fhd', 'uhd',
    'web', 'webrip', 'webdl', 'web-dl', 'bluray', 'blu-ray', 'brrip', 'bdrip',
    'dvdrip', 'hdtv', 'hdrip', 'dvdscr', 'cam', 'tc', 'r5', 'x264', 'x265',
    'h264', 'h265', 'hevc', 'avc', 'aac', 'ac3', 'dd5', 'dts', '10bit',
    '8bit', 'proper', 'repack', 'extended', 'remastered', 'imax', 'multi',
    'dual', 'subs', 'esub', 'dubbed',
}


def _has_real_extension(name):
    """Return True when the filename ends with a believable file extension."""
    m = re.search(r'\.([A-Za-z0-9+]{1,8})$', name)
    if not m:
        return False
    ext = m.group(1)
    low = ext.lower()
    if low in COMMON_FILE_EXTS:
        return True
    if low in _SCENE_TOKENS:
        return False
    if find_season_episode(ext):
        return False
    if re.fullmatch(r'\d{3,4}[pi]', low):
        return False
    return 1 <= len(ext) <= 5 and bool(re.search(r'[A-Za-z]', ext))


def preserve_extension(name, default=".mp4"):
    """Keep a file's real extension; add the default only when there is none.

    'Ted.Lasso.S03E12.1080p' -> 'Ted.Lasso.S03E12.1080p.mp4'
    'archive.zip'            -> 'archive.zip'
    'Subtitle.S01E02.srt'    -> 'Subtitle.S01E02.srt'
    """
    if not name:
        return name
    if _has_real_extension(name):
        return name
    return name + default


def to_ascii_digits(text):
    """Convert Unicode decimal digits (e.g. Burmese ၁၀၈၀) to ASCII digits (1080).

    Must run BEFORE Myanmar text is stripped, otherwise the digits vanish with
    the rest of the Myanmar block.
    """
    if not text:
        return text
    out = []
    for ch in text:
        if ch.isdigit():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


_QUALITY_RE = re.compile(r'(?<![A-Za-z0-9])(\d{3,4})\s*[pP](?![A-Za-z0-9])')


def extract_quality(text):
    """Return the resolution tag from a caption/filename as English '1080p'/'720p'.

    Understands Burmese digits too ('၁၀၈၀p' -> '1080p'). Returns '' when none.
    """
    if not text:
        return ""
    m = _QUALITY_RE.search(to_ascii_digits(text))
    return f"{m.group(1)}p" if m else ""


def extract_movie_name(text):
    """Pull the movie name out of a caption that mixes it with ads/links/junk.

    '♻️(CM) Ted Lasso.S03E12.So.Long.Farewell.1080p.mp4\nJoin Channel...' -> 'Ted Lasso.S03E12.So.Long.Farewell.1080p.mp4'
    Season/episode markers ('(S03E12) Title.mkv') are always preserved.
    """
    if not text:
        return ""
    se = find_season_episode(text)
    m = re.search(r'[\w .\-()\'&,]+\.(?:mp4|mkv|avi|mov|wmv|flv|webm)', text, re.I)
    if m:
        name = m.group(0).strip()
        name = re.sub(r'^copy of\s+', '', name, flags=re.I)
        name = re.sub(r'^[^\w()\[\]]+', '', name)
        name = re.sub(r'^\[[^\]]*\]\s*', '', name)
        name = re.sub(r'^\d{1,3}\.\s*(?=[A-Za-z])', '', name)
        paren = re.match(r'^\(([^)]*)\)\s*', name)
        if paren and not find_season_episode(paren.group(1)):
            name = re.sub(r'^\([^)]*\)\s*', '', name)
        name = re.sub(r'\s{2,}', ' ', name).strip()
        if se and not find_season_episode(name):
            name = f"{se} {name}".strip()
        return name
    # No file extension in the caption: keep the first non-junk line as the name.
    junk_re = re.compile(
        r'filesize|duration|join|subscribe|vip|ads?\b|https?://|t\.me|'
        r'quality|language|imdb|rated\b|directed|\bcast\b|release|channel', re.I)
    label_re = re.compile(r'^(?:file\s*name|filename|title|name)\s*[:\-–]?\s*', re.I)
    for raw_line in text.split('\n'):
        line = label_re.sub('', clean_caption_text_only(raw_line)).strip()
        if line and not junk_re.search(line):
            if se and not find_season_episode(line):
                line = f"{se} {line}".strip()
            return line
    return ""


def clean_caption_text_only(text):
    """Strip Myanmar text, URLs and emojis from a caption while keeping its
    punctuation/word structure (used for filename generation)."""
    if not text:
        return text
    text = strip_notice(text)
    text = re.sub(r'https?://[^\s]+', ' ', text, flags=re.I)
    text = re.sub(r't\.me/[A-Za-z0-9_+\-]+', ' ', text, flags=re.I)
    text = to_ascii_digits(text)
    text = re.sub(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+', ' ', text)
    text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def get_video_name(video, caption=None, poster_caption=None, fallback="movie.mp4"):
    """
    UNIFIED function to get video name with priority:
    1. Original filename (from video file) — the real name on the uploader's computer
    2. Caption (from video message)
    3. Poster caption (first line, for /newpost)
    4. Fallback
    """
    # ၁။ မူလဖိုင်နာမည်ကို ဦးစားပေးယူမယ် (computer ထဲမှာ save ထားတဲ့ နာမည်)
    original = getattr(video, 'file_name', None)
    if original:
        name = clean_caption_text_only(clean_file_name(re.sub(r'\s+', ' ', original).strip()))
        if not name:
            name = clean_file_name(re.sub(r'\s+', ' ', original).strip())
        if name:
            return preserve_extension(name)
    
    # ၂။ Original filename မရှိရင် caption ကိုယူမယ်
    if caption:
        name = extract_movie_name(caption)
        if not name:
            name = clean_caption_text_only(re.sub(r'\s+', ' ', caption).strip())
        if not name:
            name = clean_file_name(re.sub(r'\s+', ' ', caption).strip())
        if name:
            return preserve_extension(name)
    
    # ၃။ Poster caption ကနေယူမယ် (/newpost အတွက်)
    if poster_caption:
        lines = poster_caption.strip().split('\n')
        name = clean_caption_text_only(lines[0].strip())
        if not name:
            name = clean_file_name(lines[0].strip())
        if name:
            if len(name) > 100:
                name = name[:97] + "..."
            return preserve_extension(name)
    
    # ၄။ အကုန်မရှိရင် fallback
    return fallback

# ---------- Start Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_list = get_file_info(payload)
        link_list = get_link_info(payload)
        if file_list or link_list:
            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 Channel သို့ဝင်ရန်: {INVITE_LINK}",
                    disable_web_page_preview=True
                )
                return

            delivered_message_ids = []

            for file_info in file_list:
                file_id = file_info["file_id"]
                file_name = file_info.get("file_name")
                if not file_name:
                    file_name = "movie.mp4"
                file_name = re.sub(r'\s+', ' ', file_name).strip()
                file_name = preserve_extension(file_name)
                stored_caption = file_info.get("file_caption") or ""
                movie_name_from_caption = extract_movie_name(stored_caption)
                delivery_caption = clean_caption(movie_name_from_caption or stored_caption or f"🎬 {file_name}")
                delivery_caption = append_upload_credit(delivery_caption)
                if len(delivery_caption) > 1024:
                    delivery_caption = delivery_caption[:1020].rstrip() + "..."
                try:
                    # Prefer copying from the database channel: it works for any
                    # bot that is a member there, even after the token changes
                    # (file_ids are bot-specific and would break on a new bot).
                    channel_message_id = file_info.get("channel_message_id")
                    sent_file = None
                    if DATABASE_CHANNEL_ID and channel_message_id:
                        try:
                            sent_file = await context.bot.copy_message(
                                chat_id=user_id,
                                from_chat_id=int(DATABASE_CHANNEL_ID.strip()),
                                message_id=int(channel_message_id),
                                caption=delivery_caption,
                            )
                        except TelegramError as e:
                            logger.warning(
                                "copy_message from DB channel failed for %s: %s", file_name, e)
                    if sent_file is None:
                        sent_file = await context.bot.send_document(
                            chat_id=user_id,
                            document=file_id,
                            filename=file_name,
                            caption=delivery_caption
                        )
                    delivered_message_ids.append(sent_file.message_id)
                except Exception as e:
                    await context.bot.send_message(chat_id=user_id, text=f"❌ {file_name} ပို့ရာတွင် အမှား: {str(e)}")

            # Redirect links (deep link / post link): text + inline URL button.
            # These point the user to another bot or channel post and are left
            # in the chat (not auto-deleted).
            for ln in link_list:
                url = (ln.get("url") or "").strip()
                if not url:
                    continue
                body = (ln.get("text") or "").strip() or "🔗 ဒီလင့်ကနေ ဆက်လက်ကြည့်ရှုပါ"
                label = (ln.get("label") or "").strip() or "🔗 လင့်ဖွင့်ရန်"
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=body,
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton(label, url=url)]]
                        ),
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error("Failed to send link entry %s: %s", url, e)

            if delivered_message_ids:
                warning_text = (
                    "⚠️ ⚠️ ⚠️ အရေးကြီးပါတယ် ⚠️ ⚠️ ⚠️\n\n"
                    "ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n\n"
                    "ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n\n"
                    "ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n\n"
                    "👉Channel ကို Share ခြင်းဖြင့်လည်း ကူညီနိုင်ပါတယ်။\n"
                    "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n!!! IMPORTANT !!!\n"
                    "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
                    "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
                )
                warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)

                async def delete_after():
                    await asyncio.sleep(300)
                    tasks = []
                    for mid in delivered_message_ids:
                        tasks.append(
                            context.bot.delete_message(chat_id=user_id, message_id=mid)
                        )
                    tasks.append(
                        context.bot.delete_message(
                            chat_id=user_id, message_id=warn_msg.message_id
                        )
                    )
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    deleted = sum(1 for r in results if not isinstance(r, Exception))
                    logger.info(f"Auto-deleted {deleted}/{len(tasks)} delivered messages for user {user_id}")
                asyncio.create_task(delete_after())

            add_user(user_id)
            increment_requests()

            keyboard = []
            if OTHER_CHANNELS:
                for idx, link in enumerate(OTHER_CHANNELS, 1):
                    if idx == 1:
                        keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                    elif idx == 2:
                        keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                    elif idx == 3:
                        keyboard.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
                    else:
                        keyboard.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
            if MUSIC_CHANNEL_LINK:
                keyboard.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel အတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။",
                parse_mode="Markdown"
            )

# ---------- Admin Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 ပို့စ်အသစ်", callback_data="menu_newpost")],
        [InlineKeyboardButton("🔗 Video → Deep Link", callback_data="menu_link")],
        [InlineKeyboardButton("📦 Batch Link ထုတ်ရန်", callback_data="menu_batch")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 ပြန်လွှင့်ခြင်း", callback_data="menu_broadcast")],
        [InlineKeyboardButton("⏰ Schedule ပြုလုပ်ရန်", callback_data="menu_schedule")],
        [InlineKeyboardButton("📋 Schedule စာရင်း", callback_data="menu_listschedule")],
        [InlineKeyboardButton("❌ Schedule ဖျက်ရန်", callback_data="menu_cancelschedule")],
        [InlineKeyboardButton("🗑️ ဖိုင်ဖျက်ရန် (ID)", callback_data="menu_delete")],
        [InlineKeyboardButton("⚠️ ဖိုင်အားလုံးဖျက်ရန်", callback_data="menu_deleteall")],
        [InlineKeyboardButton("🔇 Maintenance mode ဖွင့်", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Maintenance mode ပိတ်", callback_data="menu_unmute")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🤖 **Admin Menu**\n\nအောက်ပါခလုတ်များကို နှိပ်ပါ။", reply_markup=reply_markup, parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return

    data = query.data
    if data == "menu_newpost":
        await query.edit_message_text("📸 `/newpost` command ကို သုံးပါ။ (Post ဖန်တီးရန်)")
    elif data == "menu_link":
        await query.edit_message_text("🔗 `/link` command ကို သုံးပါ။ (Video ပို့ပါက Deep Link ရမည်)")
    elif data == "menu_batch":
        await query.edit_message_text("📦 `/batchlink` command ကို သုံးပါ။ (ဖိုင်အများကြီးကို link တစ်ခုတည်းနဲ့ ချိတ်ရန်)")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_files = file_store_collection.count_documents({})
        await query.edit_message_text(
            f"📊 **စာရင်းအင်း**\n\n"
            f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
            f"🎬 ဖိုင်အရေအတွက်: {total_files}",
            parse_mode="Markdown"
        )
    elif data == "menu_broadcast":
        await query.edit_message_text("📢 `/broadcast <message>` ဖြင့် အသုံးပြုသူအားလုံးကို စာပို့နိုင်ပါသည်။")
    elif data == "menu_schedule":
        await query.edit_message_text("⏰ `/schedule` command ကို သုံးပါ။ (အဆင့်လိုက်မေးပါမည်)")
    elif data == "menu_listschedule":
        await query.edit_message_text("📋 `/listschedule` ဖြင့် schedule စာရင်းကြည့်ပါ။")
    elif data == "menu_cancelschedule":
        await query.edit_message_text("❌ `/cancelschedule <id>` ဖြင့် schedule ဖျက်ပါ။")
    elif data == "menu_delete":
        await query.edit_message_text("🗑️ `/delete <file_id>` ဖြင့် ဖိုင်ဖျက်ပါ။")
    elif data == "menu_deleteall":
        await query.edit_message_text("⚠️ `/deleteall` ဖြင့် ဖိုင်အားလုံးဖျက်ပါ။ (အတည်ပြုမေးမည်)")
    elif data == "menu_mute":
        maintenance_mode = True
        await query.edit_message_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")
    elif data == "menu_unmute":
        maintenance_mode = False
        await query.edit_message_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

# ---------- ===================== /link Command ===================== ----------
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 ဖိုင် တစ်ခု ပို့ပေးပါ။ (မည်သည့်ဖိုင်အမျိုးအစားမဆို ရပါသည်)\nCaption မှာ မြန်မာလိုနာမည်ထည့်ပေးပါ။")
    context.user_data['waiting_for_video_link'] = True

async def handle_video_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get('waiting_for_video_link'):
        return
    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text("ဖိုင် တစ်ခု ပို့ပေးပါ။")
        return
    try:
        payload = generate_payload()
        caption = update.message.caption
        original_name = get_original_filename(video)
        file_name = get_video_name(video, caption, None, "ဇာတ်ကား")
        save_file_info(payload, video.file_id, file_name, extract_movie_name(caption) or caption or None)

        datab_report = ""
        if DATABASE_CHANNEL_ID:
            try:
                clean_cap = clean_caption(
                    extract_movie_name(caption) or caption or f"🎬 {file_name}")
                clean_cap = append_upload_credit(clean_cap)
                if len(clean_cap) > 1024:
                    clean_cap = clean_cap[:1020].rstrip() + "..."
                sent_db = await context.bot.send_document(
                    chat_id=int(DATABASE_CHANNEL_ID.strip()),
                    document=video.file_id,
                    filename=original_name or file_name,
                    caption=clean_cap,
                )
                set_file_channel_message(payload, video.file_id, sent_db.message_id)
                datab_report = "\n\n🗄️ DB Channel ထဲ သိမ်းပြီးပါပြီ။"
            except Exception as db_err:
                logger.error(f"/link DB post failed: {db_err}")
                datab_report = "\n\n⚠️ DB Channel ထဲ မသိမ်းနိုင်ပါ။"
        else:
            datab_report = "\n\n⚠️ DATABASE_CHANNEL_ID မရှိပါ။"

        deep_link = create_deep_linked_url(context.bot.username, payload)
        await update.message.reply_text(
            f"သင်၏ ဇာတ်ကားရယူရန် လင့်\n\n"
            f"{deep_link}\n\n"
            f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် ({file_name}) ကို ချက်ချင်းရရှိမည်။\n"
            f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
            f"{datab_report}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
    finally:
        context.user_data.pop('waiting_for_video_link', None)

# ---------- ===================== /newfile Command ===================== ----------
async def newfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 ဖိုင် တစ်ခု ပို့ပေးပါ။ (မည်သည့်ဖိုင်အမျိုးအစားမဆို ရပါသည်)\nCaption မှာ မြန်မာလိုနာမည်ထည့်ပေးပါ။")
    context.user_data['waiting_for_newfile'] = True

async def handle_video_for_newfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_newfile'):
        video = update.message.video or update.message.document
        if video:
            try:
                payload = generate_payload()
                caption = update.message.caption
                file_name = get_video_name(video, caption, None, "ဇာတ်ကား")
                save_file_info(payload, video.file_id, file_name, extract_movie_name(caption) or caption or None)
                deep_link = create_deep_linked_url(context.bot.username, payload)
                await update.message.reply_text(
                    f"သင်၏ ဇာတ်ကားရယူရန် လင့်\n\n"
                    f"{deep_link}\n\n"
                    f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် ({file_name}) ကို ချက်ချင်းရရှိမည်။\n"
                    f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
            context.user_data.pop('waiting_for_newfile', None)
        else:
            await update.message.reply_text("ဖိုင် တစ်ခု ပို့ပေးပါ။")

# ---------- ===================== /batchlink Command ===================== ----------
BATCH_WAITING_FILES, BATCH_DONE = range(2)

# ---------- ===================== /batchlink Command ===================== ----------
BATCH_WAITING_FILES, BATCH_DONE = range(2)


async def batchlink_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text(
        "📤 ဖိုင်များကို တစ်ခါတည်း သို့မဟုတ် တစ်ခုချင်း ပို့ပါ။ (မည်သည့်ဖိုင်အမျိုးအစားမဆို ရပါသည်)\n"
        "Caption မှာ မြန်မာလိုနာမည်ထည့်ပေးပါ။\n"
        "🔗 တခြား bot/channel ရဲ့ လင့် (Deep link / Post link) ကိုလည်း ပို့နိုင်ပါတယ်။\n"
        "အားလုံးပြီးပါက /done ကိုနှိပ်ပါ။"
    )
    context.user_data['batch_files'] = []
    context.user_data['batch_links'] = []
    return BATCH_WAITING_FILES

async def batch_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    video = update.message.video or update.message.document
    if not video:
        await update.message.reply_text("ဖိုင် တစ်ခု ပို့ပါ။")
        return BATCH_WAITING_FILES

    file_id = video.file_id
    caption = update.message.caption
    file_name = get_video_name(video, caption, None, f"video_{len(context.user_data.get('batch_files', [])) + 1}")
    original_name = get_original_filename(video)

    batch_files = context.user_data.get('batch_files', [])
    batch_files.append({"file_id": file_id, "file_name": file_name, "original_name": original_name, "original_caption": extract_movie_name(caption) or caption or ""})
    context.user_data['batch_files'] = batch_files
    count = len(batch_files)
    await update.message.reply_text(f"✅ {file_name} ကို လက်ခံရရှိပါပြီ။ (စုစုပေါင်း {count} ဖိုင်)")
    return BATCH_WAITING_FILES

async def batch_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept a deep link / post link (with optional text) inside a batch.

    Only a valid Telegram link is accepted; plain text is rejected.
    """
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = update.message.text or ""
    link = extract_telegram_link(text)
    if link:
        links = context.user_data.get('batch_links', [])
        links.append({"url": link, "text": link_description(text, link)})
        context.user_data['batch_links'] = links
        await update.message.reply_text(
            f"🔗 လင့် ကို လက်ခံရရှိပါပြီ။ (စုစုပေါင်း {len(links)} လင့်)\n"
            f"နောက်ထပ် ဖိုင်/လင့် ရှိလျှင် ထပ်ပို့ပါ။ ပြီးပါက /done နှိပ်ပါ။"
        )
        return BATCH_WAITING_FILES
    if text.strip():
        await update.message.reply_text(
            "❌ ဒါက တရားဝင် လင့် မဟုတ်ပါ။\n"
            "Deep link (t.me/Bot?start=...) သို့မဟုတ် Post link (t.me/channel/123) ပို့ပါ။\n"
            "ပြီးပါက /done နှိပ်ပါ။"
        )
    return BATCH_WAITING_FILES

async def batch_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    files = context.user_data.get('batch_files', [])
    links = context.user_data.get('batch_links', [])
    if not files and not links:
        await update.message.reply_text("❌ ဖိုင်/လင့် မရှိပါ။ ထပ်မံစတင်ပါ။")
        return ConversationHandler.END
    payload = generate_payload()
    for f in files:
        save_file_info(payload, f['file_id'], f['file_name'], f.get('original_caption') or None)
    for ln in links:
        save_link_info(payload, ln['url'], ln.get('text'))
    deep_link = create_deep_linked_url(context.bot.username, payload)
    file_names = "\n".join([f"🎬 {f['file_name']}" for f in files])
    link_lines = "\n".join([f"🔗 {ln['url']}" for ln in links])

    datab_report = ""
    if DATABASE_CHANNEL_ID:
        db_chat = int(DATABASE_CHANNEL_ID.strip())
        db_ok = 0
        db_fail = []
        for f in files:
            try:
                clean_cap = clean_caption(f.get('original_caption') or f"🎬 {f.get('original_name') or f['file_name']}")
                clean_cap = append_upload_credit(clean_cap)
                if len(clean_cap) > 1024:
                    clean_cap = clean_cap[:1020].rstrip() + "..."
                sent_db = None
                for attempt in range(3):
                    try:
                        sent_db = await context.bot.send_document(
                            chat_id=db_chat,
                            document=f['file_id'],
                            filename=f.get('original_name') or f['file_name'],
                            caption=clean_cap,
                        )
                        break
                    except TelegramError as e:
                        if "flood" in str(e).lower() or "retry" in str(e).lower():
                            wait = 10 * (attempt + 1)
                            logger.warning(f"Flood control hit, waiting {wait}s: {e}")
                            await asyncio.sleep(wait)
                            continue
                        raise
                if sent_db is not None:
                    set_file_channel_message(payload, f['file_id'], sent_db.message_id)
                db_ok += 1
                await asyncio.sleep(2)
            except TelegramError as e:
                db_fail.append(f['file_name'])
                logger.error(f"Batch DB post failed for {f['file_name']}: {e}")
        datab_report = (
            f"\n\n🗄️ Database channel: ဖိုင် {db_ok}/{len(files)} တင်ပြီး။"
            + (f"\n⚠️ မတင်နိုင်တဲ့ဖိုင်များ: {', '.join(db_fail)}" if db_fail else "")
        )
    else:
        datab_report = "\n\n⚠️ DATABASE_CHANNEL_ID မရှိပါ။";

    await update.message.reply_text(
        f"✅ Batch Link ဖန်တီးပြီးပါပြီ။\n\n"
        f"ဖိုင်များ:\n{file_names}"
        + (f"\n\n🔗 လင့်များ:\n{link_lines}" if links else "")
        + f"\n\nလင့်: {deep_link}"
        + f"{datab_report}"
    )
    context.user_data.pop('batch_files', None)
    context.user_data.pop('batch_links', None)
    return ConversationHandler.END

async def batch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.pop('batch_files', None)
    context.user_data.pop('batch_links', None)
    return ConversationHandler.END

# ---------- ===================== /newpost Command ===================== ----------
POSTER, CAPTION, VIDEO_FILE, WAITING_VIDEO = range(4)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    context.user_data['newpost_videos'] = []
    context.user_data['newpost_links'] = []
    await update.message.reply_text("📸 ဇာတ်ကားအတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
    return POSTER

async def receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    context.user_data['caption_parts'] = []
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား (ဇာတ်ညွှန်း) ရေးပေးပါ...\n(စာသားရှည်ပါက ၂ ခါခွဲပို့နိုင်ပါသည်။ ပြီးပါက 'a' ရိုက်ပါ။)")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.lower() == 'a':
        caption_parts = context.user_data.get('caption_parts', [])
        if not caption_parts:
            await update.message.reply_text("⚠️ ဇာတ်ညွှန်း စာသား မရှိသေးပါ။ စာသား ပို့ပေးပါ။")
            return CAPTION
        full_caption = "\n\n".join(caption_parts)
        context.user_data['caption_full'] = full_caption
        context.user_data['telegraph_url'] = None

        if len(full_caption) > 1024:
            title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                page_url = await create_telegraph_page(title, full_caption)
                if page_url:
                    context.user_data['telegraph_url'] = page_url
                    await update.message.reply_text(f"✅ Telegraph စာမျက်နှာ ဖန်တီးပြီးပါပြီ။\n\nဇာတ်ညွှန်းအပြည့်အစုံကို ဤလင့်တွင် ဖတ်ရှုနိုင်ပါသည်။\n{page_url}")
                else:
                    await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် အမှားရှိသည်။ စာသားကို ဆက်လက်အသုံးပြုပါမည်။")
            except Exception as e:
                logger.error(f"Telegraph error: {e}")
                await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် ချို့ယွင်းချက်ရှိသည်။")

        await update.message.reply_text(
            "📁 ဖိုင်များ ပို့ပေးပါ... (မည်သည့်ဖိုင်အမျိုးအစားမဆို လက်ခံပါသည်၊ အများကြီးပို့နိုင်ပါသည်)\n"
            "🔗 တခြား bot/channel ရဲ့ လင့် (Deep link / Post link) ကိုလည်း ပို့နိုင်ပါတယ်။\n"
            "ဖိုင်ရဲ့ Caption မှာ နာမည်ထည့်ပေးနိုင်ပါတယ်။\n"
            "အားလုံးပြီးပါက 'a' ရိုက်ပါ။"
        )
        return WAITING_VIDEO
    else:
        caption_parts = context.user_data.get('caption_parts', [])
        caption_parts.append(text)
        context.user_data['caption_parts'] = caption_parts
        await update.message.reply_text(f"✅ ဇာတ်ညွှန်းအပိုင်း {len(caption_parts)} ကို လက်ခံရရှိပါပြီ။\n\nနောက်ထပ်အပိုင်းရှိလျှင် ထပ်ပို့ပါ။ ပြီးပါက 'a' ကို ရိုက်ပါ။")
        return CAPTION

async def receive_video_after_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept ANY file the admin sends (video, or a document of any type) —
    # not just movie/video files.
    video = update.message.video or update.message.document

    if not video:
        text = update.message.text or ""
        if text.strip().lower() == 'a':
            return await finalize_newpost(update, context)

        # Not a file: accept an admin-provided redirect link (deep / post link).
        # Plain text (no valid Telegram link) is rejected.
        link = extract_telegram_link(text)
        if link:
            links = context.user_data.get('newpost_links', [])
            links.append({"url": link, "text": link_description(text, link)})
            context.user_data['newpost_links'] = links
            await update.message.reply_text(
                f"🔗 လင့် ကို လက်ခံရရှိပါပြီ။ (စုစုပေါင်း {len(links)} လင့်)\n"
                f"နောက်ထပ် ဖိုင်/လင့် ရှိလျှင် ထပ်ပို့ပါ။ အားလုံးပြီးပါက 'a' ရိုက်ပါ။"
            )
            return WAITING_VIDEO
        if text.strip():
            await update.message.reply_text(
                "❌ ဒါက တရားဝင် လင့် မဟုတ်ပါ။\n"
                "Deep link (t.me/Bot?start=...) သို့မဟုတ် Post link (t.me/channel/123) ပို့ပါ။\n"
                "ဆက်လုပ်ရန် 'a' ရိုက်ပါ။"
            )
        return WAITING_VIDEO

    caption = update.message.caption
    poster_caption = context.user_data.get('caption_full', '')
    original_name = get_original_filename(video)

    script_first_line = ""
    if poster_caption:
        lines = [ln.strip() for ln in poster_caption.split('\n') if ln.strip()]
        if lines:
            script_first_line = clean_file_name(re.sub(r'\s+', ' ', lines[0]).strip())

    caption_movie_name = extract_movie_name(caption) if caption else ""

    clean_original = ""
    if original_name and not original_name.lower().startswith(('movie', 'video_')):
        clean_original = original_name

    # A Burmese (Myanmar) movie-file caption is unusable as a name (Myanmar text
    # is stripped), so fall back to the movie name from the synopsis first line.
    caption_is_myanmar = bool(
        caption and re.search(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]', to_ascii_digits(caption)))
    script_name = clean_caption_text_only(script_first_line) if script_first_line else ""

    # Resolution tag (720p/1080p) from the file caption or its original filename.
    quality = extract_quality(caption) or extract_quality(original_name)

    if caption_is_myanmar and script_name:
        file_name = script_name
        movie_caption = script_name
    elif caption_movie_name:
        file_name = caption_movie_name
        movie_caption = caption_movie_name
    elif clean_original:
        file_name = clean_original
        movie_caption = clean_original
    elif script_first_line:
        file_name = script_first_line
        movie_caption = script_first_line
    else:
        file_name = get_video_name(video, None, None, "ဇာတ်ကား")
        movie_caption = file_name

    se = find_season_episode(original_name) or find_season_episode(caption) or find_season_episode(script_first_line)
    if se and not find_season_episode(movie_caption):
        movie_caption = f"{movie_caption} {se}".strip()
    if se and not find_season_episode(file_name):
        file_name = f"{file_name} {se}".strip()

    # Keep the resolution (720p/1080p) that came with the file, even when we
    # switched the name to the synopsis title.
    if quality and not extract_quality(movie_caption):
        movie_caption = f"{movie_caption} {quality}".strip()
        file_name = f"{file_name} {quality}".strip()

    file_name = preserve_extension(file_name)

    movie_caption = clean_caption_text_only(movie_caption)

    videos = context.user_data.get('newpost_videos', [])
    videos.append({
        "file_id": video.file_id,
        "file_name": file_name,
        "original_name": original_name,
        "original_caption": movie_caption or "",
        "is_video": bool(update.message.video),
    })
    context.user_data['newpost_videos'] = videos
    await update.message.reply_text(
        f"✅ {file_name} ကို လက်ခံရရှိပါပြီ။ (စုစုပေါင်း {len(videos)} ဖိုင်)\n"
        f"နောက်ထပ် ဖိုင်ရှိလျှင် ထပ်ပို့ပါ။ အားလုံးပြီးပါက 'a' ရိုက်ပါ။"
    )
    return WAITING_VIDEO


async def finalize_newpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    videos = context.user_data.get('newpost_videos', [])
    links = context.user_data.get('newpost_links', [])
    if not videos and not links:
        await update.message.reply_text("ဖိုင် သို့မဟုတ် လင့် မပို့ရသေးပါ။ ဦးစွာ ပို့ပေးပါ။")
        return WAITING_VIDEO

    try:
        payload = generate_payload()
        for v in videos:
            save_file_info(payload, v['file_id'], v['file_name'], v.get('original_caption') or None)
        for ln in links:
            save_link_info(payload, ln['url'], ln.get('text'))
        deep_link = create_deep_linked_url(context.bot.username, payload)
        file_names = "\n".join([f"🎬 {v['file_name']}" for v in videos])
        total = len(videos)

        buttons = []
        buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
        synopsis_url = context.user_data.get('telegraph_url')
        if synopsis_url:
            buttons.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်အစုံဖတ်ရန်", url=synopsis_url)])
        if OTHER_CHANNELS:
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                if idx == 1:
                    buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                elif idx == 2:
                    buttons.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                elif idx == 3:
                    buttons.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
                else:
                    buttons.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
        if MUSIC_CHANNEL_LINK:
            buttons.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

        reply_markup = InlineKeyboardMarkup(buttons)

        poster = context.user_data.get('poster')
        caption_full = context.user_data.get('caption_full', '')
        telegraph_url = context.user_data.get('telegraph_url')

        if not poster:
            await update.message.reply_text("ပုံ မတွေ့ပါ။ /newpost ကို ထပ်မံစတင်ပါ။")
            context.user_data.clear()
            return ConversationHandler.END

        caption_lines = [ln.strip() for ln in caption_full.split('\n') if ln.strip()]
        movie_name = clean_caption(caption_lines[0]) if caption_lines else ""
        synopsis_body = strip_notice("\n".join(caption_lines[1:]).strip()) if len(caption_lines) > 1 else ""

        if telegraph_url:
            preview = synopsis_body[:300] + "..." if len(synopsis_body) > 300 else synopsis_body
            header = "📝 ဇာတ်ကားအကျဉ်းချုပ်"
        else:
            preview = synopsis_body[:1000] + "..." if len(synopsis_body) > 1000 else synopsis_body
            header = "📝 ဇာတ်ကားအကြောင်း"

        movie_name = movie_name[:200]
        if movie_name and preview:
            photo_caption = f"{movie_name}\n\n{header}\n\n{preview}"
        elif movie_name:
            photo_caption = f"{movie_name}\n\n{header}"
        elif preview:
            photo_caption = f"{header}\n\n{preview}"
        else:
            photo_caption = header

        # Telegram caption limit is 1024 characters — enforce it.
        if len(photo_caption) > 1024:
            photo_caption = photo_caption[:1020].rstrip() + "..."

        await update.message.reply_photo(photo=poster, caption=photo_caption, reply_markup=reply_markup)
        await update.message.reply_text(
            f"သင်၏ ဇာတ်ကားရယူရန် လင့်\n\n"
            f"{deep_link}\n\n"
            f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် Movie {total} ဖိုင် အားလုံးကို ချက်ချင်းရရှိမည်။\n"
            f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
        )

        # Auto-post to the movie channels: poster photo + caption + buttons.
        movie_posted = 0
        movie_failed = []
        for chat_id in MOVIE_CHANNEL_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=poster,
                    caption=photo_caption,
                    reply_markup=reply_markup,
                )
                movie_posted += 1
            except Exception as e:
                movie_failed.append(chat_id)
                logger.error(f"Auto movie-channel post failed to {chat_id}: {e}")

        # Auto-post to the database channel, in order: poster photo → synopsis → movie files.
        db_ok = 0
        db_fail = []
        if DATABASE_CHANNEL_ID:
            db_chat = int(DATABASE_CHANNEL_ID.strip())

            # 1) Poster photo with only the movie name as caption
            # (the full synopsis is posted separately below as its own message).
            # Dashes are kept; other special chars and Myanmar text are cleaned.
            db_poster_name = clean_caption_db(caption_lines[0]) if caption_lines else ""
            db_poster_caption = db_poster_name or photo_caption
            try:
                for attempt in range(3):
                    try:
                        await context.bot.send_photo(
                            chat_id=db_chat,
                            photo=poster,
                            caption=db_poster_caption,
                        )
                        break
                    except TelegramError as e:
                        if "flood" in str(e).lower() or "retry" in str(e).lower():
                            wait = 10 * (attempt + 1)
                            logger.warning(f"Flood control hit (DB poster), waiting {wait}s: {e}")
                            await asyncio.sleep(wait)
                            continue
                        raise
                db_ok += 1
                await asyncio.sleep(2)
            except Exception as e:
                db_fail.append("ပုံ (poster)")
                logger.error(f"Auto database-channel poster post failed: {e}")

            # 2) Synopsis text (ဇာတ်ညွှန်း) as its own message.
            # Dashes are split into readable words (Matchbox-The-Movie -> Matchbox The Movie)
            # while Myanmar screenplay text is kept intact.
            db_synopsis = smart_dash(strip_notice(caption_full.strip()))
            db_synopsis = re.sub(r'[ \t]{2,}', ' ', db_synopsis)
            if db_synopsis:
                if len(db_synopsis) > 4000:
                    db_synopsis = db_synopsis[:3997].rstrip() + "..."
                try:
                    for attempt in range(3):
                        try:
                            await context.bot.send_message(
                                chat_id=db_chat,
                                text=db_synopsis,
                            )
                            break
                        except TelegramError as e:
                            if "flood" in str(e).lower() or "retry" in str(e).lower():
                                wait = 10 * (attempt + 1)
                                logger.warning(f"Flood control hit (DB synopsis), waiting {wait}s: {e}")
                                await asyncio.sleep(wait)
                                continue
                            raise
                    db_ok += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    db_fail.append("ဇာတ်ညွှန်း")
                    logger.error(f"Auto database-channel synopsis post failed: {e}")

            # 3) Movie files (bot's own copies).
            for v in videos:
                try:
                    db_caption = clean_caption_db(v.get('original_caption') or f"🎬 {v.get('original_name') or v['file_name']}")
                    db_caption = append_upload_credit(db_caption)
                    if len(db_caption) > 1024:
                        db_caption = db_caption[:1020].rstrip() + "..."
                    sent_db = None
                    for attempt in range(3):
                        try:
                            if v.get('is_video'):
                                sent_db = await context.bot.send_video(
                                    chat_id=db_chat,
                                    video=v['file_id'],
                                    caption=db_caption,
                                    supports_streaming=True,
                                )
                            else:
                                sent_db = await context.bot.send_document(
                                    chat_id=db_chat,
                                    document=v['file_id'],
                                    filename=v.get('original_name') or v['file_name'],
                                    caption=db_caption,
                                )
                            break
                        except TelegramError as e:
                            if "flood" in str(e).lower() or "retry" in str(e).lower():
                                wait = 10 * (attempt + 1)
                                logger.warning(f"Flood control hit, waiting {wait}s: {e}")
                                await asyncio.sleep(wait)
                                continue
                            raise
                    if sent_db is not None:
                        set_file_channel_message(payload, v['file_id'], sent_db.message_id)
                    db_ok += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    db_fail.append(v['file_name'])
                    logger.error(f"Auto database-channel post failed for {v['file_name']}: {e}")

        summary = f"✅ **Post ဖန်တီးပြီးပါပြီ။**\n\n"
        summary += f"🎬 Movie channel {movie_posted}/{len(MOVIE_CHANNEL_IDS)} ခုမှာ တင်ပြီးပါပြီ။\n"
        if DATABASE_CHANNEL_ID:
            summary += f"🗄️ DB channel တွင် ပုံ • ဇာတ်ညွှန်း • movie post {db_ok} ခု အောင်မြင်ပြီး။\n"
        if movie_failed:
            summary += f"⚠️ တင်၍မရတဲ့ channel: {', '.join(str(c) for c in movie_failed)}\n"
        if db_fail:
            summary += f"⚠️ DB တင်၍မရတဲ့အရာများ: {', '.join(db_fail)}\n"
        await update.message.reply_text(summary)
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Post ဖန်တီးရာတွင် အမှား: {str(e)}")
        return ConversationHandler.END

async def cancel_newpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- ===================== Forwarded Movie → Database Channel ===================== ----------
# ============================================================
# 🔁 Migration: source channel post -> new post in AUTO_PUBLISH_CHANNEL
# ============================================================

_MIG_JUNK = (
    re.compile(r"myint\s*myat\s*thu", re.I),
    re.compile(r"credit", re.I),
    re.compile(r"upload(?:ed)?\s*by", re.I),
    re.compile(r"copyright\s*disclaimer|no copyright infringement", re.I),
    re.compile(r"\bjoin\b", re.I),
    re.compile(r"\bfollow\s+(?:us|our|me)\b|\bsubscribe\b", re.I),
    re.compile(r"\bpowered\s*by\b|\bcontact\s+us\b", re.I),
)
# credit fragments that can sit inside a title line: "Savi (2024) (mmsub)Crd"
_MIG_JUNK_STRIP = re.compile(
    r"(?:"
    r"\bcrds?\b|\bcr\b|\bcredits?\b|\bcrd\b|"
    r"\bmmsubs?\b|\bmm\s*subs?\b|\bmyanmar\s*sub(?:title)?s?\b|"
    r"\b(?:sub|subs|subtitle|subtitles)\s*(?:by|:)\b[^|,\n]*|"
    r"upload(?:ed)?\s*by\b[^|,\n]*|"
    r"encoded?\s*by\b[^|,\n]*|"
    r"\bencod(?:e|ed|er|ing)\b|"
    r"\bpowered\s*by\b[^|,\n]*"
    r")", re.I)
_MIG_MENTION = re.compile(r"(?<!\w)@\w+")
_MIG_EMPTY_BRACKETS = re.compile(r"[\(\[\{]\s*[\)\]\}]")
_MIG_LABEL_ONLY = re.compile(
    r"(?i)(?:download|downloads|watch|watch\s*now|link|links|"
    r"movie\s*link|telegram|join|ဒေါင်းလုဒ်|လင့်|လင့်ခ်)\s*[:\-]?\s*")
_MIG_URL = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://)\S*", re.I)
_MIG_SYMBOLS = re.compile(r"^[\W_]+$", re.UNICODE)
_MIG_SPACED = re.compile(r"\b(?:[A-Za-z][._\-]){2,}[A-Za-z]\b")
_MIG_QUALITY = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|480p|360p|4k|web[- ]?dl|webrip|web|bluray|"
    r"brrip|hdtv|hevc|h\.?264|h\.?265|x264|x265|aac|ddp\d?|dts|10bit|hdr|"
    r"nf|amzn|dsnp|hmax|k2|hevcmp4)\b",
    re.I,
)

_mig_albums = {}
_mig_last_photo = {}
_mig_series_photo = {}
_MIG_PUBLISH_LOCK = asyncio.Lock()
# The cloud forwarder sends a channel's posts one at a time (~3.8 s apart), so
# a quiet window shorter than that publishes every episode as its own post.
MIG_QUIET_PERIOD = 9.0
MIG_QUIET_PHOTO = 9.0
MIG_QUIET_SERIES = 12.0
MIG_MAX_WAIT = 180.0


_MIG_EP_MARK = re.compile(
    r"(?:"
    r"\bs\s*\d{1,2}\s*[-_. ]?\s*e\s*p?\s*\d{1,3}\b"
    r"|\bs\s*\d{1,2}\b"
    r"|\bep\s*[\[\(]?\s*\d{1,4}\b"
    r"|\bepisode\s*[\[\(]?\s*\d{1,4}\b"
    r"|\bseason\s*[\[\(]?\s*\d{1,2}\b"
    r"|\be\s*\d{1,3}\b"
    r")", re.I)
_MIG_EP_NOISE = re.compile(
    r"\b(?:end|completed?|final|uncut|extended|full|all|ep|episode)\b", re.I)
# "ep 1 +2+3", "ep 7 to 10", "ep 4+5+6"
_MIG_EP_LIST = re.compile(
    r"(?:\bep?\s*)?\b\d{1,3}\b(?:\s*(?:[+&,]|to|and|-|~)\s*\d{1,3}\b)+", re.I)


def _mig_series_base(title):
    """Strip episode/season markers: 'Spider-Noir (2026) Ep 7' -> 'Spider-Noir (2026)'."""
    if not title:
        return ""
    base = _MIG_EP_LIST.sub(" ", title)
    base = _MIG_EP_MARK.sub(" ", base)
    base = _MIG_EP_NOISE.sub(" ", base)
    base = re.sub(r"[\(\)\[\]\{\}\-_~|]+", " ", base)
    base = re.sub(r"\s{2,}", " ", base).strip(" .,-")
    return base


def _mig_series_key(item):
    """Group key for a series: base title without episode markers.

    Episodes of one series share this key, so they end up on ONE post with ONE
    deep link.  A different title/caption gives a different key -> new post.
    """
    title = _mig_clean_title(_mig_item_title(item))
    base = clean_migrate_caption(_mig_series_base(title))
    base = re.sub(r"\s+\d{1,2}$", "", base)  # trailing part number
    if not base:
        return ""
    if not _MIG_QUALITY.sub("", base).strip():
        return ""
    key = re.sub(r"[^a-z\u1000-\u109f]+", "", base.lower())
    if len(key) < 4 or key.isdigit():
        return ""
    if not re.search(r"[a-z\u1000-\u109f]", key):
        return ""
    return key


# "Season 2", "s02", "#Season[ 3]"  (optional "to 4" / "-4" range tail)
_MIG_SEASON_GET = re.compile(
    r"(?i)\b(?:season|s)\s*[\[\(]?\s*(\d{1,2})"
    r"((?:\s*(?:to|and|&|[-~–])\s*\d{1,2})*)")
# "Ep 7", "EP[1 to 6]", "episode 3", "S01E04"  (optional "+2+3" / "to 6" tail)
_MIG_EP_GET = re.compile(
    r"(?i)(?:\b(?:episode|ep)|(?<=\d)e)\s*[\[\(]?\s*(\d{1,3})"
    r"((?:\s*(?:[+&,]|to|and|[-~–])\s*\d{1,3})*)")
_MIG_NUM = re.compile(r"\d{1,3}")


def _mig_expand_nums(first, tail):
    """'1' + ' to 6' -> [1..6];  '1' + '+2+3' -> [1,2,3]."""
    nums = [int(first)]
    if not tail:
        return nums
    extra = [int(x) for x in _MIG_NUM.findall(tail)]
    if not extra:
        return nums
    lo, hi = nums[0], extra[0]
    if re.search(r"(?i)\b(?:to|and)\b|[-~–]", tail) and 0 < hi - lo <= 40:
        nums.extend(range(lo + 1, hi + 1))
    nums.extend(extra)
    return nums


def _mig_eps_info(items):
    """Season / episode numbers written in the source caption or file name.

    Captions win over file names: a caption saying "EP[4]" is the real episode
    even when the file is named S01E08.
    """
    def scan(texts):
        s, e = set(), set()
        for raw in texts:
            for first, tail in _MIG_SEASON_GET.findall(raw):
                s.update(_mig_expand_nums(first, tail))
            for first, tail in _MIG_EP_GET.findall(raw):
                e.update(_mig_expand_nums(first, tail))
        return s, e

    cs, ce = scan([it.get("caption") or "" for it in items])
    ns, ne = scan([it.get("file_name") or "" for it in items])
    return (cs or ns), (ce or ne)


def _mig_eps_fill(seasons, eps, videos, sdoc=None):
    """Source has no season/episode marks: number the files in order.

    Several files in one batch -> 1..N.  A single new file for an existing
    series -> the next number after the ones already published.  A single file
    with nothing known is left alone (it is probably a movie, not an episode).
    """
    if eps:
        return seasons, eps
    n = len(videos)
    if n < 1:
        return seasons, eps
    base = 0
    if sdoc is not None:
        prev = {int(x) for x in (sdoc.get("eps") or [])}
        base = max(prev) if prev else int(sdoc.get("episodes") or 0)
    if n >= 2 or base:
        eps = set(range(base + 1, base + 1 + n))
    if eps and not seasons:
        seasons = {1}
    return seasons, eps


def _mig_num_label(nums, limit=12):
    nums = sorted({int(n) for n in nums})
    if not nums:
        return ""
    if len(nums) == 1:
        return str(nums[0])
    if nums == list(range(nums[0], nums[-1] + 1)) or len(nums) > limit:
        return f"{nums[0]}-{nums[-1]}"
    return ", ".join(str(n) for n in nums)


def _mig_eps_line(seasons, eps):
    """'📺 Season 2 · Episode 7' / '📺 Episode 1-6' / '' when unknown."""
    parts = []
    s = _mig_num_label(seasons)
    e = _mig_num_label(eps)
    if s:
        parts.append(f"Season {s}")
    if e:
        parts.append(f"Episode {e}")
    return ("📺 " + " · ".join(parts)) if parts else ""


def _mig_build_caption(title, synopsis, seasons=None, eps=None,
                       max_len=1024):
    """Reference layout: title, '📝 ဇာတ်ကားအကြောင်း', Season/Episode, synopsis."""
    cap = title
    line = _mig_eps_line(seasons or (), eps or ())
    if line or synopsis:
        cap += "\n\n📝 ဇာတ်ကားအကြောင်း"
        if line:
            cap += f"\n\n{line}"
        if synopsis:
            cap += f"\n{synopsis}"
    return append_upload_credit(cap, max_len=max_len)


def _mig_item_title(item):
    cap = (item.get("caption") or "").strip()
    if cap:
        return cap.split("\n", 1)[0].strip()
    return _clean_file_title(item.get("file_name") or "")


def _mig_tokens(text):
    t = re.sub(r"[^0-9a-z\u1000-\u109f]+", " ", (text or "").lower())
    return {w for w in t.split() if len(w) > 2}


def _mig_same_movie(photo_title, video_caption, video_name):
    """True when a video belongs to the photo post before it."""
    pt = _mig_tokens(photo_title)
    if not pt:
        return False
    vt = _mig_tokens(f"{video_caption} {_clean_file_title(video_name or '')}")
    if not vt:
        return False
    return len(pt & vt) / len(pt) >= 0.6


def _mig_trim_brackets(ln):
    """Drop unmatched brackets left behind after removing '(mmsub(Crd' etc."""
    for op, cl in (("(", ")"), ("[", "]"), ("{", "}")):
        while ln.count(op) > ln.count(cl) and op in ln:
            i = ln.rfind(op)
            ln = ln[:i] + " " + ln[i + 1:]
        while ln.count(cl) > ln.count(op) and cl in ln:
            i = ln.rfind(cl)
            ln = ln[:i] + " " + ln[i + 1:]
    return ln


def clean_migrate_caption(text):
    """Drop credit / watermark / link / join-prompt lines, keep the rest."""
    if not text:
        return ""
    keep = []
    for ln in text.replace("\r", "").split("\n"):
        ln = _MIG_JUNK_STRIP.sub(" ", ln)
        ln = _MIG_MENTION.sub(" ", ln)
        ln = _MIG_EMPTY_BRACKETS.sub(" ", ln)
        ln = _mig_trim_brackets(ln)
        ln = re.sub(r"\s{2,}", " ", ln).strip(" |-,:;·•")
        if any(p.search(ln) for p in _MIG_JUNK):
            continue
        stripped = _MIG_URL.sub("", ln).strip()
        if not stripped:
            if keep and keep[-1] != "":
                keep.append("")
            continue
        if _MIG_LABEL_ONLY.fullmatch(stripped):
            continue
        if _MIG_SYMBOLS.match(stripped):
            continue
        if stripped.endswith(("👇", "🔰", "👇🔰")):
            continue
        keep.append(stripped)
    out = "\n".join(keep)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return _fix_spaced(out.strip())


def _mig_source_id(message):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin is not None else None
    if chat is not None:
        return chat.id
    fwd = getattr(message, "forward_from_chat", None)
    return fwd.id if fwd is not None else None


def _mig_source_msg_id(message):
    origin = getattr(message, "forward_origin", None)
    mid = getattr(origin, "message_id", None) if origin is not None else None
    if mid:
        return int(mid)
    mid = getattr(message, "forward_from_message_id", None)
    return int(mid) if mid else None


def _strip_ext(name):
    base = name or ""
    for ext in (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp"):
        if base.lower().endswith(ext):
            return base[: -len(ext)].strip()
    return base


def _mig_item(message):
    if message.photo:
        return {
            "kind": "photo",
            "file_id": message.photo[-1].file_id,
            "file_name": "photo.jpg",
            "caption": clean_migrate_caption(message.caption or ""),
        }
    media = message.video or message.document
    if media is not None:
        return {
            "kind": "video",
            "file_id": media.file_id,
            "file_name": getattr(media, "file_name", None) or "movie.mp4",
            "caption": clean_migrate_caption(message.caption or ""),
        }
    return None


def _fix_spaced(text):
    """Merge watermark-style spaced letters: B_r_i_d_g_e_r_t_o_n -> Bridgerton."""
    def repl(m):
        letters = re.sub(r"[^A-Za-z]", "", m.group(0))
        return letters if len(letters) >= 4 else m.group(0)
    return _MIG_SPACED.sub(repl, text)


def _clean_file_title(name):
    base = _strip_ext(name)
    base = base.replace("_", " ").replace(".", " ")
    base = _MIG_QUALITY.sub(" ", base)
    base = re.sub(r"\s{2,}", " ", base).strip(" -")
    return base


_MIG_TITLE_JUNK = re.compile(r"[\s,._-]*\bCM\b[\s,._-]*$")
_MIG_TITLE_FILE = re.compile(
    r"(?i)(?:\.(?:mp4|mkv|avi|mov|wmv|flv|ts|m4v|webm|srt)\s*$"
    r"|\b(?:1080p|720p|480p|2160p|4k|web-?dl|webrip|bluray|brrip|hdtv|"
    r"x264|x265|h\.?264|h\.?265|hevc|aac|ddp\d?|dts|10bit|hdr)\b)")


def _mig_clean_title(t):
    t = (t or "").strip()
    if _MIG_TITLE_FILE.search(t):
        t = _clean_file_title(t)
    t = _MIG_TITLE_JUNK.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,.-_")
    return t


def _mig_good_title(raw):
    """A usable title: not a bare number, not only 'Ep 1' / 'Season 2'."""
    t = _mig_clean_title(_fix_spaced((raw or "").strip()))
    if not t or t.isdigit():
        return ""
    base = _MIG_EP_LIST.sub(" ", t)
    base = _MIG_EP_MARK.sub(" ", base)
    base = _MIG_EP_NOISE.sub(" ", base)
    if len(re.sub(r"[^A-Za-z\u1000-\u109f]+", "", base)) < 3:
        return ""
    return t


def _mig_title(items):
    for want_photo in (True, False):
        for it in items:
            if want_photo and it["kind"] != "photo":
                continue
            cap = it["caption"].strip()
            if not cap:
                continue
            t = _mig_good_title(cap.split("\n", 1)[0])
            if t:
                return t
    for it in items:
        if it["kind"] != "photo":
            t = _mig_good_title(_clean_file_title(it["file_name"]))
            if t:
                return t
    for it in items:
        t = _mig_good_title(_clean_file_title(it["file_name"]))
        if t:
            return t
    return ""


def _mig_syn_ok(out, title):
    """False when a 'synopsis' only repeats the title / episode markers."""
    if not out or _MIG_TITLE_FILE.search(out):
        return False
    plain = _MIG_EP_MARK.sub(" ", out)
    plain = _MIG_EP_LIST.sub(" ", plain)
    plain = _MIG_EP_NOISE.sub(" ", plain)
    plain = re.sub(r"[^0-9a-z\u1000-\u109f]+", " ", plain.lower()).split()
    ttoks = set(re.sub(r"[^0-9a-z\u1000-\u109f]+", " ",
                       (title or "").lower()).split())
    return bool(plain) and not set(plain) <= ttoks


def _mig_synopsis(items, title):
    best = ""
    for it in items:
        cap = it["caption"].strip()
        if len(cap) > len(best):
            best = cap
    if not best:
        return ""
    lines = best.split("\n")
    if lines and lines[0].strip().lower() == title.strip().lower():
        lines = lines[1:]
    out = clean_migrate_caption("\n".join(lines))
    return out if _mig_syn_ok(out, title) else ""


def _mig_keyboard(deep_link):
    buttons = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)]]
    for idx, link in enumerate(OTHER_CHANNELS, 1):
        if idx == 1:
            buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
        elif idx == 2:
            buttons.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
        elif idx == 3:
            buttons.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
        else:
            buttons.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
    if MUSIC_CHANNEL_LINK:
        buttons.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])
    return InlineKeyboardMarkup(buttons)


def _cancel_mig_task(entry):
    task = entry.get("task")
    if task and not task.done():
        task.cancel()


async def _mig_flush_later(context: ContextTypes.DEFAULT_TYPE, key: str):
    await asyncio.sleep(MIG_QUIET_PERIOD)
    entry = _mig_albums.get(key)
    if not entry:
        return
    now = time.monotonic()
    quiet = now - entry["last_update"]
    total = now - entry["started"]
    quiet_limit = entry.get("quiet", MIG_QUIET_PERIOD)
    if total < MIG_MAX_WAIT and quiet < quiet_limit:
        entry["task"] = context.application.create_task(
            _mig_flush_later(context, key))
        return
    _mig_albums.pop(key, None)
    if _mig_last_photo.get(entry.get("src")) == key:
        _mig_last_photo.pop(entry.get("src"), None)
    try:
        await _mig_publish(context, key, entry["items"],
                           entry.get("series"), entry.get("src"))
    except Exception as exc:
        logger.error("migration publish failed (%s): %s", key, exc, exc_info=True)


async def _mig_publish(context: ContextTypes.DEFAULT_TYPE, key: str, items,
                       series_key=None, src=None):
    if not AUTO_PUBLISH_CHANNEL or not items:
        return
    async with _MIG_PUBLISH_LOCK:
        await _mig_publish_locked(context, key, items, series_key, src)


async def _mig_post_album(context: ContextTypes.DEFAULT_TYPE, photos, videos,
                          caption, reply_markup, chat_id=None):
    """Publish the ONE channel post that represents a movie / series.

    Always a single photo (or the first video when there is no poster) with the
    deep-link buttons attached inline - exactly like the reference series post.
    Every episode is delivered by the bot behind that one deep link, never as
    extra visible files in the channel.

    Returns (post_id, button_id).
    """
    chat_id = chat_id or AUTO_PUBLISH_CHANNEL
    if not chat_id or (not photos and not videos):
        return None, None
    try:
        if photos:
            sent = await _with_flood(lambda: context.bot.send_photo(
                chat_id=chat_id, photo=photos[0]["file_id"],
                caption=caption, reply_markup=reply_markup))
        else:
            sent = await _with_flood(lambda: context.bot.send_video(
                chat_id=chat_id, video=videos[0]["file_id"],
                caption=caption, reply_markup=reply_markup,
                supports_streaming=True))
    except Exception as exc:
        logger.error("post single media failed: %s", exc)
        return None, None
    return (sent.message_id if sent else None), None


def _mig_db_caption(title, seasons, ep=None, eps=None):
    """Caption used for a file stored in the database channel."""
    line = _mig_eps_line(seasons or (),
                         eps if eps else ([ep] if ep else ()))
    return (f"{title}\n{line}" if line else title)[:1000]


async def _mig_save_to_db(context: ContextTypes.DEFAULT_TYPE, payload, title,
                          photos, videos, seasons=None, eps=None):
    """Store photo + movie files together (one album) in DATABASE_CHANNEL_ID."""
    if not DATABASE_CHANNEL_ID:
        return False, None
    seasons = sorted({int(x) for x in (seasons or [])})
    ep_list = sorted({int(x) for x in (eps or [])})
    if not ep_list and len(videos) > 1:
        ep_list = list(range(1, len(videos) + 1))
    db_chat = int(DATABASE_CHANNEL_ID.strip())
    ok = False
    first_id = None
    try:
        media = []
        if photos:
            media.append(InputMediaPhoto(
                photos[0]["file_id"],
                caption=_mig_db_caption(title, seasons, eps=ep_list)))
        for i, v in enumerate(videos):
            # Prefer the episode number written in this file's own name/caption
            # (a series often has 720p + 1080p of the same episode).
            v_seasons, v_eps = _mig_eps_info([{
                "caption": v.get("caption") or v.get("file_caption") or "",
                "file_name": v.get("file_name") or "",
            }])
            ep = (min(v_eps) if v_eps
                  else ep_list[i] if i < len(ep_list) else None)
            v_seasons = v_seasons or set(seasons)
            media.append(InputMediaVideo(
                v["file_id"],
                caption=None if media else _mig_db_caption(
                    title, v_seasons, eps=[ep] if ep else []),
                supports_streaming=True,
            ))
        if not media:
            return False, None
        if len(media) == 1:
            if photos:
                sent = await _with_flood(lambda: context.bot.send_photo(
                    chat_id=db_chat, photo=photos[0]["file_id"],
                    caption=_mig_db_caption(title, seasons, eps=ep_list)))
            else:
                sent = await _with_flood(lambda: context.bot.send_video(
                    chat_id=db_chat, video=videos[0]["file_id"],
                    caption=_mig_db_caption(
                        title, seasons, ep_list[0] if ep_list else None),
                    supports_streaming=True))
                set_file_channel_message(payload, videos[0]["file_id"],
                                         sent.message_id)
            return True, sent.message_id

        offset = 0
        for i in range(0, len(media), 10):
            chunk = media[i:i + 10]
            sent = await _with_flood(lambda: context.bot.send_media_group(
                chat_id=db_chat, media=chunk))
            if first_id is None and sent:
                first_id = sent[0].message_id
            for j, msg in enumerate(sent):
                vid_index = offset + j - (1 if photos else 0)
                if 0 <= vid_index < len(videos):
                    set_file_channel_message(
                        payload, videos[vid_index]["file_id"], msg.message_id)
            offset += len(chunk)
            ok = True
            await asyncio.sleep(1.5)
        logger.info("migration DB album saved: %s (%d items)", title, len(media))
    except Exception as exc:
        logger.error("migration DB save failed (%s): %s", title, exc)
    return ok, first_id


_MIG_STOPWORDS = {"the", "and", "of", "in", "a", "to", "for", "part", "season",
                  "episode", "ep", "complete", "completed", "end"}


def _mig_series_tokens(title):
    base = _mig_series_base(_mig_clean_title(title or ""))
    words = re.sub(r"[^0-9a-z\u1000-\u109f]+", " ", base.lower()).split()
    return {w for w in words
            if w not in _MIG_STOPWORDS
            and not re.fullmatch(r"(?:19|20)\d{2}|s\d{1,2}|e\d{1,3}|"
                                 r"s\d{1,2}e\d{1,3}|\d+", w)
            and (len(w) > 1 or w.isalpha())}


def _mig_name_prefix(name, words=3):
    """'Show.Name.S01E02.720p.mkv' -> 'show name' (episode markers dropped).

    Used to keep a series sent as separate files together even when its
    captions do not repeat the show name.
    """
    base = _mig_series_base(_clean_file_title(name or ""))
    toks = re.sub(r"[^0-9a-z\u1000-\u109f]+", " ", base.lower()).split()
    toks = [t for t in toks
            if t not in _MIG_STOPWORDS
            and t not in {"movie", "video", "file", "photo", "download"}
            and not re.fullmatch(r"(?:19|20)\d{2}|s\d{1,2}|e\d{1,3}|"
                                 r"s\d{1,2}e\d{1,3}|\d+", t)]
    return " ".join(toks[:words])


def _mig_series_doc(src, series_key, title=None):
    """Series docs are global (one post per series); keep the old id as fallback.

    The source often names the same series differently from one batch to the
    next ("Gen V (2023)" vs "Gen V Trojan 2"), so fall back to a title-token
    match instead of publishing a second post.
    """
    for _id in (f"s:{series_key}", f"{src}:s:{series_key}"):
        doc = migrated_collection.find_one({"_id": _id})
        if doc:
            return doc
    toks = _mig_series_tokens(title or "")
    if not toks:
        return None
    for d in migrated_collection.find({"series_key": {"$exists": True},
                                       "done": True,
                                       "payload": {"$exists": True}}):
        dt = _mig_series_tokens(d.get("title") or "")
        if not dt:
            continue
        small, big = (toks, dt) if len(toks) <= len(dt) else (dt, toks)
        if len(small) < 2:
            continue
        if len(small & big) / len(small) >= 0.75:
            return d
    return None


async def _mig_merge_into_series(context: ContextTypes.DEFAULT_TYPE, key: str,
                                 src, series_key, sdoc, items):
    """A new episode of an already-published series: add files to that post.

    No new channel post is created, so every episode shares ONE deep link.
    """
    payload = sdoc.get("payload")
    title = sdoc.get("title") or _mig_title(items) or "🎬 ဇာတ်ကားအသစ်"
    videos = [it for it in items if it["kind"] != "photo"]
    photos = [it for it in items if it["kind"] == "photo"]
    if photos:
        _mig_series_photo[(src, series_key)] = photos[0]
    photo = photos[0] if photos else _mig_series_photo.get((src, series_key))

    # keep the season / episode list from the source caption growing
    new_seasons, new_eps = _mig_eps_info(items)
    new_seasons, new_eps = _mig_eps_fill(new_seasons, new_eps, videos, sdoc)
    known_eps = {int(x) for x in (sdoc.get("eps") or [])}
    if not known_eps and int(sdoc.get("episodes") or 0) > 1:
        known_eps = set(range(1, int(sdoc.get("episodes")) + 1))
    seasons = {int(x) for x in (sdoc.get("seasons") or [])} | new_seasons
    eps = known_eps | new_eps
    synopsis = sdoc.get("synopsis") or _mig_synopsis(items, title)

    added = []
    if payload:
        known = {f.get("file_id") for f in (get_file_info(payload) or [])}
        for it in videos:
            if it["file_id"] in known:
                continue
            save_file_info(payload, it["file_id"], it["file_name"], title)
            known.add(it["file_id"])
            added.append(it)

    if added and payload:
        db_ok2, db_post_id = await _mig_save_to_db(
            context, payload, title, [photo] if photo else [], added,
            seasons, sorted(new_eps))
        migrated_collection.update_one(
            {"_id": sdoc["_id"]},
            {"$inc": {"episodes": len(added)},
             "$set": {"db_saved": True,
                      "db_post_id": db_post_id or sdoc.get("db_post_id")}},
        )
    # Keep the single series post: only refresh its Season / Episode line. New
    # files were appended to the SAME payload, so the one deep link in that
    # post now delivers every episode - no extra post, no duplicate.
    post_id = sdoc.get("post_id")
    button_id = sdoc.get("button_id")
    caption = _mig_build_caption(title, synopsis, seasons, eps)
    if post_id and caption != (sdoc.get("caption") or ""):
        try:
            await context.bot.edit_message_caption(
                chat_id=AUTO_PUBLISH_CHANNEL,
                message_id=int(post_id),
                caption=caption,
            )
        except Exception as exc:
            logger.warning("series caption update failed (%s): %s", title, exc)

    migrated_collection.update_one(
        {"_id": sdoc["_id"]},
        {"$set": {"seasons": sorted(seasons), "eps": sorted(eps),
                  "synopsis": synopsis, "caption": caption}},
    )

    logger.info("series episode merged: %s (+%d files, %d items)",
                title, len(added), len(items))
    migrated_collection.update_one(
        {"_id": key},
        {"$set": {
            "done": True,
            "title": title,
            "merged_into": sdoc["_id"],
            "published_at": time.time(),
        }},
        upsert=True,
    )
    await asyncio.sleep(MIGRATE_GAP * max(1, len(items)))


async def _mig_publish_locked(context: ContextTypes.DEFAULT_TYPE, key: str,
                              items, series_key=None, src=None):

    if series_key:
        sdoc = _mig_series_doc(src, series_key, _mig_title(items))
        if sdoc and sdoc.get("done") and sdoc.get("payload"):
            await _mig_merge_into_series(context, key, src, series_key, sdoc,
                                         items)
            return

    title = _mig_title(items) or "🎬 ဇာတ်ကားအသစ်"
    title = clean_migrate_caption(title)[:200].strip() or "🎬 ဇာတ်ကားအသစ်"
    if series_key and _MIG_EP_MARK.search(title):
        base_title = _mig_series_base(title)
        if base_title:
            title = base_title
    synopsis = _mig_synopsis(items, title)

    videos = [it for it in items if it["kind"] != "photo"]
    photos = [it for it in items if it["kind"] == "photo"]
    seasons, eps = _mig_eps_info(items) if series_key else (set(), set())
    if series_key:
        seasons, eps = _mig_eps_fill(seasons, eps, videos)

    payload = generate_payload()
    for it in videos:
        save_file_info(payload, it["file_id"], it["file_name"], title)

    # A post with no video has nothing to deliver: never attach a deep link
    # (it would open the bot and say "invalid link").
    if videos:
        deep_link = create_deep_linked_url(context.bot.username, payload)
        reply_markup = _mig_keyboard(deep_link)
    else:
        reply_markup = None

    caption = _mig_build_caption(title, synopsis, seasons, eps)

    post_id, button_id = await _mig_post_album(
        context, photos, videos, caption, reply_markup)

    if post_id is None:
        logger.info("migration skipped (no media): %s", title)
        migrated_collection.update_one(
            {"_id": key},
            {"$set": {"done": True, "skipped": "no_media", "title": title}},
            upsert=True,
        )
        return

    logger.info(
        "migration published '%s' (%d media) -> %s id=%s",
        title, len(items), AUTO_PUBLISH_CHANNEL, post_id,
    )
    db_ok, db_post_id = await _mig_save_to_db(
        context, payload, title, photos, videos, seasons, eps)
    migrated_collection.update_one(
        {"_id": key},
        {"$set": {
            "done": True,
            "title": title,
            "post_id": post_id,
            "button_id": button_id,
            "payload": payload,
            "db_saved": bool(db_ok),
            "db_post_id": db_post_id,
            "published_at": time.time(),
        }},
        upsert=True,
    )
    if series_key:
        if photos:
            _mig_series_photo[(src, series_key)] = photos[0]
        migrated_collection.update_one(
            {"_id": f"s:{series_key}"},
            {"$set": {
                "done": True,
                "source": src,
                "series_key": series_key,
                "title": title,
                "post_id": post_id,
                "button_id": button_id,
                "payload": payload,
                "db_saved": bool(db_ok),
                "episodes": len(videos),
                "seasons": sorted(seasons),
                "eps": sorted(eps),
                "synopsis": synopsis,
                "caption": caption,
                "db_post_id": db_post_id,
                "published_at": time.time(),
            }},
            upsert=True,
        )
    await asyncio.sleep(MIGRATE_GAP * max(1, len(items)))


async def handle_migrate_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """source channel ကနေ forward လာတဲ့ post ကို ဖမ်းပြီး buffer လုပ်ပါတယ်။"""
    message = update.message
    if message is None or not SOURCE_CHANNEL_IDS or not AUTO_PUBLISH_CHANNEL:
        return

    src = _mig_source_id(message)
    if src is None:
        return
    if src not in SOURCE_CHANNEL_IDS and src not in _mig_extra_sources():
        # Admin forwarded from a channel we do not track yet: accept it and
        # remember the channel so this "send it to the bot" flow always works.
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            return
        _mig_add_extra_source(src)
        logger.info("new source channel accepted from admin forward: %s", src)
    if message.chat_id == AUTO_PUBLISH_CHANNEL:
        return

    item = _mig_item(message)
    if item is None:
        return

    src_msg_id = _mig_source_msg_id(message)
    group_id = getattr(message, "media_group_id", None)
    is_photo = item["kind"] == "photo"
    series_key = _mig_series_key(item)
    series_doc_id = f"{src}:s:{series_key}" if series_key else None

    if group_id:
        key = f"{src}:g{group_id}"
    elif series_doc_id:
        # every episode of one series shares this buffer, so they all land on
        # ONE post / ONE deep link
        key = series_doc_id
    elif is_photo:
        key = f"{src}:p{src_msg_id or message.message_id}"
        _mig_last_photo[src] = key
    else:
        # A photo followed by its own 720p/1080p files (separate messages):
        # attach the video to the photo's post when the title matches.
        key = None
        pk = _mig_last_photo.get(src)
        if pk and pk in _mig_albums:
            pending = _mig_albums[pk]
            photo_item = next(
                (i for i in pending["items"] if i["kind"] == "photo"), None)
            if (photo_item and time.monotonic() - pending["started"] < 90
                    and _mig_same_movie(
                        photo_item["caption"].strip().split("\n", 1)[0],
                        item["caption"], item["file_name"])):
                key = pk
        if key is None:
            # No poster and no usable title (e.g. captions are only "EP 2"):
            # a series sent as several files in a row must still become ONE
            # post, so join the newest pending video batch of this source when
            # the captions / file names name the same show.
            my_toks = _mig_series_tokens(
                _mig_clean_title(f"{item['caption'] or ''} "
                                 f"{item['file_name'] or ''}"))
            my_pref = _mig_name_prefix(item["file_name"])
            now_m = time.monotonic()
            for k, e in list(_mig_albums.items()):
                if e.get("src") != src or not e["items"]:
                    continue
                if now_m - e["last_update"] > 300:
                    continue
                if any(i["kind"] == "photo" for i in e["items"]):
                    continue
                first = e["items"][0]
                t0_toks = _mig_series_tokens(
                    _mig_clean_title(f"{first['caption'] or ''} "
                                     f"{first['file_name'] or ''}"))
                same = bool(my_toks and t0_toks and my_toks == t0_toks)
                if not same and my_pref \
                        and my_pref == _mig_name_prefix(first["file_name"]):
                    same = True
                if same:
                    key = k
                    break
        if key is None:
            key = f"{src}:m{src_msg_id or message.message_id}"

    doc = migrated_collection.find_one({"_id": key})
    if doc and doc.get("done") and key != series_doc_id:
        raise ApplicationHandlerStop

    now = time.monotonic()
    if key not in _mig_albums:
        _mig_albums[key] = {
            "items": [],
            "started": now,
            "last_update": now,
            "chat_id": message.chat_id,
            "src": src,
            "series": series_key,
            "quiet": (MIG_QUIET_SERIES if series_key
                      else MIG_QUIET_PERIOD if group_id else MIG_QUIET_PHOTO),
        }
    entry = _mig_albums[key]
    if series_key and (is_photo or not entry.get("series")):
        entry["series"] = series_key
    entry["items"].append(item)
    entry["last_update"] = now

    _cancel_mig_task(entry)
    entry["task"] = context.application.create_task(
        _mig_flush_later(context, key))

    try:
        migrated_collection.update_one(
            {"_id": key},
            {"$setOnInsert": {
                "source": src,
                "group": group_id,
                "src_msg_id": src_msg_id,
                "ts": time.time(),
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("migration record failed: %s", exc)

    raise ApplicationHandlerStop


async def migrate_posts_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total = migrated_collection.count_documents({})
    done = migrated_collection.count_documents({"done": True})
    skipped = migrated_collection.count_documents({"skipped": {"$exists": True}})
    await update.message.reply_text(
        f"🔁 Migration\n\n"
        f"လက်ခံရရှိ: {total}\n"
        f"တင်ပြီး: {done - skipped}\n"
        f"ကျော်: {skipped}\n"
        f"ကျန် (buffer): {len(_mig_albums)}\n"
        f"Target: {AUTO_PUBLISH_CHANNEL}\n"
        f"Sources: {len(SOURCE_CHANNEL_IDS)}"
    )

async def migrate_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear dedupe records so a channel can be re-migrated from scratch."""
    if not is_admin(update.effective_user.id):
        return
    removed = migrated_collection.delete_many({}).deleted_count
    _mig_albums.clear()
    await update.message.reply_text(f"♻️ Migration state cleared ({removed} records).")


async def dbsave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save a payload's stored files into DATABASE_CHANNEL_ID (MyDatabasechannel)."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not DATABASE_CHANNEL_ID:
        await update.message.reply_text("Usage: /dbsave <payload>")
        return
    payloads = [a.strip() for a in context.args if a.strip()]
    if not payloads:
        await update.message.reply_text("Usage: /dbsave <payload> [payload...]")
        return
    db_chat = int(DATABASE_CHANNEL_ID.strip())
    saved = skipped = failed = 0
    for payload in payloads:
        files = get_file_info(payload)
        if not files:
            failed += 1
            continue
        for f in files:
            if f.get("channel_message_id"):
                skipped += 1
                continue
            cap = (f.get("file_caption") or "")[:1000]
            try:
                try:
                    sent_db = await _with_flood(lambda: context.bot.send_video(
                        chat_id=db_chat,
                        video=f["file_id"],
                        caption=cap,
                        supports_streaming=True,
                    ))
                except TelegramError:
                    sent_db = await _with_flood(lambda: context.bot.send_document(
                        chat_id=db_chat,
                        document=f["file_id"],
                        filename=f.get("file_name") or "movie.mp4",
                        caption=cap,
                    ))
                set_file_channel_message(payload, f["file_id"], sent_db.message_id)
                saved += 1
            except Exception as exc:
                failed += 1
                logger.warning("dbsave failed for %s: %s", f.get("file_name"), exc)
            await asyncio.sleep(1.2)
    await update.message.reply_text(
        f"🗄️ DB save\nsaved: {saved}\nskipped: {skipped}\nfailed: {failed}")


async def dbfix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Copy already-published migration posts into DATABASE_CHANNEL_ID."""
    if not is_admin(update.effective_user.id):
        return
    if not DATABASE_CHANNEL_ID or not AUTO_PUBLISH_CHANNEL:
        await update.message.reply_text("⚠️ DATABASE_CHANNEL_ID / target not set.")
        return
    chat_id = update.effective_chat.id
    db_chat = int(DATABASE_CHANNEL_ID.strip())
    docs = list(migrated_collection.find({
        "post_id": {"$exists": True},
        "db_saved": {"$ne": True},
    }))
    await update.message.reply_text(f"🗄️ Backfill start: {len(docs)} posts")

    async def worker():
        saved = failed = 0
        for d in docs:
            try:
                await context.bot.copy_message(
                    chat_id=db_chat,
                    from_chat_id=AUTO_PUBLISH_CHANNEL,
                    message_id=int(d["post_id"]),
                )
                migrated_collection.update_one(
                    {"_id": d["_id"]}, {"$set": {"db_saved": True}})
                saved += 1
            except Exception as exc:
                failed += 1
                logger.warning("dbfix failed post %s: %s", d.get("post_id"), exc)
            if (saved + failed) % 20 == 0:
                try:
                    await context.bot.send_message(
                        chat_id, f"🗄️ {saved + failed}/{len(docs)} ...")
                except Exception:
                    pass
            await asyncio.sleep(1.6)
        try:
            await context.bot.send_message(
                chat_id, f"✅ DB backfill done\nsaved: {saved}\nfailed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        f"DB channel: {DATABASE_CHANNEL_ID}\n"
        f"Target: {AUTO_PUBLISH_CHANNEL}\n"
        f"Sources: {sorted(SOURCE_CHANNEL_IDS)}")


async def dbrepair_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Make sure every migration post has its movie file in DATABASE_CHANNEL_ID.

    - saves payload files that were never copied
    - a photo-only post borrows the files of the sibling post with the same title
    """
    if not is_admin(update.effective_user.id):
        return
    if not DATABASE_CHANNEL_ID:
        await update.message.reply_text("⚠️ DATABASE_CHANNEL_ID not set.")
        return
    chat_id = update.effective_chat.id
    db_chat = int(DATABASE_CHANNEL_ID.strip())
    docs = list(migrated_collection.find({"post_id": {"$exists": True}}))
    with_files = [d for d in docs if d.get("payload")
                  and get_file_info(d["payload"])]
    await update.message.reply_text(
        f"🗄️ Repair start: {len(docs)} posts, {len(with_files)} with files")

    async def save_file(payload, f):
        cap = (f.get("file_caption") or "")[:1000]
        try:
            try:
                sent_db = await _with_flood(lambda: context.bot.send_video(
                    chat_id=db_chat, video=f["file_id"], caption=cap,
                    supports_streaming=True))
            except TelegramError:
                sent_db = await _with_flood(lambda: context.bot.send_document(
                    chat_id=db_chat, document=f["file_id"],
                    filename=f.get("file_name") or "movie.mp4", caption=cap))
            set_file_channel_message(payload, f["file_id"], sent_db.message_id)
            return sent_db.message_id
        except Exception as exc:
            logger.warning("dbrepair send failed: %s", exc)
            return None

    async def worker():
        added = borrowed = failed = 0
        for n, d in enumerate(docs, 1):
            payload = d.get("payload")
            files = get_file_info(payload) if payload else []
            if not files:
                tt = _mig_tokens(d.get("title") or "")
                if not tt:
                    continue
                donor = None
                for s in with_files:
                    st = _mig_tokens(s.get("title") or "")
                    if st and len(tt & st) / len(tt) >= 0.6:
                        donor = s
                        break
                if not donor or not payload:
                    continue
                for f in get_file_info(donor["payload"]):
                    mid = f.get("channel_message_id")
                    if not mid:
                        mid = await save_file(donor["payload"], f)
                    if mid:
                        save_file_info(payload, f["file_id"], f["file_name"],
                                       f.get("file_caption"))
                        set_file_channel_message(payload, f["file_id"], mid)
                        borrowed += 1
                continue
            for f in files:
                if f.get("channel_message_id"):
                    continue
                if await save_file(payload, f):
                    added += 1
                else:
                    failed += 1
                await asyncio.sleep(1.2)
            if n % 25 == 0:
                try:
                    await context.bot.send_message(
                        chat_id, f"🗄️ {n}/{len(docs)} ...")
                except Exception:
                    pass
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ Repair done\nadded: {added}\nborrowed: {borrowed}\n"
                f"failed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


def _mig_title_series_key(title):
    """Series key from a stored post title (used by /seriesfix)."""
    base = clean_migrate_caption(_mig_series_base(title or ""))
    if not base:
        return ""
    if not _MIG_QUALITY.sub("", base).strip():
        return ""
    key = re.sub(r"[^a-z\u1000-\u109f]+", "", base.lower())
    if len(key) < 4 or key.isdigit():
        return ""
    if not re.search(r"[a-z\u1000-\u109f]", key):
        return ""
    return key


async def setsession_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the userbot session string in Mongo so the cloud forwarder can run."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setsession <session string>")
        return
    try:
        import cloud_forwarder
    except Exception as exc:
        await update.message.reply_text(f"⚠️ forwarder module error: {exc}")
        return
    cloud_forwarder.set_config(session=" ".join(context.args).strip(),
                               enabled=False)
    try:
        await update.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        update.effective_chat.id,
        "✅ userbot session saved. /forwardstart <last_id> နဲ့ စတင်ပါ။")


async def forwardstart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the cloud forwarder (no PC needed)."""
    if not is_admin(update.effective_user.id):
        return
    try:
        import cloud_forwarder
    except Exception as exc:
        await update.message.reply_text(f"⚠️ forwarder module error: {exc}")
        return
    cfg = cloud_forwarder.config()
    if not cfg.get("session"):
        await update.message.reply_text("⚠️ /setsession <string> အရင်လုပ်ပါ။")
        return
    args = context.args or []
    cloud_forwarder.set_config(enabled=True, watch=True)
    # /forwardstart <channel_id> <last_id>  -> reset one channel
    # /forwardstart <last_id>              -> reset the first channel
    if len(args) >= 2 and args[0].lstrip("-").isdigit() and args[1].isdigit():
        ch = str(cloud_forwarder.to_entity_id(args[0]))
        cloud_forwarder.state_col().update_one(
            {"_id": ch},
            {"$set": {"last_id": int(args[1])}, "$unset": {"done": ""}},
            upsert=True)
    elif args and args[0].isdigit():
        first = (cfg.get("sources") or cloud_forwarder.DEFAULT_SOURCES)[0]
        cloud_forwarder.state_col().update_one(
            {"_id": str(cloud_forwarder.to_entity_id(first))},
            {"$set": {"last_id": int(args[0])}, "$unset": {"done": ""}},
            upsert=True)
    started = cloud_forwarder.start_in_background()
    if not started:
        await update.message.reply_text(
            "ℹ️ Forwarder run နေပြီးသား (/forwardstate ကြည့်ပါ)")
        return

    async def _delayed_start():
        # wait so the admin's own client can disconnect first (two IPs with one
        # session string would invalidate the auth key)
        await asyncio.sleep(35)
        cloud_forwarder.start_in_background()

    context.application.create_task(_delayed_start())
    await update.message.reply_text(
        "▶️ Cloud forwarder ၃၅ စက္ကန့်အတွင်း စတင်ပါမယ်။")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live health: is polling alive, when was the last update, forwarder."""
    if not is_admin(update.effective_user.id):
        return
    now = time.time()
    lines = [
        "🩺 BOT STATUS",
        f"uptime: {int((now - BOOT_TS) // 60)} min",
        f"polling: {'ON' if LIVE['polling'] else 'OFF'}  ({LIVE['label']})",
        f"updates: {LIVE['updates']}",
    ]
    if LIVE["last_update"]:
        lines.append(f"last update: {int(now - LIVE['last_update'])}s ago"
                     f" ({LIVE['last_update_kind']})")
    else:
        lines.append("last update: none since restart")
    if LIVE["last_error"]:
        lines.append(f"last error: {LIVE['last_error'][:200]}")
    try:
        import cloud_forwarder as cf
        cfg = cf.config()
        fwd_on = "ON" if cfg.get("enabled") else "OFF"
        thr = "alive" if (cf._task and cf._task.is_alive()) else "dead"
        cli = "yes" if cf._client else "no"
        lines.append(f"forwarder: {fwd_on} thread={thr} client={cli}")
        if getattr(cf, "LAST_LOG", ""):
            lines.append(f"last: {cf.LAST_LOG[:200]}")
    except Exception as exc:
        lines.append(f"forwarder: ERR {exc}")
    await update.message.reply_text("\n".join(lines)[:3900])


async def migstat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Per-source migration counts + highest source message id already used."""
    if not is_admin(update.effective_user.id):
        return
    lines = ["📊 Migrated posts per source"]
    for src in sorted(SOURCE_CHANNEL_IDS):
        n = migrated_collection.count_documents({"source": src})
        top = list(migrated_collection.find(
            {"source": src, "src_msg_id": {"$exists": True}}
        ).sort("src_msg_id", -1).limit(1))
        mx = top[0].get("src_msg_id") if top else 0
        st = db["forward_state"].find_one({"_id": str(src)}) or {}
        lines.append(f"{src}: posts={n} max_src_id={mx} "
                     f"state_last={st.get('last_id')}")
    await update.message.reply_text("\n".join(lines)[:3900])


async def linkcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnose a published post: /linkcheck <part of title>."""
    if not is_admin(update.effective_user.id):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.message.reply_text("Usage: /linkcheck <title စာသား>")
        return
    rx = re.compile(re.escape(q), re.I)
    docs = list(migrated_collection.find({"title": rx}).limit(5))
    if not docs:
        await update.message.reply_text(f"🔍 '{q}' နဲ့ ကိုက်တဲ့ post မတွေ့ပါ")
        return
    lines = [f"🔍 {len(docs)} post တွေ့ပါ"]
    for d in docs:
        payload = d.get("payload") or ""
        files = get_file_info(payload) if payload else []
        have = sum(1 for f in files if f.get("channel_message_id"))
        lines.append(
            f"• id={d.get('post_id')} title={str(d.get('title'))[:40]!r}\n"
            f"  payload={payload or 'NONE'}\n"
            f"  files={len(files)} in_db_channel={have} "
            f"db_saved={d.get('db_saved')} done={d.get('done')}")
    await update.message.reply_text("\n".join(lines)[:3900])


async def fixempty_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove broken deep-link buttons from posts that have no movie file."""
    if not is_admin(update.effective_user.id):
        return
    if not AUTO_PUBLISH_CHANNEL:
        await update.message.reply_text("⚠️ target channel not set.")
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text("🧹 ဖိုင်မရှိတဲ့ post တွေရဲ့ deeplink button ဖျက်နေတယ်...")

    async def worker():
        fixed = failed = 0
        for d in migrated_collection.find({"post_id": {"$exists": True}}):
            payload = d.get("payload")
            files = get_file_info(payload) if payload else []
            if files:
                continue
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=AUTO_PUBLISH_CHANNEL,
                    message_id=int(d["post_id"]),
                    reply_markup=None)
                fixed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.8)
        try:
            await context.bot.send_message(
                chat_id, f"✅ ပြီးပါပြီ\nbutton ဖျက်ပြီး: {fixed}\nfailed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


def _mig_insert_eps_line(caption, line):
    """Put the '📺 Season …' line right under the title of an existing caption."""
    lines = [ln for ln in (caption or "").split("\n")
             if not ln.strip().startswith("📺")]
    if not lines:
        return caption
    lines.insert(1, line)
    text = "\n".join(lines)
    return text if len(text) <= 1024 else text[:1024]


async def _epsfix_posts(bot, admin_chat):
    """Add a '📺 Season … · Episode …' line to published series posts."""
    fixed = skipped = failed = 0
    for d in migrated_collection.find({"series_key": {"$exists": True},
                                       "post_id": {"$exists": True}}):
        if d.get("eps"):
            continue
        n = int(d.get("episodes") or 0)
        if n < 2:
            skipped += 1
            continue
        seasons = sorted({int(x) for x in (d.get("seasons") or [])}) or [1]
        eps = list(range(1, n + 1))
        cap = d.get("caption") or await _mig_read_caption(
            bot, AUTO_PUBLISH_CHANNEL, d["post_id"], admin_chat)
        if not cap:
            skipped += 1
            continue
        new_cap = _mig_insert_eps_line(cap, _mig_eps_line(seasons, eps))
        try:
            await bot.edit_message_caption(
                chat_id=AUTO_PUBLISH_CHANNEL,
                message_id=int(d["post_id"]),
                caption=new_cap,
            )
            migrated_collection.update_one(
                {"_id": d["_id"]},
                {"$set": {"seasons": seasons, "eps": eps,
                          "caption": new_cap}})
            fixed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.8)
    return fixed, skipped, failed


async def epfix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a '📺 Season … · Episode …' line to series posts that lack one."""
    if not is_admin(update.effective_user.id):
        return
    if not AUTO_PUBLISH_CHANNEL:
        await update.message.reply_text("⚠️ target channel not set.")
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "📺 Season/Episode မပါတဲ့ series post တွေ ဖြည့်နေတယ်...")

    async def worker():
        fixed, skipped, failed = await _epsfix_posts(context.bot, chat_id)
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ ပြီးပါပြီ\nဖြည့်ပြီး: {fixed}\nကျော်: {skipped}\nfailed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


async def _mig_read_caption(bot, chat_id, message_id, admin_chat):
    """Read a channel message caption by forwarding a temp copy to the admin."""
    try:
        fwd = await bot.forward_message(chat_id=admin_chat,
                                        from_chat_id=chat_id,
                                        message_id=int(message_id))
    except Exception:
        return None
    cap = fwd.caption or ""
    try:
        await bot.delete_message(chat_id=admin_chat, message_id=fwd.message_id)
    except Exception:
        pass
    return cap


async def _epsfix_db(bot, admin_chat):
    """Put '📺 Season … · Episode …' captions on files already in the DB channel."""
    if not DATABASE_CHANNEL_ID:
        return 0, 0, 0
    db_chat = int(DATABASE_CHANNEL_ID.strip())
    fixed = skipped = failed = 0
    for d in migrated_collection.find({"series_key": {"$exists": True},
                                       "payload": {"$exists": True}}):
        payload = d.get("payload")
        files = get_file_info(payload) if payload else []
        ids = [int(f["channel_message_id"]) for f in files
               if f.get("channel_message_id")]
        if not ids:
            skipped += 1
            continue
        seasons = sorted({int(x) for x in (d.get("seasons") or [])})
        eps = sorted({int(x) for x in (d.get("eps") or [])})
        if not eps and int(d.get("episodes") or 0) > 1:
            eps = list(range(1, int(d.get("episodes")) + 1))
        if not eps:
            skipped += 1
            continue
        if not seasons:
            seasons = [1]
        title = (d.get("title") or "").strip()
        new_cap = _mig_db_caption(title, seasons, eps=eps)
        cand = []
        if d.get("db_post_id"):
            cand.append(int(d["db_post_id"]))
        cand.append(min(ids))
        cand.append(min(ids) - 1)
        target = None
        for mid in dict.fromkeys(cand):
            cap = await _mig_read_caption(bot, db_chat, mid, admin_chat)
            if cap is None:
                continue
            if not title or title[:16].lower() in cap.lower():
                target = mid
                break
        if target is None:
            skipped += 1
            continue
        try:
            await bot.edit_message_caption(
                chat_id=db_chat, message_id=target, caption=new_cap)
            migrated_collection.update_one(
                {"_id": d["_id"]},
                {"$set": {"db_post_id": target, "seasons": seasons,
                          "eps": eps}})
            fixed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.8)
    return fixed, skipped, failed


async def dbeps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Put '📺 Season … · Episode …' captions on files already in the DB channel."""
    if not is_admin(update.effective_user.id):
        return
    if not DATABASE_CHANNEL_ID:
        await update.message.reply_text("⚠️ DATABASE_CHANNEL_ID not set.")
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🗄 DB channel ဖိုင်တွေမှာ Season/Episode ဖြည့်နေတယ်...")

    async def worker():
        fixed, skipped, failed = await _epsfix_db(context.bot, chat_id)
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ ပြီးပါပြီ\nDB caption ဖြည့်ပြီး: {fixed}\n"
                f"ကျော်: {skipped}\nfailed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


def _mig_doc_title(d):
    """Title of a published post; falls back to the movie file name."""
    t = _mig_good_title(d.get("title") or "")
    if t:
        return t
    for f in get_file_info(d.get("payload") or {}) or []:
        t = _mig_good_title(_clean_file_title(f.get("file_name") or ""))
        if t:
            return _mig_series_base(t) or t
    return ""


def _mig_best_title(docs):
    """Pick the cleanest title among the posts of one series."""
    best, best_score = "", -99
    for d in docs:
        raw = (d.get("title") or "").strip()
        t = _mig_doc_title(d)
        if not t:
            continue
        score = 0
        if re.search(r"\b(?:19|20)\d{2}\b", t):
            score += 2
        if _MIG_TITLE_FILE.search(raw):
            score -= 3
        if len(_mig_series_base(t)) < 3:
            score -= 3
        if score > best_score:
            best, best_score = t, score
    return best or "🎬 ဇာတ်ကားအသစ်"


def _mig_group_posts(docs):
    """Cluster published posts whose titles are the same series."""
    groups = []
    for d in sorted(docs, key=lambda x: int(x.get("post_id") or 0)):
        toks = _mig_series_tokens(_mig_doc_title(d))
        if not toks:
            continue
        for g in groups:
            small, big = ((toks, g["toks"]) if len(toks) <= len(g["toks"])
                          else (g["toks"], toks))
            if len(small) >= 2 and len(small & big) / len(small) >= 0.8:
                g["docs"].append(d)
                g["toks"] |= toks
                break
        else:
            groups.append({"toks": set(toks), "docs": [d]})
    return [g["docs"] for g in groups]


_SERIESMERGE_LOCK = asyncio.Lock()


async def _seriesmerge(bot, dry=False):
    """Merge published per-episode posts of one series into ONE post.

    Also repairs junk titles ("Ep 1+2", "Gen V Trojan 2") by reading the movie
    file name stored in the post payload.
    """
    if _SERIESMERGE_LOCK.locked():
        return [], 0, 0, 0, 0
    async with _SERIESMERGE_LOCK:
        return await _seriesmerge_locked(bot, dry)


async def _seriesmerge_locked(bot, dry=False):
    docs = list(migrated_collection.find({
        "post_id": {"$exists": True},
        "merged_into": {"$exists": False},
    }))
    cand = []
    for d in docs:
        t = d.get("title") or ""
        if (d.get("series_key") or int(d.get("episodes") or 0) >= 2
                or _MIG_EP_MARK.search(t) or _MIG_EP_LIST.search(t)
                or not _mig_good_title(t)):
            cand.append(d)
    groups = _mig_group_posts(cand)
    if dry:
        return groups, 0, 0, 0, 0

    merged = deleted = failed = renamed = 0
    for items in groups:
        items.sort(key=lambda d: int(d.get("post_id") or 0))
        keep = items[0]
        keep_id = int(keep["post_id"])
        payload = keep.get("payload")
        if not payload:
            payload = generate_payload()
            try:
                await bot.edit_message_reply_markup(
                    chat_id=AUTO_PUBLISH_CHANNEL, message_id=keep_id,
                    reply_markup=_mig_keyboard(
                        create_deep_linked_url(bot.username, payload)))
            except Exception as exc:
                logger.warning("seriesmerge markup failed %s: %s", keep_id, exc)
            migrated_collection.update_one(
                {"_id": keep["_id"]}, {"$set": {"payload": payload}})

        seasons, eps = set(), set()
        for d in items:
            seasons.update(int(x) for x in (d.get("seasons") or []))
            eps.update(int(x) for x in (d.get("eps") or []))
        total = sum(int(d.get("episodes") or 0) for d in items)
        if not eps and total > 1:
            eps = set(range(1, total + 1))
        if eps and not seasons:
            seasons = {1}
        title = _mig_best_title(items)
        if _MIG_EP_MARK.search(title):
            title = _mig_series_base(title) or title
        synopsis = ""
        for d in items:
            s = (d.get("synopsis") or "").strip()
            if s and _mig_syn_ok(s, title):
                synopsis = s
                break

        if len(items) == 1:
            # a single post: only fix a junk title, never touch its files
            old = _mig_clean_title(keep.get("title") or "")
            if not title or title == old:
                continue
            caption = _mig_build_caption(title, synopsis, seasons, eps)
            try:
                await bot.edit_message_caption(
                    chat_id=AUTO_PUBLISH_CHANNEL, message_id=keep_id,
                    caption=caption)
                renamed += 1
            except Exception as exc:
                logger.warning("seriesmerge rename failed %s: %s", keep_id, exc)
                failed += 1
                continue
            migrated_collection.update_one(
                {"_id": keep["_id"]},
                {"$set": {"title": title, "caption": caption,
                          "seasons": sorted(seasons), "eps": sorted(eps)}})
            await asyncio.sleep(1.0)
            continue

        for d in items[1:]:
            pid = int(d["post_id"])
            donor = d.get("payload")
            if donor:
                for f in get_file_info(donor) or []:
                    save_file_info(payload, f["file_id"], f["file_name"],
                                   f.get("file_caption"))
                    if f.get("channel_message_id"):
                        set_file_channel_message(payload, f["file_id"],
                                                 f["channel_message_id"])
            try:
                await bot.delete_message(AUTO_PUBLISH_CHANNEL, pid)
                deleted += 1
            except Exception as exc:
                logger.warning("seriesmerge delete failed %s: %s", pid, exc)
                failed += 1
                continue
            migrated_collection.update_one(
                {"_id": d["_id"]},
                {"$set": {"merged_into": f"post:{keep_id}"}})
            merged += 1
            await asyncio.sleep(1.2)

        caption = _mig_build_caption(title, synopsis, seasons, eps)
        try:
            await bot.edit_message_caption(
                chat_id=AUTO_PUBLISH_CHANNEL, message_id=keep_id,
                caption=caption)
        except Exception as exc:
            logger.warning("seriesmerge caption failed %s: %s", keep_id, exc)
            failed += 1
        migrated_collection.update_one(
            {"_id": keep["_id"]},
            {"$set": {"title": title, "caption": caption,
                      "seasons": sorted(seasons), "eps": sorted(eps),
                      "episodes": max(total,
                                      len(get_file_info(payload) or [])),
                      "payload": payload, "series_fixed": True}})
        await asyncio.sleep(1.2)
    return groups, merged, deleted, failed, renamed


async def _epsfix_once(bot, admin_chat):
    """One-time backfill: merge split series, then Season/Episode captions."""
    await asyncio.sleep(60)
    try:
        g, merged, deleted, failed, renamed = await _seriesmerge(bot)
        p = await _epsfix_posts(bot, admin_chat)
        d = await _epsfix_db(bot, admin_chat)
        await bot.send_message(
            admin_chat,
            f"🧩 series merge: {len(g)} group / merged {merged} / deleted {deleted}"
            f" / renamed {renamed} / fail {failed}\n"
            f"📺 Season/Episode\n"
            f"post: {p[0]} (ကျော် {p[1]}, fail {p[2]})\n"
            f"DB: {d[0]} (ကျော် {d[1]}, fail {d[2]})")
    except Exception as exc:
        logger.warning("epsfix once failed: %s", exc)


async def _seriesmerge_loop(bot):
    """Keep merging while the cloud forwarder is still filling the channel."""
    await asyncio.sleep(300)
    was = False
    while True:
        try:
            cfg = db["config"].find_one({"_id": "forwarder"}) or {}
            on = bool(cfg.get("enabled"))
            if on or was:
                g, m, d, f, r = await _seriesmerge(bot)
                if m or d or r:
                    logger.info("seriesmerge loop: groups=%d merged=%d "
                                "deleted=%d renamed=%d", len(g), m, d, r)
            was = on
        except Exception as exc:
            logger.warning("seriesmerge loop failed: %s", exc)
        await asyncio.sleep(600)


async def _epsfix_post_init(application):
    """Run the one-time repair after a deploy."""
    try:
        # Tell the admin the bot is alive again (at most once every 10 min so a
        # crash/restart loop cannot spam).
        try:
            last = db["config"].find_one({"_id": "boot_notice"}) or {}
            if time.time() - float(last.get("ts") or 0) > 600:
                db["config"].update_one({"_id": "boot_notice"},
                                        {"$set": {"ts": time.time()}},
                                        upsert=True)
                admin = int((db["config"].find_one(
                    {"_id": "forwarder"}) or {}).get("admin_chat") or 1147922719)
                await application.bot.send_message(
                    admin,
                    "♻️ Bot ပြန်တက်ပြီးပါပြီ။ /status နဲ့ စစ်လို့ရပါတယ်။")
        except Exception as exc:
            logger.warning("boot notice failed: %s", exc)
        application.create_task(_seriesmerge_loop(application.bot))
        if db["config"].find_one({"_id": "backfill2_once"}):
            return
        db["config"].update_one({"_id": "backfill2_once"},
                                {"$set": {"ts": time.time()}}, upsert=True)
        cfg = db["config"].find_one({"_id": "forwarder"}) or {}
        admin = int(cfg.get("admin_chat") or 1147922719)
        application.create_task(_epsfix_once(application.bot, admin))
    except Exception as exc:
        logger.warning("epsfix post_init failed: %s", exc)


async def forwardstop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        import cloud_forwarder
    except Exception as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return
    cloud_forwarder.set_config(enabled=False, watch=False)
    cloud_forwarder.stop()
    await update.message.reply_text("⏹️ Cloud forwarder ရပ်နေပါပြီ။")


async def forwardstate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        import cloud_forwarder
    except Exception as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return
    cfg = cloud_forwarder.config()
    rows = list(cloud_forwarder.state_col().find({}))
    lines = [
        "🚚 Cloud forwarder",
        f"enabled: {bool(cfg.get('enabled'))}",
        f"watch (24/7): {bool(cfg.get('watch', True))}",
        f"last scan: {int((time.time() - float(cfg.get('last_scan') or 0)) // 60) if cfg.get('last_scan') else '-'} min ago",
        f"session: {'set' if cfg.get('session') else 'missing'}",
    ]
    for r in rows:
        lines.append(
            f"{r.get('title') or r.get('_id')}: last={r.get('last_id')} "
            f"done={r.get('done', 0)} failed={r.get('failed', 0)}")
    await update.message.reply_text("\n".join(lines)[:3900])


async def seriesfix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Consolidate per-episode series posts into ONE post with ONE deep link.

    Keeps the earliest post of each series, copies every other episode's files
    into its payload, then deletes the extra posts.
    Usage: /seriesfix dry   (preview only)   |   /seriesfix
    """
    if not is_admin(update.effective_user.id):
        return
    if not AUTO_PUBLISH_CHANNEL:
        await update.message.reply_text("⚠️ target channel not set.")
        return
    dry = any(a.lower() == "dry" for a in (context.args or []))
    chat_id = update.effective_chat.id

    docs = list(migrated_collection.find({
        "post_id": {"$exists": True},
        "merged_into": {"$exists": False},
        "series_fixed": {"$ne": True},
    }))
    groups = {}
    for d in docs:
        title = d.get("title") or ""
        if not _MIG_EP_MARK.search(title):
            continue
        k = _mig_title_series_key(title)
        if not k:
            continue
        groups.setdefault(k, []).append(d)
    # merge near-identical keys (source typos: "disenchanment" vs "disenchantment")
    import difflib
    keys = sorted(groups)
    merged_groups = {}
    taken = set()
    for k in keys:
        if k in taken:
            continue
        cluster = [k]
        taken.add(k)
        for k2 in keys:
            if k2 in taken:
                continue
            if difflib.SequenceMatcher(None, k, k2).ratio() >= 0.86:
                cluster.append(k2)
                taken.add(k2)
        items = []
        for c in cluster:
            items.extend(groups[c])
        merged_groups[k] = items
    groups = {k: v for k, v in merged_groups.items() if len(v) > 1}

    if dry:
        lines = [f"🧩 Series groups: {len(groups)}"]
        for k, items in list(groups.items())[:25]:
            items.sort(key=lambda d: int(d.get("post_id") or 0))
            ids = ",".join(str(d["post_id"]) for d in items)
            lines.append(f"{items[0].get('title')} -> keep {items[0]['post_id']} "
                         f"({len(items)}): {ids}")
        await update.message.reply_text("\n".join(lines)[:3900])
        return

    await update.message.reply_text(
        f"🧩 Series fix start: {len(groups)} series, "
        f"{sum(len(v) for v in groups.values())} posts")

    async def worker():
        merged = deleted = failed = 0
        for k, items in groups.items():
            items.sort(key=lambda d: int(d.get("post_id") or 0))
            keep = items[0]
            keep_id = int(keep["post_id"])
            payload = keep.get("payload")
            if not payload:
                payload = generate_payload()
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=AUTO_PUBLISH_CHANNEL,
                        message_id=keep_id,
                        reply_markup=_mig_keyboard(
                            create_deep_linked_url(context.bot.username,
                                                   payload)),
                    )
                except Exception as exc:
                    logger.warning("seriesfix markup failed %s: %s", keep_id, exc)
                migrated_collection.update_one(
                    {"_id": keep["_id"]}, {"$set": {"payload": payload}})
            base_title = (clean_migrate_caption(
                _mig_series_base(keep.get("title") or ""))
                or keep.get("title") or "")
            for d in items[1:]:
                pid = int(d["post_id"])
                donor = d.get("payload")
                if donor:
                    for f in get_file_info(donor) or []:
                        save_file_info(payload, f["file_id"], f["file_name"],
                                       f.get("file_caption"))
                        if f.get("channel_message_id"):
                            set_file_channel_message(
                                payload, f["file_id"],
                                f["channel_message_id"])
                try:
                    await context.bot.delete_message(AUTO_PUBLISH_CHANNEL, pid)
                    deleted += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("seriesfix delete failed %s: %s", pid, exc)
                    continue
                migrated_collection.update_one(
                    {"_id": d["_id"]},
                    {"$set": {"merged_into": f"post:{keep_id}"}})
                merged += 1
                await asyncio.sleep(1.2)
            migrated_collection.update_one(
                {"_id": keep["_id"]},
                {"$set": {"title": base_title, "series_fixed": True,
                          "episodes": len(items), "db_saved": True}})
            src = keep.get("source")
            if src:
                migrated_collection.update_one(
                    {"_id": f"s:{k}"},
                    {"$set": {"done": True, "source": src, "series_key": k,
                              "title": base_title, "post_id": keep_id,
                              "payload": payload, "db_saved": True,
                              "episodes": len(items),
                              "published_at": time.time()}},
                    upsert=True)
            await asyncio.sleep(1.5)
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ Series fix done\nmerged: {merged}\ndeleted: {deleted}\n"
                f"failed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


async def seriesmerge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Merge split series posts by title similarity.  /seriesmerge dry | /seriesmerge"""
    if not is_admin(update.effective_user.id):
        return
    if not AUTO_PUBLISH_CHANNEL:
        await update.message.reply_text("⚠️ target channel not set.")
        return
    dry = any(a.lower() == "dry" for a in (context.args or []))
    chat_id = update.effective_chat.id
    if dry:
        groups, *_ = await _seriesmerge(context.bot, dry=True)
        lines = [f"🧩 {len(groups)} series"]
        for items in groups[:20]:
            lines.append(" + ".join(
                f"{_mig_doc_title(d)}({d.get('post_id')})" for d in items)[:180])
        await update.message.reply_text("\n".join(lines)[:3900])
        return
    await update.message.reply_text("🧩 Series post တွေ ပေါင်းနေတယ်...")

    async def worker():
        groups, merged, deleted, failed, renamed = await _seriesmerge(context.bot)
        try:
            await context.bot.send_message(
                chat_id,
                f"✅ Series merge ပြီးပါပြီ\nseries: {len(groups)}\n"
                f"merged: {merged}\ndeleted: {deleted}\n"
                f"renamed: {renamed}\nfailed: {failed}")
        except Exception:
            pass

    context.application.create_task(worker())


async def dbcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report which migration posts are missing their movie file in the DB channel."""
    if not is_admin(update.effective_user.id):
        return
    no_payload = []
    no_files = []
    not_saved = []
    ok = 0
    for d in migrated_collection.find({"post_id": {"$exists": True}}):
        payload = d.get("payload")
        if not payload:
            no_payload.append(d.get("post_id"))
            continue
        files = get_file_info(payload)
        if not files:
            no_files.append(d.get("post_id"))
            continue
        if any(not f.get("channel_message_id") for f in files):
            not_saved.append(d.get("post_id"))
            continue
        ok += 1
    lines = [
        "🗄️ DB check",
        f"complete: {ok}",
        f"no payload (old): {len(no_payload)}",
        f"payload but no file: {len(no_files)}",
        f"file not saved to DB: {len(not_saved)}",
    ]
    for label, ids in (("no payload", no_payload), ("no file", no_files),
                       ("not saved", not_saved)):
        if ids:
            lines.append(f"{label} ids: " + ",".join(str(i) for i in ids[:60]))
    text = "\n".join(lines)
    await update.message.reply_text(text[:3900])


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anyone forwards a movie → bot stores it in the database channel.

    The bot posts its OWN copy (by file_id, not a Telegram forward), so the
    copy survives even if the original source channel is deleted. The ORIGINAL
    caption (as typed by the uploader) is preserved — NO translation is applied,
    so no error captions like "AUTO IS AN INVALID SOURCE LANGUAGE".
    """
    msg = update.message
    media = msg.video or msg.document
    if not media:
        return

    if not DATABASE_CHANNEL_ID:
        if is_admin(update.effective_user.id):
            await msg.reply_text("⚠️ DATABASE_CHANNEL_ID မသတ်မှတ်ရသေးပါ။")
        return

    db_chat = int(DATABASE_CHANNEL_ID.strip())
    file_name = get_original_filename(media)
    raw_caption = msg.caption or ""
    extracted_raw = extract_movie_name(raw_caption)
    if extracted_raw and re.search(r"[\u1000-\u109F]", extracted_raw):
        extracted_raw = ""
    original_caption = clean_caption(extracted_raw or raw_caption)
    original_caption = append_upload_credit(original_caption)
    if len(original_caption) > 1024:
        original_caption = original_caption[:1020].rstrip() + "..."

    for attempt in range(3):
        try:
            if msg.video:
                await context.bot.send_video(
                    chat_id=db_chat,
                    video=media.file_id,
                    caption=original_caption or f"🎬 {file_name}",
                    supports_streaming=True,
                )
            else:
                await context.bot.send_document(
                    chat_id=db_chat,
                    document=media.file_id,
                    filename=file_name,
                    caption=original_caption or f"🎬 {file_name}",
                )
            break
        except TelegramError as e:
            if "flood" in str(e).lower() or "retry" in str(e).lower():
                wait = 10 * (attempt + 1)
                logger.warning(f"Flood control hit, waiting {wait}s: {e}")
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as e:
            logger.error(f"Forwarded DB post failed: {e}")
            if is_admin(update.effective_user.id):
                await msg.reply_text(f"❌ Database channel မှာ တင်၍မရပါ: {str(e)}")
            return
    await asyncio.sleep(1)
    if is_admin(update.effective_user.id):
        await msg.reply_text(f"✅ Database channel မှာ တင်ပြီးပါပြီ။\n🔖 {file_name}")

def get_original_filename(media, fallback="movie.mp4"):
    """Return the real filename as saved on the uploader's computer."""
    name = getattr(media, 'file_name', None)
    if name:
        name = clean_file_name(re.sub(r'\s+', ' ', name).strip())
        if name:
            return preserve_extension(name)
    return fallback

def strip_notice(text):
    """Remove auto-delete / 'forward to Saved Messages' promos and ad (ADS) blocks from captions."""
    if not text:
        return text
    for pat in (
        re.compile(r'\*\*\s*.*?moviesandseriesforallwzn.*?\*\*', re.I | re.S),
        re.compile(r'\*\*\s*.*?(?:Forward\s+Saved\s+Messages|Forward\s*လုပ်ဖို့|Save\s*Message\s*ထဲ).*?\*\*', re.I | re.S),
    ):
        text = pat.sub('', text)
    text = re.sub(
        r'[（(]\s*[^()（）]*(?:Auto\s*Delete|Save\s*Messages?|Saved\s*Messages?|Forward)[^()（）]*[)）]',
        '', text, flags=re.I | re.S)
    text = re.sub(r'https?://t\.me/[A-Za-z0-9_]+', '', text, flags=re.I)
    text = re.sub(r'\b[uU]pload(?:ed|er)?[:\s.\-]*[bB][yY][:\s.\-]*\S+', '', text, flags=re.I)
    # Remove parenthesized ad blocks: (==== ADS ==== ... ), (===== AD ===== ... )
    text = re.sub(
        r'[（(]\s*=+\s*(?:ads?|ad)\s*=+[^()（）]*[)）]',
        ' ', text, flags=re.I | re.S)
    # Remove unparenthesized ad blocks: ===== ADS =====  ...  (until a blank line or next caption)
    text = re.sub(
        r'=+\s*(?:ads?|ad)\s*=+[^\n]*',
        ' ', text, flags=re.I)
    text = re.sub(
        r'[（(]\s*=+\s*(?:ads?|ad)\s*=+.*?[)）]',
        ' ', text, flags=re.I | re.S)
    AD_LINE_KEYWORDS = (
        'win rate', 'ace play', 'aceplay', 'free 9000',
        'telegram', 'viber', 'casino', 'slot', 'betting',
        'ဂိမ်း', 'ဘောနပ်', 'အကောင့်', 'ကစား',
    )
    keep = []
    for ln in text.split('\n'):
        low = ln.lower()
        if ('forward' in low and ('save' in low or 'saved' in low)) \
           or 'moviesandseriesforallwzn' in low \
           or ('auto delete' in low and ('save' in low or 'forward' in low)) \
           or any(k in low for k in AD_LINE_KEYWORDS):
            continue
        keep.append(ln)
    text = '\n'.join(keep)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def smart_dash(text):
    """Merge copyright-style single letters (L-i-b-a-n-g -> Libang) but keep
    real hyphenated words separated (WEB-DL -> WEB DL, S01-EP01 -> S01 EP01).
    Season/episode markers (S03-E12) are normalised to S03E12."""
    text = re.sub(r'\bS(\d{1,3})\s*[-–]\s*E(\d{1,4})\b',
                  lambda m: f"S{m.group(1)}E{m.group(2)}", text, flags=re.I)
    text = re.sub(r'\b([A-Za-z0-9](?:-[A-Za-z0-9])+)\b',
                  lambda m: m.group(1).replace('-', ''), text)
    return text.replace('-', ' ')


def clean_caption(text):
    """Remove dashes, special chars and emojis from captions: 'A-B_C.D/E😀' -> 'A B C D E'."""
    if not text:
        return text
    text = strip_notice(text)
    text = smart_dash(text)
    text = to_ascii_digits(text)
    text = re.sub(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+', ' ', text)
    text = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+', ' ', text)
    text = re.sub(r'[_=+/.\-*#|\\\'\"!?@,\[\]\(\)\x27]', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def append_upload_credit(text, max_len=1024):
    """Append the channel upload credit (e.g. '(Upload By WZN Cinema Hub Movies)') to a caption."""
    if not text:
        return text
    suffix = f"\n{UPLOAD_CREDIT}"
    if len(text) + len(suffix) <= max_len:
        return text + suffix
    return text[:max_len - len(suffix)].rstrip() + suffix


def clean_caption_db(text):
    """Strip dashes, special chars and Myanmar (Burmese) text from captions.

    'L-i-b-a-n-g (2026) - မြန်မာစာ' -> 'Libang 2026'
    Copyright-style single letters merge (L-i-b-a-n-g -> Libang), real hyphenated
    words keep a space (WEB-DL -> WEB DL), other special chars become spaces, and
    Myanmar text is dropped.
    """
    if not text:
        return text
    text = strip_notice(text)
    text = smart_dash(text)
    text = to_ascii_digits(text)
    text = re.sub(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+', ' ', text)
    text = re.sub(r'[^A-Za-z0-9\s]', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

# ---------- Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_files = file_store_collection.count_documents({})
    await update.message.reply_text(
        f"📊 **စာရင်းအင်း**\n\n"
        f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
        f"🎬 ဖိုင်အရေအတွက်: {total_files}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <message>")
        return
    users = get_all_users()
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပြန်လွှင့်ခြင်း ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Schedule ပြုလုပ်ရန် (လုပ်ဆောင်ဆဲ)")

async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📋 Schedule စာရင်း (လုပ်ဆောင်ဆဲ)")

async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("❌ Schedule ဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🗑️ /delete <file_id> ဖြင့် ဖိုင်ဖျက်ပါ။")

async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚠️ ဖိုင်အားလုံးဖျက်ရန် (အတည်ပြုရန် /done ရိုက်ပါ)")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id):
        return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await show_menu(update, context)

# ---------- Background Migration (Render) ----------
from migrate_db import run_migration as _run_db_migration, Progress as _DbProgress

MIGRATION_STATE = {"type": None, "running": False, "started": ""}

def _db_migration_worker(chat_id, dry, limit, token=None):
    from telegram import Bot as _Bot
    bot = _Bot(token or TOKEN)
    MIGRATION_STATE["running"] = True
    MIGRATION_STATE["type"] = "db"
    MIGRATION_STATE["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def send(text):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"migration notify failed: {e}")

    async def finish_success(res):
        await send(f"✅ DB migration DONE: ok={res['ok']} fail={res['fail']} "
                   f"(groups={res['groups']}, done={res['done']})")

    async def finish_error(e):
        await send(f"❌ migratedb failed: {type(e).__name__}: {e}")

    async def main():
        try:
            res = await _run_db_migration(dry=dry, limit=limit)
        except Exception as e:
            logger.exception("migratedb thread failed")
            await finish_error(e)
            return
        finally:
            MIGRATION_STATE["type"] = None
            MIGRATION_STATE["running"] = False
        await finish_success(res)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()

async def migratedb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    if MIGRATION_STATE["running"]:
        await update.message.reply_text(
            f"⚠️ Migration လုပ်နေဆဲပါ ({MIGRATION_STATE['type']}), "
            f"started {MIGRATION_STATE['started']}")
        return
    args = context.args or []
    dry = "dry" in args or os.environ.get("DRY_RUN", "0") == "1"
    limit = 0
    for a in args:
        if a.startswith("limit=") and a.split("=")[1].isdigit():
            limit = int(a.split("=")[1])
    mode = "DRY-RUN" if dry else f"real run (limit={limit or 'all'})"
    threading.Thread(target=_db_migration_worker,
                     args=(update.effective_chat.id, dry, limit, context.bot.token),
                     daemon=True).start()
    await update.message.reply_text(
        f"🚀 migratedb စတင်နေပါပြီ ({mode})။\n"
        f"ဒီ run က Render server ပေါ်မှာ background ဖြစ်လို့ "
        f"မင်းရဲ့ computer ပိတ်ထားရင်လဲ ဆက်လုပ်နေမှာပါ။\n"
        f"ပြီးတဲ့အခါ ဒီ chat ထဲ status ပြန်ပို့ပါမယ်။")

async def migrate_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    if MIGRATION_STATE["running"]:
        await update.message.reply_text(
            f"🔄 Migration လုပ်နေဆဲ ({MIGRATION_STATE['type']}), "
            f"started {MIGRATION_STATE['started']}")
        return
    prog = _DbProgress()
    await update.message.reply_text(
        f"⏹️ Migration မလုပ်ဖြစ်သေးပါ။\n"
        f"Done groups (file+mongo progress): {len(prog.done)}")

# ---------- Relink old DB-channel files (survive bot token swap) ----------
# Telegram file_ids are bot-specific. Files uploaded before message_ids were
# stored cannot be sent by a NEW bot. This scans the database channel and links
# each existing file_store entry to the channel message that holds its media, so
# copy_message can deliver it from any bot.
RELINK_STATE = {"running": False, "started": ""}
_relink_col = db["relink_progress"]


def _norm_match(text):
    """Normalize a filename/caption so DB-channel text and file_store entries
    compare consistently (credit, URLs, digits and punctuation removed)."""
    if not text:
        return ""
    t = strip_notice(text)
    if UPLOAD_CREDIT:
        t = t.replace(UPLOAD_CREDIT, " ")
    t = re.sub(r'\(\s*upload(?:ed)?\s*(?:by)?[^)]*\)', ' ', t, flags=re.I)
    t = re.sub(r'https?://\S+', ' ', t, flags=re.I)
    t = to_ascii_digits(t).lower()
    t = re.sub(r'[^0-9a-z\u1000-\u109f]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def _take_match(cands, key, used):
    if not key:
        return None
    lst = cands.get(key)
    while lst:
        item = lst.pop(0)
        if item not in used:
            used.add(item)
            return item
    return None


async def _with_flood(fn, attempts=30):
    for attempt in range(attempts):
        try:
            return await fn()
        except TelegramError as e:
            low = str(e).lower()
            if "flood" in low or "retry" in low:
                wait = getattr(e, "retry_after", None) or 10 * (attempt + 1)
                logger.warning("relink flood, waiting %ss: %s", wait, e)
                await asyncio.sleep(wait)
            else:
                raise


def _relink_worker(chat_id, dry, start_id=None, end_id=None, limit=0, token=None):
    from telegram import Bot as _Bot
    bot = _Bot(token or TOKEN)
    RELINK_STATE["running"] = True
    RELINK_STATE["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def send(text):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("relink notify failed: %s", e)

    async def main():
        if not DATABASE_CHANNEL_ID:
            await send("❌ DATABASE_CHANNEL_ID မသတ်မှတ်ထားပါ။")
            return
        db_chat = int(DATABASE_CHANNEL_ID.strip())
        scratch = chat_id

        # Index every still-unlinked file by its normalized caption/filename.
        cands = {}
        used = set()
        total_files = linked_already = 0
        for doc in file_store_collection.find({}):
            payload = doc.get("payload")
            for f in doc.get("files", []):
                total_files += 1
                if f.get("channel_message_id"):
                    linked_already += 1
                    continue
                item = (payload, f.get("file_id"))
                name = f.get("file_name") or ""
                stem = os.path.splitext(name)[0] if name else ""
                for raw in (name, stem, f.get("file_caption")):
                    k = _norm_match(raw)
                    if k:
                        cands.setdefault(k, []).append(item)

        unlinked = total_files - linked_already
        if not unlinked:
            await send("✅ ဖိုင်အားလုံးမှာ message_id ရှိပြီးသားပါ။ relink မလိုပါ။")
            return

        latest = end_id
        if not latest:
            probe = await bot.send_message(chat_id=db_chat, text="🔗 relink probe")
            latest = probe.message_id
            try:
                await bot.delete_message(chat_id=db_chat, message_id=latest)
            except Exception:
                pass

        start = start_id
        if not start:
            saved = _relink_col.find_one({"_id": "cursor"})
            start = int(saved["next_id"]) if saved and saved.get("next_id") else 1
        start = max(1, int(start))
        limit = int(limit or 0)

        await send(
            f"🔗 Relink စတင်ပါပြီ ({'DRY-RUN' if dry else 'RUN'})\n"
            f"IDs {start}..{latest} | unlinked={unlinked} "
            f"(already linked={linked_already})")

        matched = scanned = 0
        last_report = 0
        cursor = start
        for mid in range(start, latest + 1):
            if limit and scanned >= limit:
                break
            scanned += 1
            cursor = mid
            try:
                m = await _with_flood(lambda: bot.forward_message(
                    chat_id=scratch, from_chat_id=db_chat, message_id=mid,
                    disable_notification=True))
            except TelegramError:
                continue
            if m is None:
                continue
            try:
                await bot.delete_message(chat_id=scratch, message_id=m.message_id)
            except TelegramError:
                pass

            media = m.document or m.video or m.audio
            if not media:
                continue
            name = getattr(media, "file_name", None) or ""
            stem = os.path.splitext(name)[0] if name else ""
            item = None
            for raw in (name, stem, m.caption or ""):
                item = _take_match(cands, _norm_match(raw), used)
                if item:
                    break
            if not item:
                continue
            matched += 1
            if not dry:
                set_file_channel_message(item[0], item[1], m.message_id)
            if not dry and mid % 100 == 0:
                _relink_col.update_one(
                    {"_id": "cursor"}, {"$set": {"next_id": mid}}, upsert=True)
            if scanned - last_report >= 200:
                last_report = scanned
                await send(
                    f"⏳ relink: scanned={scanned}, matched={matched}, id={mid}")

        if not dry:
            _relink_col.update_one(
                {"_id": "cursor"}, {"$set": {"next_id": cursor}}, upsert=True)
        await send(
            f"✅ Relink {'DRY-RUN ' if dry else ''}ပြီးပါပြီ။\n"
            f"scanned={scanned}, matched={matched}, "
            f"already_linked={linked_already}, unlinked_before={unlinked}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except Exception as e:
        logger.exception("relink failed")
        try:
            loop.run_until_complete(
                send(f"❌ relink failed: {type(e).__name__}: {e}"))
        except Exception:
            pass
    finally:
        RELINK_STATE["running"] = False
        loop.close()


async def relinkdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    if RELINK_STATE["running"]:
        await update.message.reply_text(
            f"⚠️ Relink လုပ်နေဆဲပါ, started {RELINK_STATE['started']}")
        return
    args = context.args or []
    dry = "run" not in args
    nums = [int(a) for a in args if a.isdigit()]
    start_id = nums[0] if len(nums) >= 1 else None
    end_id = nums[1] if len(nums) >= 2 else None
    limit = 0
    for a in args:
        if a.startswith("limit=") and a.split("=")[1].isdigit():
            limit = int(a.split("=")[1])
    threading.Thread(
        target=_relink_worker,
        args=(update.effective_user.id, dry, start_id, end_id, limit, context.bot.token),
        daemon=True).start()
    await update.message.reply_text(
        f"🔗 relinkdb စတင်ပါပြီ ({'DRY-RUN' if dry else 'RUN'})။\n"
        f"• ပထမ `/relinkdb` (dry) နဲ့ စမ်းပါ — ဘယ်နှခု match လဲ ကြည့်ပါ။\n"
        f"• ကောင်းရင် `/relinkdb run` နဲ့ တကယ်ရေးပါ။\n"
        f"• အပိုင်းလိုက်: `/relinkdb run <start> <end>` ဥပမာ `/relinkdb run 1 2000`\n"
        f"ပြီးတဲ့အခါ ဒီ chat ထဲ status ပြန်ပို့ပါမယ်။")


# ---------- Set Bot Commands ----------
async def set_commands(application: Application):
    await application.bot.set_my_commands([
        ("start", "Bot ကိုစတင်ရန်"),
        ("newpost", "ပို့စ်အသစ်ဖန်တီးရန် (ပုံ+စာ+Video)"),
        ("link", "Video တစ်ခုအတွက် Deep Link ထုတ်ရန်"),
        ("newfile", "Video တစ်ခုအတွက် Deep Link ထုတ်ရန်"),
        ("batchlink", "Video အများကြီးအတွက် Deep Link တစ်ခုတည်းထုတ်ရန်"),
        ("stats", "စာရင်းအင်းကြည့်ရန်"),
        ("broadcast", "အသုံးပြုသူအားလုံးကို စာပို့ရန်"),
        ("menu", "Admin Menu ပြသရန်"),
        ("mute", "Maintenance mode ဖွင့်ရန်"),
        ("unmute", "Maintenance mode ပိတ်ရန်"),
        ("migratedb", "DB channel သို့ movies migrate လုပ်ရန် (Admin)"),
        ("migrate_status", "Migration status ကြည့်ရန် (Admin)"),
        ("relinkdb", "ဖိုင်ဟောင်းတွေကို DB channel နဲ့ ပြန်ချိတ်ရန် (Admin)")
    ])

# ---------- Channel member join/leave notifications ----------
# Telegram only sends 'chat_member' updates when the bot is an administrator of
# the chat AND 'chat_member' is listed in allowed_updates (see Application
# builder below). Myanmar time is UTC+6:30.
MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))
JOINED_STATUSES = ("member", "administrator", "creator", "restricted")
GONE_STATUSES = ("left", "kicked")


def _member_display(user) -> str:
    name = " ".join(
        p for p in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if p
    )
    if getattr(user, "username", None):
        return f"{name} (@{user.username})" if name else f"@{user.username}"
    return name or str(user.id)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    recipients = set(ADMIN_IDS)
    recipients.add(OWNER_ID)
    for admin_id in recipients:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
            logger.info("Join/leave notice delivered to %s", admin_id)
        except TelegramError as e:
            logger.warning(
                "Could not notify %s: %s "
                "(that account must press /start once so the bot can PM it)",
                admin_id,
                e,
            )


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report channel joins/leaves to every admin in ADMIN_IDS."""
    change = update.chat_member
    if change is None:
        return

    chat = change.chat
    logger.info(
        "chat_member: chat=%s (%s) %s -> %s",
        chat.id,
        chat.type,
        change.old_chat_member.status,
        change.new_chat_member.status,
    )
    if NOTIFY_CHANNEL_IDS and chat.id not in NOTIFY_CHANNEL_IDS:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    joined = old_status in GONE_STATUSES and new_status in JOINED_STATUSES
    left = new_status in GONE_STATUSES and old_status in JOINED_STATUSES
    if not (joined or left):
        return

    member = change.new_chat_member.user if joined else change.old_chat_member.user
    actor = change.from_user
    when = change.date.astimezone(MYANMAR_TZ).strftime("%Y-%m-%d %H:%M:%S")

    lines = ["🟢 ချန်နယ်သို့ ဝင်လာသူ အသစ်" if joined else "🔴 ချန်နယ်မှ ထွက်သွားသူ"]
    lines.append(f"👤 အမည်: {_member_display(member)}")
    lines.append(f"🆔 ID: {member.id}")
    if chat.title:
        lines.append(f"📢 ချန်နယ်: {chat.title}")
    lines.append(f"🕐 အချိန်: {when} (MMT)")
    if actor and actor.id != member.id:
        lines.append(f"✍️ ဆောင်ရွက်သူ: {_member_display(actor)} (ID: {actor.id})")

    await _notify_admins(context, "\n".join(lines))


async def _log_notify_channel_status(bot):
    """Startup diagnostic: is the bot an admin in each notify channel?"""
    logger.info(
        "Join/leave notices go to these accounts: %s",
        sorted(set(ADMIN_IDS) | {OWNER_ID}),
    )
    if not NOTIFY_CHANNEL_IDS:
        logger.warning("No notify channels configured (set CHANNEL_IDS or CHANNEL_ID).")
        return
    try:
        me = await bot.get_me()
    except Exception as e:
        logger.warning("Startup self-check failed: %s", e)
        return
    for chat_id in NOTIFY_CHANNEL_IDS:
        try:
            member = await bot.get_chat_member(chat_id, me.id)
            logger.info("Notify channel %s: bot status=%s", chat_id, member.status)
            if member.status not in ("administrator", "creator"):
                logger.warning(
                    "Bot is NOT an admin in %s — Telegram will not send "
                    "join/leave updates for it.",
                    chat_id,
                )
        except Exception as e:
            logger.warning("Cannot read channel %s (bot not an admin?): %s", chat_id, e)


# ---------- Application ----------
def build_application(token):
    """Build a fresh Application for a given token.

    Handlers are (re)built per token so the bot can swap to a spare token at
    runtime without restarting the process.
    """
    application = Application.builder().token(token).post_init(
        _epsfix_post_init).build()

    newpost_handler = ConversationHandler(
        entry_points=[CommandHandler('newpost', newpost_start)],
        states={
            POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
            CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
            WAITING_VIDEO: [
                MessageHandler(filters.VIDEO, receive_video_after_caption),
                MessageHandler(filters.Document.ALL, receive_video_after_caption),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_video_after_caption)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_newpost)],
    )

    batchlink_handler = ConversationHandler(
        entry_points=[CommandHandler('batchlink', batchlink_start)],
        states={
        BATCH_WAITING_FILES: [
            MessageHandler(filters.VIDEO | filters.Document.ALL, batch_receive_file),
            MessageHandler(filters.TEXT & ~filters.COMMAND, batch_receive_text),
            CommandHandler('done', batch_done)
        ],
        },
        fallbacks=[CommandHandler('cancel', batch_cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_migrate_forward), group=-1)
    application.add_handler(CommandHandler("migrate_posts", migrate_posts_status))
    application.add_handler(CommandHandler("migrate_reset", migrate_reset_command))
    application.add_handler(CommandHandler("dbsave", dbsave_command))
    application.add_handler(CommandHandler("dbfix", dbfix_command))
    application.add_handler(CommandHandler("dbcheck", dbcheck_command))
    application.add_handler(CommandHandler("dbrepair", dbrepair_command))
    application.add_handler(CommandHandler("seriesfix", seriesfix_command))
    application.add_handler(CommandHandler("seriesmerge", seriesmerge_command))
    application.add_handler(CommandHandler("setsession", setsession_command))
    application.add_handler(CommandHandler("forwardstart", forwardstart_command))
    application.add_handler(CommandHandler("forwardstop", forwardstop_command))
    application.add_handler(CommandHandler("forwardstate", forwardstate_command))
    application.add_handler(CommandHandler("migstat", migstat_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("linkcheck", linkcheck_command))
    application.add_handler(CommandHandler("fixempty", fixempty_command))
    application.add_handler(CommandHandler("epfix", epfix_command))
    application.add_handler(CommandHandler("dbeps", dbeps_command))
    application.add_handler(CommandHandler("dbinfo", dbinfo_command))
    application.add_handler(newpost_handler)
    application.add_handler(batchlink_handler)
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    application.add_handler(CommandHandler("link", link_command))
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_link))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_video_for_link))
    application.add_handler(CommandHandler("newfile", newfile_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("schedule", schedule))
    application.add_handler(CommandHandler("listschedule", listschedule))
    application.add_handler(CommandHandler("cancelschedule", cancelschedule))
    application.add_handler(CommandHandler("delete", delete_file))
    application.add_handler(CommandHandler("deleteall", deleteall))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("mute", mute))
    application.add_handler(CommandHandler("unmute", unmute))
    application.add_handler(CommandHandler("migratedb", migratedb_command))
    application.add_handler(CommandHandler("migrate_status", migrate_status_command))
    application.add_handler(CommandHandler("relinkdb", relinkdb_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))
    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    # heartbeat: group -99 runs before everything, just counts traffic
    application.add_handler(TypeHandler(Update, _hb), group=-99)
    return application


# The live Application is built per-token so several bots can run at once.

# ---------- Polling ----------
def _bot_prepare(app, label, loop):
    """Validate the token, set the command menu and log channel-admin status.

    Runs on the SAME loop that run_polling() will use (PTB reuses the thread's
    current event loop). Using a separate asyncio.run() loop here would bind the
    bot's HTTP client to a different loop and break polling.
    """
    async def _prepare():
        # Raises InvalidToken for a dead token so the caller can give up on this
        # bot instead of retrying forever.
        me = await app.bot.get_me()
        LIVE["bots"][str(me.id)] = {
            "username": me.username,
            "label": label,
            "polling": True,
            "updates": 0,
            "last_update": 0.0,
            "last_error": "",
        }
        try:
            await set_commands(app)
        except Exception as e:
            logger.warning("%s set_my_commands failed: %s", label, e)
        try:
            await _log_notify_channel_status(app.bot)
        except Exception as e:
            logger.warning("%s channel status check failed: %s", label, e)

    loop.run_until_complete(_prepare())


def _run_bot_thread(token, label):
    """Poll one bot in its own thread + event loop.

    stop_signals=None: signal handlers may only be installed from the main
    thread, so background bots are stopped when the process exits. A dead
    token only kills that bot, leaving the other bots running.
    """
    while True:
        app = build_application(token)
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _bot_prepare(app, label, loop)
            LIVE["label"] = label
            LIVE["polling"] = True
            logger.info("Starting bot polling (%s)...", label)
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                stop_signals=None,
            )
            logger.info("Bot polling stopped (%s).", label)
            LIVE["polling"] = False
            return
        except InvalidToken as e:
            LIVE["polling"] = False
            logger.error("Token for %s is invalid/revoked: %s. Giving up on this bot.",
                         label, e)
            return
        except Exception as e:
            LIVE["polling"] = False
            LIVE["last_error"] = f"{type(e).__name__}: {e}"
            logger.exception("Bot polling crashed (%s): %s. Restarting in 10s",
                             label, e)
            time.sleep(10)


def run_bot():
    """Run the bot(s).

    * 1 token  -> a single bot in the main thread (graceful SIGTERM handling).
    * 2+ tokens (TELEGRAM_TOKEN_2, _3, ...) -> every bot runs SIMULTANEOUSLY,
      each in its own thread with its own event loop. All of them share the
      same Mongo data and movie channels, so each one serves its own users.
    """
    pairs = _load_token_pairs()
    if not pairs:
        logger.error("No TELEGRAM_TOKEN configured. Set TELEGRAM_TOKEN.")
        return

    if len(pairs) == 1:
        token, _username = pairs[0]
        LIVE["label"] = f"@{_username or '?'}"
        # Self-healing poll loop: a 409 Conflict (two instances / leftover
        # webhook) or a network blip must never leave the process alive with a
        # dead bot. Clear the webhook, retry with backoff, log every failure.
        attempt = 0
        while True:
            attempt += 1
            LIVE["loops"] = attempt
            app = build_application(token)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(set_commands(app))
                loop.run_until_complete(_log_notify_channel_status(app.bot))
                # A webhook left over from any earlier setup makes getUpdates
                # fail with 409 for ever; drop it before polling.
                loop.run_until_complete(
                    app.bot.delete_webhook(drop_pending_updates=True))
                LIVE["polling"] = True
                LIVE["last_error"] = ""
                logger.info("Starting bot polling... (attempt %d)", attempt)
                # drop_pending_updates: never replay stale queued updates after
                # a restart, otherwise old forwarded movies get reposted.
                app.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )
                logger.info("Bot polling stopped normally.")
                LIVE["polling"] = False
                if attempt >= 3:
                    break
            except InvalidToken:
                LIVE["polling"] = False
                LIVE["last_error"] = "InvalidToken"
                logger.error("TELEGRAM_TOKEN is invalid - cannot poll.")
                break
            except Exception as exc:
                LIVE["polling"] = False
                LIVE["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("Bot polling crashed (%s). Retrying in 15s",
                                 exc)
                time.sleep(15)
                continue
            time.sleep(5)
        return

    logger.info("Running %d bots SIMULTANEOUSLY.", len(pairs))
    threads = []
    for i, (token, username) in enumerate(pairs, start=1):
        label = f"bot {i}/{len(pairs)} @{username or '?'}"
        t = threading.Thread(target=_run_bot_thread, args=(token, label), daemon=True)
        t.start()
        threads.append(t)
        # Stagger starts so the bots do not all hit Telegram at once.
        time.sleep(3)

    # Flask is already serving in its own thread; keep the main thread alive.
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(30)
        logger.error("All bot threads have exited.")
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down.")

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    migrate_old_documents()
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        import cloud_forwarder
        if cloud_forwarder.start_in_background():
            logger.info("Cloud forwarder started.")
    except Exception as exc:
        logger.warning("Cloud forwarder start failed: %s", exc)
    run_bot()



