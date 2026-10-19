import os
import asyncio
import threading
import logging
import sys
import secrets
import re
import unicodedata
import time
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler, ChatMemberHandler
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
    if context.user_data.get('waiting_for_video_link'):
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
            context.user_data.pop('waiting_for_video_link', None)
        else:
            await update.message.reply_text("ဖိုင် တစ်ခု ပို့ပေးပါ။")

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
    application = Application.builder().token(token).build()

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
    application.add_handler(newpost_handler)
    application.add_handler(batchlink_handler)
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    application.add_handler(CommandHandler("link", link_command))
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_link))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_video_for_link))
    application.add_handler(CommandHandler("newfile", newfile_command))
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_newfile))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_video_for_newfile))
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
        await app.bot.get_me()
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
            logger.info("Starting bot polling (%s)...", label)
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                stop_signals=None,
            )
            logger.info("Bot polling stopped (%s).", label)
            return
        except InvalidToken as e:
            logger.error("Token for %s is invalid/revoked: %s. Giving up on this bot.",
                         label, e)
            return
        except Exception as e:
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
        app = build_application(token)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(set_commands(app))
        loop.run_until_complete(_log_notify_channel_status(app.bot))
        logger.info("Starting bot polling...")
        # drop_pending_updates: never replay stale queued updates after a
        # restart, otherwise old forwarded movies get reposted/redelivered.
        # allowed_updates MUST include chat_member or Telegram never sends
        # the channel join/leave notifications (PTB takes it here, not on
        # the builder).
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Bot polling stopped normally.")
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
    run_bot()



