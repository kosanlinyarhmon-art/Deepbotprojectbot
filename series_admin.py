"""Admin-only series repair.

Old series were published as several posts (one per episode), sometimes with a
video instead of a poster, and often with no deep-link button.  This module
rebuilds them in the format the admin now posts by hand with /newpost:

    <Title S 1 to 8>

    📝 ဇာတ်ကားအကြောင်း

    <synopsis>

ONE post, ONE poster, ONE deep link that holds every file.

The report is read-only.  Nothing is changed without ``apply=1`` and nothing is
deleted until the replacement post exists and has its buttons.
"""

import json
import re
import time
import urllib.request

from flask import jsonify, request


_EP_MARK = re.compile(
    r"(?i)\b(?:s\s*\d{1,2}\s*[-_. ]?\s*e\s*p?\s*\d{1,3}"
    r"|s\s*\.?\s*\d{1,2}\b"
    r"|ep\s*\.?\s*\d{1,3}\b"
    r"|episode\s*\.?\s*\d{1,3}\b"
    r"|season\s*\.?\s*\d{1,2}\b)"
)
_TITLE_MARK = re.compile(
    r"(?i)\s*[\(\[]?\s*\bs\s*\.?\s*-?\s*\d{1,2}\s*(?:to|-|~|–)\s*\d{1,2}\s*[\)\]]?"
    r"|\s*[\(\[]?\s*\bs\s*\.?\s*-?\s*\d{1,2}\s*[\)\]]?"
    r"|\s*[\(\[]?\s*\b(?:ep|episode|e)\s*\.?\s*\d{1,3}(?:\s*[-–~]\s*\d{1,3})*\s*[\)\]]?"
)
_NOISE = re.compile(r"(?i)\b(?:complete|completed|end|final|full|all)\b")
_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _api(token, method, **params):
    body = json.dumps({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('description')}")
    return out["result"]


def _clean_title(raw):
    t = _ZW.sub("", raw or "").strip()
    t = _TITLE_MARK.sub(" ", t)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"[\(\)\[\]]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -.,·|")


def _clean_synopsis(raw):
    """Drop the leftover source lines that only repeat 'S.03 (Episode-22)'."""
    out = []
    for line in (raw or "").splitlines():
        ln = _ZW.sub("", line).strip()
        if not ln:
            continue
        if len(ln) <= 70 and _EP_MARK.search(ln):
            continue
        if re.fullmatch(r"(?i)[\s\S]{0,30}(?:upload by|crd|credit|mmsub|mm sub)[\s\S]{0,40}", ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _files_have_episodes(files):
    for f in files or []:
        text = f"{f.get('file_name') or ''} {f.get('file_caption') or ''}"
        if _EP_MARK.search(text):
            return True
    return False


def _range_title(doc, files):
    title = _clean_title(doc.get("title") or "") or "🎬 ဇာတ်ကားသစ်"
    seasons = sorted({int(x) for x in (doc.get("seasons") or []) if str(x).isdigit()})
    # Only trust the numbers when the files really are episodes; a movie split
    # into 50 parts must not become "S 1".
    if not seasons or not _files_have_episodes(files):
        return title
    if len(seasons) > 1:
        return f"{title} S {seasons[0]} to {seasons[-1]}"
    return f"{title} S {seasons[0]}"


def _caption(doc, files):
    title = _range_title(doc, files)
    syn = _clean_synopsis(doc.get("synopsis") or "")
    cap = f"{title}\n\n📝 ဇာတ်ကားအကြောင်း"
    if syn:
        cap += f"\n\n{syn}"
    return cap[:1024]


def _keyboard(payload, username, other_channels, music_link):
    url = f"https://t.me/{username}?start={payload}"
    rows = [[{"text": "🎬 ဇာတ်ကားရယူရန်", "url": url}]]
    labels = {1: "🎬 ဇာတ်ကားချန်နယ်", 2: "👥 လူကြီးချန်နယ်",
              3: "🎵 မြန်မာသီချင်းချန်နယ်"}
    for i, link in enumerate(other_channels or [], 1):
        rows.append([{"text": labels.get(i, f"Channel {i}"), "url": link}])
    if music_link:
        rows.append([{"text": "🎵 သီချင်း/တရားတော် 🙏", "url": music_link}])
    return {"inline_keyboard": rows}


def register(app, *, token, mongo_db, channel_id, db_channel_id,
             bot_username, other_channels, music_link, admin_key):
    """Attach /admin/series to the Flask app."""
    migrated = mongo_db["migrated_posts"]
    state = {"username": bot_username or ""}

    def files_of(payload):
        doc = mongo_db["file_store"].find_one({"payload": payload})
        return (doc or {}).get("files", []) or []

    def username():
        if not state["username"]:
            state["username"] = _api(token, "getMe").get("username") or ""
        return state["username"]

    def summarise(doc, with_caption=True):
        files = files_of(doc.get("payload")) if doc.get("payload") else []
        item = {
            "id": str(doc["_id"]),
            "title": doc.get("title") or "",
            "post_id": doc.get("post_id"),
            "button_id": doc.get("button_id"),
            "media": doc.get("media_kind") or "?",
            "seasons": doc.get("seasons"),
            "eps": doc.get("eps"),
            "files": len(files),
            "poster": bool(doc.get("poster_file_id") or doc.get("db_post_id")),
            "synopsis_len": len(doc.get("synopsis") or ""),
        }
        if with_caption:
            item["new_title"] = _range_title(doc, files)
            item["new_caption"] = _caption(doc, files)
        return item

    def rebuild(doc):
        payload = doc.get("payload")
        files = files_of(payload) if payload else []
        if not payload or not files:
            return {"id": str(doc["_id"]), "ok": False,
                    "why": "no payload/files"}
        caption = _caption(doc, files)
        markup = _keyboard(payload, username(), other_channels, music_link)
        pid = int(doc["post_id"]) if doc.get("post_id") else None
        btn = int(doc["button_id"]) if doc.get("button_id") else None

        new_id = None
        kind = None
        db_id = doc.get("db_post_id")
        if db_channel_id and db_id:
            try:
                msg = _api(token, "copyMessage", chat_id=channel_id,
                           from_chat_id=int(str(db_channel_id).strip()),
                           message_id=int(db_id))
                new_id = msg["message_id"]
                kind = "photo" if msg.get("photo") else "other"
            except Exception as exc:
                app.logger.warning("series copy failed %s: %s", db_id, exc)
        if new_id is None and doc.get("poster_file_id"):
            try:
                msg = _api(token, "sendPhoto", chat_id=channel_id,
                           photo=doc["poster_file_id"], caption=caption)
                new_id = msg["message_id"]
                kind = "photo"
            except Exception as exc:
                app.logger.warning("series sendPhoto failed: %s", exc)
        if new_id is None:
            f = next((x for x in files if x.get("file_id")), None)
            if not f:
                return {"id": str(doc["_id"]), "ok": False,
                        "why": "no poster and no file"}
            try:
                msg = _api(token, "sendDocument", chat_id=channel_id,
                           document=f["file_id"], caption=caption)
                new_id = msg["message_id"]
                kind = "other"
            except Exception as exc:
                return {"id": str(doc["_id"]), "ok": False,
                        "why": f"send failed: {exc}"}

        try:
            _api(token, "editMessageCaption", chat_id=channel_id,
                 message_id=new_id, caption=caption)
        except Exception as exc:
            app.logger.warning("series caption edit failed %s: %s", new_id, exc)
        try:
            _api(token, "editMessageReplyMarkup", chat_id=channel_id,
                 message_id=new_id, reply_markup=markup)
        except Exception as exc:
            app.logger.warning("series markup failed %s: %s", new_id, exc)
            try:
                _api(token, "deleteMessage", chat_id=channel_id,
                     message_id=new_id)
            except Exception:
                pass
            return {"id": str(doc["_id"]), "ok": False,
                    "why": f"markup failed: {exc}"}

        deleted = []
        for old in (pid, btn):
            if old and old != new_id:
                try:
                    _api(token, "deleteMessage", chat_id=channel_id,
                         message_id=old)
                    deleted.append(old)
                except Exception as exc:
                    app.logger.warning("series delete failed %s: %s", old, exc)

        migrated.update_one({"_id": doc["_id"]}, {"$set": {
            "post_id": new_id, "button_id": new_id, "media_kind": kind,
            "caption": caption, "reposted_at": time.time()}})
        return {"id": str(doc["_id"]), "ok": True, "old": deleted,
                "new": new_id, "media": kind, "title": _range_title(doc, files)}

    def fix_photo(doc):
        """Poster already correct: only the caption / buttons need fixing."""
        payload = doc.get("payload")
        files = files_of(payload) if payload else []
        caption = _caption(doc, files)
        markup = _keyboard(payload, username(), other_channels, music_link)
        pid = int(doc["post_id"])
        try:
            _api(token, "editMessageCaption", chat_id=channel_id,
                 message_id=pid, caption=caption)
            _api(token, "editMessageReplyMarkup", chat_id=channel_id,
                 message_id=pid, reply_markup=markup)
        except Exception as exc:
            return {"id": str(doc["_id"]), "ok": False, "why": str(exc)}
        migrated.update_one({"_id": doc["_id"]},
                            {"$set": {"button_id": pid, "caption": caption}})
        return {"id": str(doc["_id"]), "ok": True, "post": pid,
                "edited": True, "title": _range_title(doc, files)}

    @app.route("/admin/series")
    def _admin_series():
        if not admin_key or request.args.get("key") != admin_key:
            return jsonify({"ok": False, "error": "forbidden"}), 403
        action = request.args.get("action", "report")
        limit = max(1, min(int(request.args.get("limit", "20")), 50))
        after = int(request.args.get("after", "0") or 0)
        only_bad = request.args.get("only_bad", "0") == "1"

        query = {"series_key": {"$exists": True},
                 "merged_into": {"$exists": False},
                 "payload": {"$exists": True}}
        if after:
            query["post_id"] = {"$gt": after}

        if action == "report":
            docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
            return jsonify({"ok": True, "count": len(docs),
                            "items": [summarise(d) for d in docs]})

        if action != "fix":
            return jsonify({"ok": False, "error": "unknown action"}), 400

        if request.args.get("apply") != "1":
            docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
            return jsonify({"ok": True, "dry_run": True,
                            "items": [summarise(d) for d in docs]})

        docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
        results = []
        for d in docs:
            pid = d.get("post_id")
            btn = d.get("button_id")
            if only_bad and pid and btn and int(btn) == int(pid) \
                    and d.get("media_kind") == "photo":
                continue
            try:
                if d.get("media_kind") == "photo" and pid:
                    results.append(fix_photo(d))
                else:
                    results.append(rebuild(d))
            except Exception as exc:
                results.append({"id": str(d["_id"]), "ok": False,
                                "why": str(exc)[:200]})
            time.sleep(1.0)
        return jsonify({"ok": True, "done": len(results), "results": results})

    return app
