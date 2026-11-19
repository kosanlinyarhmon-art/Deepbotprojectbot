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
import re
import time
import urllib.parse
import urllib.request

from pymongo import MongoClient
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import MessageService

logger = logging.getLogger(__name__)

API_ID = 31778137
API_HASH = "357247ed45b28459c6600e31cce5b773"
TARGET_BOT = "@UwinzawnyuntBot"
DEFAULT_HUBS = ["-1002531024735"]
_LINK = re.compile(
    r"(?:https?://)?t\.me/(?:c/(\d+)/(\d+)|(\+[A-Za-z0-9_-]+)"
    r"|([A-Za-z][\w_]{3,})(?:/(\d+))?)")
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
LAST_LOG = ""
LAST_LOG_TS = 0.0
_NOTIFY_SEEN = {}


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


def paused():
    """True when /forwardstop was pressed: stop forwarding immediately.

    ``enabled`` is re-set to True by the scan loop itself, so the pause needs
    its own flag - otherwise a restart or the next loop iteration resumes.
    """
    return bool(config().get("paused"))


def running():
    """True while the forwarder thread is actually scanning sources."""
    return bool(_task is not None and _task.is_alive())


def state_col():
    return _db()["forward_state"]


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    logger.info("[forwarder] %s", msg)
    global LAST_LOG, LAST_LOG_TS
    LAST_LOG, LAST_LOG_TS = msg[:300], time.time()
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
    # Watch mode re-scans every few minutes: never repeat the same note.
    now = time.time()
    for k, ts in list(_NOTIFY_SEEN.items()):
        if now - ts > 1800:
            _NOTIFY_SEEN.pop(k, None)
    if text in _NOTIFY_SEEN:
        return
    _NOTIFY_SEEN[text] = now
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
    while not _stop and not paused():
        remain = target - time.time()
        if remain <= 0:
            return
        log(f"   drain: bot still publishing, waiting {int(remain)}s")
        await asyncio.sleep(min(60, max(5, remain)))


async def forward_channel(client, bot, raw_source, per_item, min_delay,
                          watch=False):
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
    if st.get("done") and not watch:
        log(f"== SKIP {title} (already finished)")
        return
    if not watch:
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
        if _stop or paused():
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
    if not _stop and not paused():
        log(f"== FORWARDED ALL {title} ({sent} msgs) - draining")
        await drain(sent, chan_started, per_item)
        if watch:
            state_col().update_one(
                {"_id": key},
                {"$set": {"last_scan": time.time()}, "$unset": {"done": ""}},
                upsert=True)
        else:
            state_col().update_one({"_id": key}, {"$set": {"done": 1}}, upsert=True)
        if watch:
            if sent:
                log(f"== NEW {title}: {sent} msg")
        else:
            log(f"== DONE {title}")
            notify(f"✅ ပြီးသွားပြီ: {title} ({sent} msg)")


async def forward_hub(client, bot, raw_hub, per_item, min_delay, watch=False):
    """Link-hub channel: join the channels it links to, forward the movies the
    links point at, then leave again (only channels we joined ourselves)."""
    hub_id = to_entity_id(raw_hub)
    try:
        hub = await client.get_entity(hub_id)
    except Exception as exc:
        log(f"SKIP hub {raw_hub}: cannot resolve ({exc})")
        return
    title = getattr(hub, "title", str(hub_id))
    key = str(hub_id)
    st = state_col().find_one({"_id": key}) or {"_id": key, "last_id": 0}
    last_id = int(st.get("last_id") or 0)
    if st.get("done") and not watch:
        log(f"== SKIP hub {title} (already finished)")
        return
    log(f"== START hub {title} ({hub_id}) from id > {last_id}")
    notify(f"🚚 Hub စတင်ပါပြီ: {title}")

    seen = _db()["hub_seen"]
    joined = set()
    sent = 0
    last_note = time.time()

    async def flush(msgs):
        nonlocal sent, last_note
        by_chat = {}
        for m in msgs:
            by_chat.setdefault(m.chat_id, []).append(m)
        for ch, group in by_chat.items():
            ids = [m.id for m in group]
            try:
                await client.forward_messages(bot, ids, from_peer=ch)
                for m in group:
                    seen.update_one({"_id": f"{ch}:{m.id}"},
                                    {"$set": {"ts": time.time()}},
                                    upsert=True)
                sent += len(ids)
                log(f"   hub forwarded {len(ids)} from {ch}")
                if time.time() - last_note > 900:
                    last_note = time.time()
                    notify(f"🚚 {title}\n{sent} msg တင်ပြီး ဆက်လုပ်နေတယ်")
            except FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 5)
            except Exception as exc:
                log(f"   hub forward failed {ch} {ids}: {exc}")
            await asyncio.sleep(max(min_delay, per_item * len(ids) / 2))

    async for msg in client.iter_messages(hub, min_id=last_id, reverse=True):
        if _stop or paused():
            break
        text = msg.message or ""
        targets = []
        invites = []
        for cid, mid, inv, uname, umid in _LINK.findall(text):
            if cid and mid:
                targets.append((int("-100" + cid), int(mid)))
            elif uname and umid:
                targets.append((uname, int(umid)))
            elif inv:
                invites.append(inv)
        if not targets and not invites:
            state_col().update_one({"_id": key},
                                   {"$set": {"last_id": msg.id}},
                                   upsert=True)
            continue

        # join the channels this post points at (they are usually private)
        for inv in invites:
            link = "https://t.me/" + (inv if inv.startswith("+") else "+" + inv)
            try:
                ch = await client.get_entity(link)
                _add_extra_source(ch.id)
                if getattr(ch, "left", True):
                    await client(JoinChannelRequest(link))
                    joined.add(ch.id)
                    await asyncio.sleep(2)
                    log(f"   joined {getattr(ch, 'title', ch.id)}")
            except Exception as exc:
                log(f"   join {link} failed: {type(exc).__name__}")

        batch = []
        for ref, mid in targets:
            if isinstance(ref, int):
                _add_extra_source(ref)
            if seen.find_one({"_id": f"{ref}:{mid}"}):
                continue
            try:
                m = await client.get_messages(ref, ids=mid)
            except Exception:
                m = None
            if not m or not getattr(m, "media", None):
                continue
            batch.append(m)
        if batch:
            await flush(batch)

        state_col().update_one({"_id": key},
                               {"$set": {"last_id": msg.id, "title": title}},
                               upsert=True)

        # leave the channels we joined for this post
        for ch_id in list(joined):
            try:
                await client(LeaveChannelRequest(ch_id))
                log(f"   left {ch_id}")
            except Exception:
                pass
            joined.discard(ch_id)

    if not _stop and not paused():
        if watch:
            state_col().update_one(
                {"_id": key},
                {"$set": {"last_scan": time.time()}, "$unset": {"done": ""}},
                upsert=True)
        else:
            state_col().update_one({"_id": key}, {"$set": {"done": 1}}, upsert=True)
        if watch:
            if sent:
                log(f"== NEW hub {title}: {sent} msg")
        else:
            log(f"== DONE hub {title} ({sent} msgs)")
            notify(f"✅ Hub ပြီးသွားပြီ: {title} ({sent} msg)")


_EXTRA_ADDED = set()


def _add_extra_source(channel_id):
    cid = int(channel_id)
    if cid in _EXTRA_ADDED:
        return
    _EXTRA_ADDED.add(cid)
    try:
        _db()["config"].update_one(
            {"_id": "migration"},
            {"$addToSet": {"extra_sources": cid}}, upsert=True)
    except Exception:
        pass


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
    watch = bool(cfg.get("watch", True))

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
    if not cfg.get("watching"):
        try:
            await client.send_message(bot, "/start")
            await asyncio.sleep(2)
        except Exception as exc:
            log(f"warning: /start failed: {exc}")

    # 24/7: keep the userbot connected and re-scan for new posts. Reconnecting
    # every few minutes would force a fresh session login each time.
    while not _stop:
        if paused():
            log("paused by /forwardstop - stopping")
            break
        hubs = config().get("hubs") or DEFAULT_HUBS
        for hub in hubs:
            if _stop or paused():
                break
            await forward_hub(client, bot, hub, per_item, min_delay, watch)

        for src in (config().get("sources") or DEFAULT_SOURCES):
            if _stop or paused():
                break
            await forward_channel(client, bot, src, per_item, min_delay, watch)

        if _stop or paused():
            break
        if not watch:
            log("all channels finished")
            set_config(enabled=False, finished_at=time.time())
            break
        interval = int(config().get("watch_interval") or 300)
        set_config(enabled=True, watching=True, last_scan=time.time())
        log(f"scan finished - watching for new posts (next scan in {interval}s)")
        # interruptible wait: /forwardstop must take effect within seconds
        waited = 0
        while (waited < interval and not _stop and not paused()):
            await asyncio.sleep(2)
            waited += 2

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
        if _stop or paused() or not config().get("enabled"):
            return
        time.sleep(30)


def start_in_background():
    """Start the forwarder thread if it should run and is not running yet."""
    global _task
    import threading
    if _task and _task.is_alive():
        return False
    if paused():
        return False
    cfg = config()
    if not (cfg.get("enabled") or cfg.get("watch", True)):
        return False
    if not (cfg.get("session") or "").strip():
        return False
    if not cfg.get("enabled"):
        set_config(enabled=True)
    _task = threading.Thread(target=main_thread, daemon=True)
    _task.start()
    return True


def stop():
    global _stop
    _stop = True
