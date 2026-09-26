import asyncio, os, re, sys
from telegram import Bot
from telegram.error import TelegramError
from pymongo import MongoClient

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
DST = int(os.environ.get("DATABASE_CHANNEL_ID", os.environ.get("DB_CHANNEL", "0")))
PROGRESS_FILE = os.environ.get("MIGRATE_MONGO_PROGRESS", "migrate_mongo_progress.json")
DRY = os.environ.get("DRY_RUN", "0") == "1"
LIMIT = int(os.environ.get("MIGRATE_LIMIT", "0"))
CAP = 1020
DB_NAME = "telegram_bot"
COLLECTION = "file_store"


def clean_caption_db(text):
    if not text:
        return text
    text = text.replace('-', '')
    text = re.sub(r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+', ' ', text)
    text = re.sub(r'[^A-Za-z0-9\s]', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


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


def main():
    import json

    if not TOKEN:
        print("TELEGRAM_TOKEN env is required")
        sys.exit(1)
    if not MONGO_URI:
        print("MONGO_URI env is required")
        sys.exit(1)

    mongo = MongoClient(MONGO_URI)
    coll = mongo[DB_NAME][COLLECTION]

    # flatten: each doc = {"payload": ..., "files": [{file_id, file_name, file_caption}]}
    docs = list(coll.find({}))
    print(f"file_store documents: {len(docs)}", flush=True)

    payloads = []
    for doc in docs:
        files = doc.get("files")
        if not files:
            continue
        payload = doc.get("payload", "")
        payloads.append({"payload": payload, "files": files})
    payloads.sort(key=lambda p: p["payload"])

    if DRY:
        total = 0
        for p in payloads:
            total += len(p["files"])
        print(f"DRY_RUN: {len(payloads)} payloads, {total} files would be sent to DST", flush=True)
        for p in payloads[:10]:
            for f in p["files"][:4]:
                print(f"  dry: file_id={f.get('file_id','')[:22]}... "
                      f"name='{f.get('file_name','')[:40]}' cap='{(f.get('file_caption') or '')[:40]}'",
                      flush=True)
        return

    if not DST or DST == 0:
        print("DATABASE_CHANNEL_ID (or DB_CHANNEL) env is required for real run")
        sys.exit(1)

    done = load_progress()
    todo = [p for p in payloads if p["payload"] not in done]
    if LIMIT:
        todo = todo[:LIMIT]
    print(f"Skipping {len(payloads) - len(todo)} done payloads, "
          f"processing {len(todo)} payloads", flush=True)

    logger = open("migrate_mongo.log", "a", encoding="utf-8")

    async def run():
        bot = Bot(TOKEN)
        ok = fail = 0
        for i, p in enumerate(todo, 1):
            payload = p["payload"]
            for f in p["files"]:
                cap = clean_caption_db(f.get("file_caption") or (f.get("file_name") or "Movie"))
                if not cap:
                    cap = "Movie"
                if len(cap) > CAP:
                    cap = cap[: CAP - 3].rstrip() + "..."
                try:
                    await with_flood(lambda: bot.send_document(
                        chat_id=DST,
                        document=f["file_id"],
                        filename=f.get("file_name"),
                        caption=cap,
                    ))
                except TelegramError as e:
                    logger.write(f"FAIL {payload} :: {e}\n"); logger.flush()
                    fail += 1
                await asyncio.sleep(2)
            ok += 1
            done.add(payload)
            logger.write(f"OK {payload} files={len(p['files'])}\n"); logger.flush()

            if i % 10 == 0:
                save_progress(done)
            if LIMIT and i >= LIMIT:
                break
            print(f"progress {i}/{len(todo)} payloads_ok={ok} fail={fail}", flush=True)

        save_progress(done)
        logger.close()
        print(f"DONE payloads_ok={ok} fail={fail}", flush=True)

    asyncio.run(run())


main()