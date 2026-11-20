"""Cloud rebuild worker.

Runs inside the deepbot Flask process.  The user-account Telethon session and
the on/off switch live in MongoDB (worker_config), so no Render env var or
local PC is needed.  Progress lives in MongoDB too, so a Render restart simply
resumes where it stopped.

USER ACCOUNT -> source channel -> @UwinzawnyuntBot /newpost -> channel post.
The old post is deleted only after the replacement is fully verified.
"""
import asyncio
import logging
import re
import threading
import time

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

from series_admin import _range_title

LOG = logging.getLogger("series_worker")
CHANNEL = -1003753299714
BOT = "@UwinzawnyuntBot"
API_ID = 31778137
API_HASH = "357247ed45b28459c6600e31cce5b773"


async def retry(coro_factory, times=4):
    for n in range(times):
        try:
            return await coro_factory()
        except FloodWaitError as exc:
            await asyncio.sleep(int(exc.seconds) + 3)
        except RPCError as exc:
            if n == times - 1:
                raise
            await asyncio.sleep(3 + n * 3)
    raise RuntimeError("retry exhausted")


def clean_syn(text):
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if len(line) < 70:
            continue
        low = line.lower()
        if ("t.me/" in low or "http" in low or line.startswith("@")
                or "upload by" in low or "ညွှန်း" in line):
            continue
        if set(line) <= set("=-_~*#• 0123456789"):
            continue
        lines.append(line)
    return "\n".join(lines[:3])[:900]


def split_source(text):
    title = ""
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or set(ln) <= set("=-_~*#• 0123456789"):
            continue
        if len(ln) < 70 and not title:
            title = ln
    return title, clean_syn(text)


STOP_WORDS = {"the", "a", "an", "s", "season", "ep", "episode", "episodes",
              "cm", "end", "complete", "final", "part", "dual", "web", "dl",
              "ita", "ger", "series", "sub", "subbed"}


def series_sig(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    words = [w for w in words if len(w) >= 3 and w not in STOP_WORDS
             and not (len(w) == 4 and w.isdigit())]
    return " ".join(words[:2])


EPISODE_RE = re.compile(r"^\s*(?:ep(?:isode)?|part)\s*[\.\-_]?\s*\d+", re.I)


def is_video_msg(m):
    return bool(m and (m.video or (
        m.document and str(getattr(m.document, "mime_type", "")).startswith("video"))))


def is_episode_continuation(m):
    """A later album where every episode is captioned 'Ep 2', 'Ep 3', ...

    These belong to the same series as the anchored poster even though they
    do not repeat its name, so they must not end the series window.
    """
    return bool(m and m.message and EPISODE_RE.match(m.message.strip()))


def norm_text(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def fuzzy_match(text, sig):
    """Source captions are often misspelt: Disenchanment / disenchantment."""
    norm = norm_text(text)
    if not sig or not norm:
        return False
    if sig in norm:
        return True
    return len(sig) >= 5 and sig.split()[0][:5] in norm


async def locate_anchor(client, ent, item):
    """Older docs have no src_msg_id — find the source post by its title."""
    sig = series_sig(item.get("title"))
    word = sig.split()[0] if sig else ""
    if not word:
        return None
    found = await retry(lambda: client.get_messages(ent, search=word, limit=100))
    cands = [m for m in found if m and fuzzy_match(m.message, sig)] or \
            [m for m in found if m]
    if not cands:
        return None
    cands.sort(key=lambda m: (0 if m.photo else 1, m.id))
    return cands[0].id


async def scan_source(client, item):
    src = int(str(item["source"]).strip())
    raw_start = item.get("src_msg_id")
    start = (int(str(raw_start).strip())
             if raw_start not in (None, "", "None") else None)
    ent = await retry(lambda: client.get_entity(src))
    if start is None:
        start = await locate_anchor(client, ent, item)
        if start is None:
            return None, [], "", ""
    newest = (await retry(lambda: client.get_messages(ent, limit=1)))[0].id
    lo, hi = max(1, start - 25), min(newest, start + 120)
    msgs = await retry(lambda: client.get_messages(
        ent, ids=list(range(lo, hi + 1))))
    msgs = [m for m in msgs if m]
    by_id = {m.id: m for m in msgs}

    sig = series_sig(item.get("title"))
    candidates = [m for m in msgs if fuzzy_match(m.message, sig)
                  or is_episode_continuation(m)]
    groups = {m.grouped_id for m in candidates if m.grouped_id}
    posters = []
    for m in msgs:
        if not m.photo:
            continue
        if fuzzy_match(m.message, sig) or (m.grouped_id and m.grouped_id in groups):
            posters.append(m)
    if not posters:
        near = [m for m in msgs if m.photo and m.id <= start
                and start - m.id <= 14]
        if near:
            posters.append(max(near, key=lambda m: m.id))
    posters.sort(key=lambda m: m.id)
    if not posters:
        return None, [], "", ""

    # Do not run into the next series when this one has no repeated captions.
    others = [m.id for m in msgs if m.id >= start and m.message
              and len(norm_text(m.message)) > 3
              and not fuzzy_match(m.message, sig)
              and not is_episode_continuation(m)]
    if others:
        hi = min(hi, min(others) - 1)
    # The final labelled video's album includes its trailing poster; the next
    # album belongs to another series even when its captions are empty.
    if candidates:
        last = max(candidates, key=lambda m: m.id)
        if last.grouped_id:
            tail = [m.id for m in msgs if m.grouped_id == last.grouped_id]
            if tail:
                hi = min(hi, max(tail))

    videos = set()
    for m in candidates:
        if is_video_msg(m) and m.id <= hi:
            videos.add(m.id)
    for m in msgs:
        if m.grouped_id and m.grouped_id in groups and is_video_msg(m) and m.id <= hi:
            videos.add(m.id)
    for p in posters:
        seen_gap = 0
        for mid in range(p.id + 1, min(hi, p.id + 40) + 1):
            m = by_id.get(mid)
            if not m:
                seen_gap += 1
                if seen_gap >= 3:
                    break
                continue
            if m.photo and fuzzy_match(m.message, sig):
                break
            if is_video_msg(m):
                videos.add(mid)
                seen_gap = 0
                if len(videos) >= 60:
                    break
                continue
            seen_gap += 1
            if seen_gap >= 3:
                break
    videos = sorted(videos)
    if not videos:
        return None, [], "", ""
    poster = max((p for p in posters if p.id <= start),
                 key=lambda m: m.id, default=posters[0])

    syn_source, src_title = "", ""
    nearby = [m for m in msgs if m.id >= poster.id and m.id <= videos[0]
              and m.message]
    for m in nearby:
        title, body = split_source(m.message)
        if len(body) > len(syn_source):
            syn_source, src_title = body, title
    if not src_title:
        labeled = sorted((m for m in candidates if m.message),
                         key=lambda m: m.id)
        if labeled:
            src_title, _ = split_source(labeled[0].message)
    return poster.id, videos, syn_source, src_title


async def latest_channel(client):
    return (await retry(lambda: client.get_messages(CHANNEL, limit=1)))[0]


async def wait_new_post(client, before, timeout=600):
    """Watch for the published post using a server-side id filter.

    get_messages(limit=1) can return a stale cached "latest" message when the
    user session's channel difference is out of date, which made real posts
    look like failures.  min_id is resolved by Telegram, so it never depends
    on that local state.  The bot also occasionally stalls for minutes, hence
    the longer timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        found = await retry(lambda: client.get_messages(
            CHANNEL, min_id=before, limit=10))
        new = [m for m in found if m and m.id > before and m.photo]
        if new:
            return max(new, key=lambda m: m.id)
        await asyncio.sleep(10)
    return None


async def rebuild(client, bot, item):
    poster, videos, source_syn, source_title = await scan_source(client, item)
    if not poster or not videos:
        return False, f"no poster/videos (poster={poster}, videos={len(videos)})"

    exact_title = source_title or item["title"]
    synopsis = source_syn or clean_syn(item.get("synopsis"))
    if "ဇာတ်ကားအကြောင်း" not in synopsis and len(synopsis) < 40:
        synopsis = ""
    caption = exact_title + ("\n" + synopsis if synopsis else "")
    before = (await latest_channel(client)).id

    await retry(lambda: client.send_message(bot, "/cancel"))
    await asyncio.sleep(1.5)
    await retry(lambda: client.send_message(bot, "/newpost"))
    await asyncio.sleep(2)
    await retry(lambda: client.forward_messages(
        bot, [poster], from_peer=int(str(item["source"]).strip())))
    await asyncio.sleep(1.4)
    await retry(lambda: client.send_message(bot, caption))
    await asyncio.sleep(0.8)
    await retry(lambda: client.send_message(bot, "a"))
    await asyncio.sleep(1.5)
    for vid in videos:
        await retry(lambda v=vid: client.forward_messages(
            bot, [v], from_peer=int(str(item["source"]).strip())))
        await asyncio.sleep(1.2)
    await retry(lambda: client.send_message(bot, "a"))
    # Bot DM replies can be minutes late, so verify the published post itself
    # instead of trusting acknowledgement text in the shared DM.
    after = await wait_new_post(client, before)
    if not after:
        return False, "no new channel post within 600s"
    shown = await retry(lambda: client.get_messages(CHANNEL, ids=[after.id]))
    msg = shown[0]
    if not msg:
        return False, "new post unreadable"
    rows = getattr(getattr(msg, "reply_markup", None), "rows", None) or []
    buttons = [b.text for row in rows for b in row.buttons]
    final_cap = msg.message or ""
    if not msg.photo or not buttons:
        return False, "new post is not a verified photo post with buttons"
    parts = series_sig(exact_title).split()
    key = parts[0] if parts else exact_title.split()[0]
    if key.lower() not in final_cap.lower():
        return False, f"new post belongs to another series ({key})"
    if synopsis:
        if synopsis[:40] not in final_cap:
            return False, "new post is missing the synopsis"
    elif exact_title.split()[0] not in final_cap:
        return False, "new post is not this series"

    if source_title:
        _, sep, tail = final_cap.partition("\n")
        budget = 1024 - len(exact_title) - len(sep)
        if len(tail) > budget:
            tail = tail[:max(0, budget - 3)].rstrip() + "..."
        exact_cap = exact_title + sep + tail
        try:
            await retry(lambda: client.edit_message(CHANNEL, after.id, text=exact_cap))
            await asyncio.sleep(0.5)
            check = (await retry(lambda: client.get_messages(
                CHANNEL, ids=[after.id])))[0]
            check_cap = check.message or ""
            check_rows = getattr(getattr(check, "reply_markup", None),
                                 "rows", None) or []
            check_buttons = [b.text for row in check_rows for b in row.buttons]
            if exact_title not in check_cap or not check_buttons:
                return False, "exact title edit did not verify"
        except Exception as exc:
            return False, f"exact title edit failed: {exc}"

    old = int(item.get("post") or 0)
    if old and old != after.id:
        try:
            await retry(lambda: client.delete_messages(CHANNEL, [old]))
        except Exception:
            pass
    return True, {"new": after.id, "old": old, "files": len(videos)}


def _make_item(db, doc):
    payload = doc.get("payload")
    fdoc = db["file_store"].find_one({"payload": payload}) if payload else None
    files = (fdoc or {}).get("files", []) or []
    return {
        "id": str(doc["_id"]),
        "title": _range_title(doc, files),
        "post": doc.get("post_id"),
        "source": doc.get("source"),
        "src_msg_id": doc.get("src_msg_id"),
        "episodes": doc.get("episodes"),
        "synopsis": doc.get("synopsis") or "",
    }


async def run_all(db):
    cfg = db["worker_config"]
    secret = cfg.find_one({"_id": "USER_SESSION"})
    if not secret or not secret.get("value"):
        LOG.warning("no USER_SESSION in worker_config")
        return
    client = TelegramClient(StringSession(secret["value"]), API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    LOG.info("series worker connected as %s", me.id)
    bot = await retry(lambda: client.get_entity(BOT))
    progress = db["rebuild_progress"]
    query = {"series_key": {"$exists": True},
             "merged_into": {"$exists": False},
             "payload": {"$exists": True}}
    try:
        while cfg.find_one({"_id": "REBUILD_ENABLED", "value": True}):
            prog = {str(r["_id"]): r for r in progress.find({})}
            done = {k for k, r in prog.items() if r.get("ok") or r.get("settled")}
            docs = list(db["migrated_posts"].find(query).sort("post_id", -1).limit(600))
            # A genuine failure is tried at most twice; it must never spin
            # forever and keep publishing duplicate posts.
            todo = [d for d in docs
                    if str(d["_id"]) not in done
                    and int(prog.get(str(d["_id"]), {}).get("tries") or 0) < 2]
            if not todo:
                LOG.info("series worker: everything done, idling")
                await asyncio.sleep(60)
                continue
            LOG.info("series worker: %d remaining", len(todo))
            for d in todo:
                if not cfg.find_one({"_id": "REBUILD_ENABLED", "value": True}):
                    break
                item = _make_item(db, d)
                key = item["id"]
                tries = int(prog.get(key, {}).get("tries") or 0) + 1
                try:
                    ok, result = await rebuild(client, bot, item)
                    rec = {"ok": ok, "ts": int(time.time()),
                           "title": item["title"], "tries": tries}
                    rec.update(result if ok else {"why": str(result)[:250]})
                    if not ok and tries >= 2:
                        rec["settled"] = True
                    progress.update_one({"_id": key}, {"$set": rec}, upsert=True)
                    if ok:
                        LOG.info("OK %s %s -> %s (%s files)", item["title"],
                                 result["old"], result["new"], result["files"])
                    else:
                        LOG.warning("FAIL %s: %s", item["title"], result)
                except Exception as exc:
                    LOG.exception("series worker item failed: %s", item["title"])
                    rec = {"ok": False, "ts": int(time.time()),
                           "title": item["title"], "why": str(exc)[:250],
                           "tries": tries}
                    if tries >= 2:
                        rec["settled"] = True
                    progress.update_one({"_id": key}, {"$set": rec}, upsert=True)
                await asyncio.sleep(3)
    finally:
        await client.disconnect()


def settle_failed(db):
    """Stop retrying records that already produced a channel post but were
    marked failed by the old, broken verification check."""
    return db["rebuild_progress"].update_many(
        {"ok": False}, {"$set": {"settled": True}}).modified_count


def settle_false_confirm(db):
    """Only the records hit by the outdated DM-based verification checks."""
    return db["rebuild_progress"].update_many(
        {"ok": False, "why": {"$regex": "bot did not confirm|bot confirmed a different"}},
        {"$set": {"settled": True}}).modified_count


def settle_pattern(db, pattern):
    """Mark failed records matching a regex as settled (no more retries)."""
    return db["rebuild_progress"].update_many(
        {"ok": False, "why": {"$regex": pattern}},
        {"$set": {"settled": True}}).modified_count


def retry_pattern(db, pattern):
    """Give failed records matching a regex another chance.

    Used after a scanner/verification fix: clear 'settled' and reset 'tries'
    so the worker actually reprocesses them instead of idling.
    """
    progress = db["rebuild_progress"]
    ids = [r["_id"] for r in progress.find(
        {"why": {"$regex": pattern}}, {"_id": 1})]
    if ids:
        progress.update_many(
            {"_id": {"$in": ids}},
            {"$unset": {"settled": "", "why": ""},
             "$set": {"ok": False, "tries": 0}})
    return len(ids)


def progress_summary(db):
    progress = db["rebuild_progress"]
    return {
        "done": progress.count_documents({"ok": True}),
        "failed": progress.count_documents({"ok": False}),
        "settled": progress.count_documents({"settled": True}),
        "retryable": progress.count_documents(
            {"ok": False, "settled": {"$exists": False},
             "tries": {"$lt": 2}}),
    }


def status(db):
    cfg = db["worker_config"]
    progress = db["rebuild_progress"]
    return {
        "ver": 6,
        "progress": progress_summary(db),
        "enabled": bool(cfg.find_one({"_id": "REBUILD_ENABLED", "value": True})),
        "has_session": bool(cfg.find_one({"_id": "USER_SESSION"})),
        "ok": progress.count_documents({"ok": True}),
        "failed": progress.count_documents({"ok": False}),
        "total": db["migrated_posts"].count_documents({
            "series_key": {"$exists": True},
            "merged_into": {"$exists": False},
            "payload": {"$exists": True}}),
        "recent": [{"title": r.get("title"), "new": r.get("new"),
                    "files": r.get("files"), "ts": r.get("ts")}
                   for r in progress.find({"ok": True}).sort("ts", -1).limit(10)],
        "recent_failed": [{"title": r.get("title"), "why": r.get("why"),
                           "ts": r.get("ts")}
                          for r in progress.find({"ok": False}).sort("ts", -1).limit(10)],
        "reasons": [{"why": r["_id"], "n": r["n"]} for r in progress.aggregate([
            {"$match": {"ok": False}},
            {"$group": {"_id": {"$substr": ["$why", 0, 90]}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 10}])],
    }


def _thread(db):
    while True:
        try:
            asyncio.run(run_all(db))
        except Exception:
            LOG.exception("series worker crashed; restarting in 30s")
        time.sleep(30)


def start(db):
    threading.Thread(target=_thread, args=(db,), daemon=True,
                     name="series-rebuild-worker").start()
