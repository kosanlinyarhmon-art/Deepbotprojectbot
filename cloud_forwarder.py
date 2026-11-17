"""Cloud forwarder: run the userbot migration inside the bot's own process.

No PC needed.  The session string and settings live in Mongo
(``config`` document ``_id="forwarder"``), so the admin only has to send
``/setsession <string>`` and ``/forwardstart`` once.

State is kept per source channel in ``forward_state`` (same DB as the bot), so
the run always continues from the last forwarded message.
"""

import asyncio
import logging
import os
import time
import urllib.parse
import urllib.request

from pymongo import MongoClient
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import MessageService

logger = logging.getLogger(__name__)

API_ID = 31778137
API_HASH = "357247ed45b28459c6600e31cce5b773"
TARGET_BOT = "@UwinzawnyuntBot"
DEFAULT_SOURCES = [
    "-1002384798185",
    "-1002463885600",
    "-1002190088840",
    "-1002480433985",
    "-1002477764635",
]

_client = None
_task = None
_stop = False


def _db():
    import sys
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "db"):
        return main.db
    from app import db  # reuse the bot's Mongo connection / database
    return db


def config():
    doc = _db()["config"].find_one({"_id": "forwarder"}) or {}
    return doc


def set_config(**fields):
    _db()["config"].update_one({"_id": "forwarder"},
                               {"$set": fields}, upsert=True)


def state_col():
    return _db()["forward_state"]


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    logger.info("[forwarder] %s", msg)
    try:
        _db()["forward_log"].insert_one({"ts": time.time(), "msg": msg[:500]})
    except Exception:
        pass
    return line


def notify(text):
    """Send a progress note to the admin's Telegram via the bot API."""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        return
    chat = str(config().get("admin_chat") or 1147922719)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data,
            timeout=20)
    except Exception:
        pass


def to_entity_id(raw):
    raw = str(raw).strip()
    if "t.me/c/" in raw:
        raw = raw.split("t.me/c/")[1].split("/")[0]
        return int("-100" + raw)
    return int(raw)


async def drain(sent, started, per_item):
    if sent <= 0:
        return
    target = started + sent * per_item + 90
    while not _stop:
        remain = target - time.time()
        if remain <= 0:
            return
        log(f"   drain: bot still publishing, waiting {int(remain)}s")
        await asyncio.sleep(min(60, max(5, remain)))


async def forward_channel(client, bot, raw_source, per_item, min_delay):
    source_id = to_entity_id(raw_source)
    try:
        source = await client.get_entity(source_id)
    except Exception as exc:
        log(f"SKIP {raw_source}: cannot resolve ({exc})")
        return
    title = getattr(source, "title", str(source_id))
    key = str(source_id)
    st = state_col().find_one({"_id": key}) or {"_id": key, "last_id": 0}
    last_id = int(st.get("last_id") or 0)
    if st.get("done"):
        log(f"== SKIP {title} (already finished)")
        return
    log(f"== START {title} ({source_id}) resume from id > {last_id}")
    notify(f"🚚 စတင်ပါပြီ: {title}\nid {last_id} ကနေ ဆက်တင်နေတယ်")

    pending = []
    sent = 0
    chan_started = time.time()
    last_note = time.time()

    async def flush():
        nonlocal pending, sent, last_note
        if not pending:
            return
        ids = [m.id for m in pending]
        try:
            await client.forward_messages(bot, ids, from_peer=source)
            state_col().update_one(
                {"_id": key},
                {"$set": {"last_id": max(ids), "title": title},
                 "$inc": {"done": len(ids)}},
                upsert=True)
            sent += len(ids)
            log(f"   forwarded {len(ids)}: {ids[0]}..{ids[-1]} (total {sent})")
            if time.time() - last_note > 900:
                last_note = time.time()
                notify(f"🚚 {title}\nid {max(ids)} အထိ တင်ပြီး "
                       f"({sent} msg)\nဆက်လုပ်နေတယ်")
        except FloodWaitError as exc:
            log(f"   FloodWait {exc.seconds}s")
            await asyncio.sleep(exc.seconds + 5)
            return await flush()
        except RPCError as exc:
            log(f"   FAILED {ids}: {exc}")
            state_col().update_one(
                {"_id": key},
                {"$set": {"last_id": max(ids)}, "$inc": {"failed": len(ids)}},
                upsert=True)
            sent += len(ids)
        pending = []
        await asyncio.sleep(max(min_delay, per_item * len(ids)))

    async for msg in client.iter_messages(source, min_id=last_id, reverse=True):
        if _stop:
            break
        if isinstance(msg, MessageService):
            continue
        if not (msg.media or (msg.message and msg.message.strip())):
            continue
        gid = getattr(msg, "grouped_id", None)
        if gid:
            if pending and getattr(pending[0], "grouped_id", None) != gid:
                await flush()
            pending.append(msg)
            if len(pending) >= 10:
                await flush()
            continue
        await flush()
        pending = [msg]
        await flush()

    await flush()
    if not _stop:
        log(f"== FORWARDED ALL {title} ({sent} msgs) - draining")
        await drain(sent, chan_started, per_item)
        state_col().update_one({"_id": key}, {"$set": {"done": 1}}, upsert=True)
        log(f"== DONE {title}")
        notify(f"✅ ပြီးသွားပြီ: {title} ({sent} msg)")


async def _run():
    global _stop, _client
    cfg = config()
    session = (cfg.get("session") or "").strip()
    if not session:
        log("no session configured - nothing to do")
        return
    sources = cfg.get("sources") or DEFAULT_SOURCES
    per_item = float(cfg.get("per_item") or 3.8)
    min_delay = float(cfg.get("min_delay") or 2)

    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    _client = client
    await client.connect()
    if not await client.is_user_authorized():
        log("session is not authorised - /setsession again")
        await client.disconnect()
        return
    me = await client.get_me()
    log(f"userbot: {me.first_name} ({me.id})")
    bot = await client.get_entity(TARGET_BOT)
    log(f"target bot: {getattr(bot, 'username', TARGET_BOT)} ({bot.id})")
    try:
        await client.send_message(bot, "/start")
        await asyncio.sleep(2)
    except Exception as exc:
        log(f"warning: /start failed: {exc}")

    for src in sources:
        if _stop:
            break
        await forward_channel(client, bot, src, per_item, min_delay)

    if not _stop:
        log("all channels finished")
        set_config(enabled=False, finished_at=time.time())
    await client.disconnect()
    _client = None


def main_thread():
    """Blocking entry point - run inside a daemon thread."""
    global _stop
    while True:
        _stop = False
        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.exception("forwarder crashed: %s", exc)
        if _stop or not config().get("enabled"):
            return
        time.sleep(30)


def start_in_background():
    """Start the forwarder thread if it is enabled and not already running."""
    global _task
    import threading
    if _task and _task.is_alive():
        return False
    if not config().get("enabled"):
        return False
    _task = threading.Thread(target=main_thread, daemon=True)
    _task.start()
    return True


def stop():
    global _stop
    _stop = True
