import asyncio, json, os, re, sys
from telegram import Bot
from telegram.error import TelegramError

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SRC = int(os.environ.get("MIGRATE_SOURCE", "-1003753299714"))
DST = int(os.environ.get("DATABASE_CHANNEL_ID", os.environ.get("DB_CHANNEL", "0")))
SCRATCH = int(os.environ.get("SCRATCH_CHAT", os.environ.get("ADMIN_ID", "1147922719")))
JSON_FILE = os.environ.get("MIGRATE_JSON", "old_posts.json")
PROGRESS_FILE = os.environ.get("MIGRATE_PROGRESS", "migrate_progress.json")
DRY = os.environ.get("DRY_RUN", "0") == "1"
LIMIT = int(os.environ.get("MIGRATE_LIMIT", "0"))
CAP = 1020
SYN_CAP = 4000
SOURCE_CHANNEL_KEY = "3753299714"
POSTER_BACK_SCAN = 4


def clean_caption_db(text):
    if not text:
        return text
    text = text.replace('-', '')
    text = re.sub(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+', ' ', text)
    text = re.sub(r'[^A-Za-z0-9\s]', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def split_poster_caption(cap):
    cap = (cap or "").strip()
    if not cap:
        return "", ""
    lines = [ln.strip() for ln in cap.split("\n") if ln.strip()]
    if not lines:
        return "", cap
    name = clean_caption_db(lines[0])
    syn = "\n".join(lines[1:]).strip()
    if not name:
        name = "Movie"
    return name, syn


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_progress(done):
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)
    os.replace(tmp, PROGRESS_FILE)


async def with_flood(fn):
    for attempt in range(30):
        try:
            return await fn()
        except TelegramError as e:
            low = str(e).lower()
            if "flood" in low or "retry" in low:
                wait = getattr(e, "retry_after", None) or 10 * (attempt + 1)
                print(f"  flood, waiting {wait}s: {e}", flush=True)
                await asyncio.sleep(wait)
            else:
                raise


async def fetch_poster(bot, poster):
    """Probe candidate poster ids back from `poster` via scratch chat, read the
    photo caption, delete all probes. Returns (poster_id, name, synopsis)."""
    probes = []
    best = None
    for pid in range(poster, poster - POSTER_BACK_SCAN - 1, -1):
        try:
            m = await with_flood(lambda: bot.forward_message(
                chat_id=SCRATCH, from_chat_id=SRC, message_id=pid))
            probes.append(m.message_id)
            if m.photo and (m.caption or "").strip() and best is None:
                best = (pid, m.caption)
        except TelegramError:
            continue
    clean_probes = []
    for pmid in probes:
        try:
            await bot.delete_message(chat_id=SCRATCH, message_id=pmid)
            clean_probes.append(pmid)
        except TelegramError:
            pass
    if best is None:
        return poster, "", ""
    pid, cap = best
    name, syn = split_poster_caption(cap)
    return pid, name, syn


def main():
    if not TOKEN:
        print("TELEGRAM_TOKEN env is required")
        sys.exit(1)

    with open(JSON_FILE, encoding="utf-8") as f:
        entries = json.load(f)

    groups = {}
    for x in entries:
        if x.get("channel") != SOURCE_CHANNEL_KEY:
            continue
        groups.setdefault(x.get("caption", ""), []).append(x["message_id"])
    print(f"Source channel {SOURCE_CHANNEL_KEY}: {len(groups)} groups, "
          f"{sum(len(v) for v in groups.values())} video messages", flush=True)

    done = load_progress()
    todo = [(cap, sorted(mids)) for cap, mids in groups.items() if cap not in done]
    todo.sort(key=lambda t: t[1][0])
    if LIMIT:
        todo = todo[:LIMIT]
    need = len(groups) - len(todo)
    print(f"Skipping {need} done groups, processing {len(todo)} groups", flush=True)

    if DRY:
        for cap, mids in todo[:20]:
            poster = mids[0] - 1
            print(f"  dry: poster-candidates=#{poster - POSTER_BACK_SCAN}..#{poster} "
                  f"videos={mids[:4]} cap='{cap[:40]}'", flush=True)
        print(f"DRY_RUN done ({min(20, len(todo))} shown).", flush=True)
        return

    if not DST or DST == 0:
        print("DATABASE_CHANNEL_ID (or DB_CHANNEL) env is required for real run")
        sys.exit(1)

    logger = open("migrate.log", "a", encoding="utf-8")

    async def run():
        bot = Bot(TOKEN)
        ok = fail = 0
        for i, (cap, mids) in enumerate(todo, 1):
            poster = mids[0] - 1
            try:
                p_pid, name, syn = await fetch_poster(bot, poster)
            except Exception as e:
                print(f"  poster probe failed #{poster}: {e}", flush=True)
                name, syn = split_poster_caption(cap)
                p_pid = poster

            try:
                await with_flood(lambda: bot.copy_message(
                    chat_id=DST, from_chat_id=SRC, message_id=p_pid, caption=name))
            except TelegramError as e:
                logger.write(f"POSTER_FAIL {poster} :: {e}\n"); logger.flush()
                fail += 1
                continue
            await asyncio.sleep(2)

            if syn:
                if len(syn) > SYN_CAP:
                    syn = syn[: SYN_CAP - 3].rstrip() + "..."
                try:
                    await with_flood(lambda: bot.send_message(chat_id=DST, text=syn))
                except TelegramError as e:
                    logger.write(f"SYN_FAIL {poster} :: {e}\n"); logger.flush()
                await asyncio.sleep(2)

            for mid in mids:
                vcap = clean_caption_db(cap)
                if not vcap:
                    vcap = name or "Movie"
                if len(vcap) > CAP:
                    vcap = vcap[: CAP - 3].rstrip() + "..."
                try:
                    await with_flood(lambda: bot.copy_message(
                        chat_id=DST, from_chat_id=SRC, message_id=mid, caption=vcap))
                except TelegramError as e:
                    logger.write(f"VID_FAIL {mid} {cap[:40]} :: {e}\n"); logger.flush()
                await asyncio.sleep(2)

            ok += 1
            done.add(cap)
            logger.write(f"OK {cap[:60]} poster#{p_pid} videos={mids[:4]}\n"); logger.flush()

            if i % 10 == 0:
                save_progress(done)
            if LIMIT and i >= LIMIT:
                break
            print(f"progress {i}/{len(todo)} ok={ok} fail={fail}", flush=True)

        save_progress(done)
        logger.close()
        print(f"DONE ok={ok} fail={fail}", flush=True)

    asyncio.run(run())


main()