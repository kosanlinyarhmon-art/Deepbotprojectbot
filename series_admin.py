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

Posts in the channel were made by more than one bot token, and a bot can only
edit its own messages, so every call is tried with each configured token.
"""

import json
import re
import time
import urllib.error
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
_CREDIT = re.compile(
    r"(?i)\b(?:mm\s*sub|mmsub|mmsu|crd|credit|credits|myintmyatthu|"
    r"upload\s*by|encoded\s*by|powered\s*by|channel\s*x)\b"
)
_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# A bot can only touch its own messages; a different token often can.
_RETRY = ("message_id_invalid", "message to edit not found",
          "message can't be edited", "message to copy not found",
          "not enough rights", "chat not found", "message not found",
          "have no rights", "message_id_invalid")


def _api_one(token, method, params):
    body = json.dumps({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            desc = json.loads(raw).get("description") or raw
        except Exception:
            desc = raw
        raise RuntimeError(f"{method}: {desc}") from None
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('description')}")
    return out["result"]


class _Bots:
    """Try every configured bot token, because each bot owns its own posts."""

    def __init__(self, pairs):
        self.pairs = [(t, u) for t, u in pairs if t]
        self._username = ""

    def call_token(self, method, **params):
        """Return (result, token) — file ids only work with the same token."""
        last = None
        for token, _user in self.pairs:
            try:
                return _api_one(token, method, params), token
            except Exception as exc:
                last = exc
                low = str(exc).lower()
                if any(marker in low for marker in _RETRY):
                    continue
                raise
        raise last or RuntimeError("no bot token configured")

    def call(self, method, **params):
        return self.call_token(method, **params)[0]

    def username(self):
        if not self._username:
            self._username = (self.pairs[0][1] if self.pairs else "") or \
                (_api_one(self.pairs[0][0], "getMe", {}).get("username") or "")
        return self._username


def _clean_title(raw):
    t = _ZW.sub("", raw or "").strip()
    t = _CREDIT.sub(" ", t)
    t = _TITLE_MARK.sub(" ", t)
    t = _NOISE.sub(" ", t)
    t = re.sub(r"[\(\)\[\]]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = t.strip(" -.,·|")
    # Titles copied from file names are all lower case ("wizard",
    # "disenchanment"); give them the capitalisation a hand-written post has.
    if t and not re.search(r"[A-Z]", t):
        small = {"a", "an", "and", "the", "of", "to", "in", "on", "for", "at"}
        words = t.split()
        t = " ".join(w if w in small and i else w[:1].upper() + w[1:]
                     for i, w in enumerate(words))
    return t


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


def register(app, *, tokens, mongo_db, channel_id, db_channel_id,
             other_channels, music_link, admin_key):
    """Attach /admin/series to the Flask app."""
    migrated = mongo_db["migrated_posts"]
    bots = _Bots(tokens)

    def files_of(payload):
        doc = mongo_db["file_store"].find_one({"payload": payload})
        return (doc or {}).get("files", []) or []

    def is_modified(exc):
        return "not modified" in str(exc).lower()

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
            "series": _files_have_episodes(files)
                      or len(doc.get("seasons") or []) > 1,
            "poster": bool(doc.get("poster_file_id") or doc.get("db_post_id")),
            "synopsis_len": len(doc.get("synopsis") or ""),
            "sample": [f.get("file_name") or "" for f in files[:4]],
        }
        if with_caption:
            item["new_title"] = _range_title(doc, files)
            item["new_caption"] = _caption(doc, files)
        return item

    def fix_photo(doc):
        """The post exists and is the only post: edit caption + buttons."""
        payload = doc.get("payload")
        files = files_of(payload) if payload else []
        caption = _caption(doc, files)
        markup = _keyboard(payload, bots.username(), other_channels, music_link)
        pid = int(doc["post_id"])
        shown = {}
        try:
            shown = bots.call("editMessageCaption", chat_id=channel_id,
                              message_id=pid, caption=caption) or {}
        except Exception as exc:
            if not is_modified(exc):
                return {"id": str(doc["_id"]), "ok": False,
                        "why": f"caption: {exc}"}
        if isinstance(shown, dict) and shown and not shown.get("photo"):
            kinds = [k for k in ("video", "document", "animation", "audio",
                                 "text") if shown.get(k)]
            return {"id": str(doc["_id"]), "ok": False,
                    "why": "post is not a photo (" + (",".join(kinds) or "?") + ")"}
        try:
            bots.call("editMessageReplyMarkup", chat_id=channel_id,
                      message_id=pid, reply_markup=markup)
        except Exception as exc:
            if not is_modified(exc):
                return {"id": str(doc["_id"]), "ok": False,
                        "why": f"markup: {exc}"}
        migrated.update_one({"_id": doc["_id"]},
                            {"$set": {"button_id": pid, "caption": caption,
                                      "media_kind": "photo"}})
        return {"id": str(doc["_id"]), "ok": True, "post": pid,
                "edited": True, "title": _range_title(doc, files)}

    def rebuild(doc):
        """Recreate the post (keeps the media) then delete the old message."""
        payload = doc.get("payload")
        files = files_of(payload) if payload else []
        if not payload or not files:
            return {"id": str(doc["_id"]), "ok": False,
                    "why": "no payload/files"}
        caption = _caption(doc, files)
        markup = _keyboard(payload, bots.username(), other_channels, music_link)
        pid = int(doc["post_id"]) if doc.get("post_id") else None
        btn = int(doc["button_id"]) if doc.get("button_id") else None

        sources = []
        if pid:
            sources.append(("self", channel_id, pid))
        db_id = doc.get("db_post_id")
        if db_channel_id and db_id:
            sources.append(("db", int(str(db_channel_id).strip()), int(db_id)))

        # copyMessage only returns a MessageId in Bot API 7+, so the media type
        # is read from the message that editMessageCaption hands back. That also
        # proves the copy accepts a caption (i.e. it is not an album).
        new_id = None
        media = None
        notes = []
        for label, from_chat, msg_id in sources:
            try:
                msg = bots.call("copyMessage", chat_id=channel_id,
                                from_chat_id=from_chat, message_id=msg_id)
            except Exception as exc:
                notes.append(f"{label}: {exc}")
                continue
            mid = msg["message_id"]
            try:
                shown = bots.call("editMessageCaption", chat_id=channel_id,
                                  message_id=mid, caption=caption)
            except Exception as exc:
                if not is_modified(exc):
                    notes.append(f"{label} caption: {exc}")
                    try:
                        bots.call("deleteMessage", chat_id=channel_id,
                                  message_id=mid)
                    except Exception:
                        pass
                    continue
                shown = {}
            if isinstance(shown, dict) and shown.get("photo"):
                media = "photo"
            elif isinstance(shown, dict) and shown:
                kinds = [k for k in ("video", "document", "animation",
                                     "audio", "text") if shown.get(k)]
                notes.append(f"{label}: not a photo ({','.join(kinds) or '?'})")
                try:
                    bots.call("deleteMessage", chat_id=channel_id,
                              message_id=mid)
                except Exception:
                    pass
                continue
            else:
                media = media or "photo"
            new_id = mid
            break

        if new_id is None:
            return {"id": str(doc["_id"]), "ok": False,
                    "why": "could not recreate the post", "notes": notes}

        try:
            bots.call("editMessageReplyMarkup", chat_id=channel_id,
                      message_id=new_id, reply_markup=markup)
        except Exception as exc:
            if not is_modified(exc):
                try:
                    bots.call("deleteMessage", chat_id=channel_id,
                              message_id=new_id)
                except Exception:
                    pass
                return {"id": str(doc["_id"]), "ok": False,
                        "why": f"markup failed: {exc}"}

        deleted = []
        for old in (pid, btn):
            if old and old != new_id:
                try:
                    bots.call("deleteMessage", chat_id=channel_id,
                              message_id=old)
                    deleted.append(old)
                except Exception as exc:
                    app.logger.warning("series delete failed %s: %s", old, exc)

        migrated.update_one({"_id": doc["_id"]}, {"$set": {
            "post_id": new_id, "button_id": new_id,
            "caption": caption, "reposted_at": time.time(),
            **({"media_kind": media} if media else {})}})
        return {"id": str(doc["_id"]), "ok": True, "old": deleted,
                "new": new_id, "media": media,
                "title": _range_title(doc, files),
                "notes": notes}

    def fix_video_post(doc, photo_id=None):
        """Replace a rebuilt video post with a real photo (the video thumbnail).

        The migration used the video's own thumbnail as the poster image, so
        reusing that file id restores the picture in the poster slot without
        having to re-upload anything or guess at a file name.
        """
        pid = int(doc["post_id"])
        files = files_of(doc.get("payload")) if doc.get("payload") else []
        caption = _caption(doc, files)
        markup = _keyboard(doc.get("payload"), bots.username(),
                           other_channels, music_link)
        temp = {"inline_keyboard": markup["inline_keyboard"] + [[
            {"text": "🔄", "url": f"https://t.me/{bots.username()}"}]]}
        shown, token = bots.call_token(
            "editMessageReplyMarkup", chat_id=channel_id,
            message_id=pid, reply_markup=temp)
        if not isinstance(shown, dict):
            return {"id": str(doc["_id"]), "ok": False,
                    "why": "could not read the current post"}
        try:
            if shown.get("photo"):
                return {"id": str(doc["_id"]), "ok": True, "post": pid,
                        "already_photo": True}
            media = shown.get("video") or shown.get("document") or \
                shown.get("animation") or {}
            thumb = media.get("thumbnail") or media.get("thumb")
            source = photo_id or (thumb or {}).get("file_id")
            if not source:
                return {"id": str(doc["_id"]), "ok": False,
                        "why": "no poster photo available"}
            sent = _api_one(token, "sendPhoto", {
                "chat_id": channel_id, "photo": source,
                "caption": caption})
            new_id = sent["message_id"]
            if not sent.get("photo"):
                try:
                    bots.call("deleteMessage", chat_id=channel_id,
                              message_id=new_id)
                except Exception:
                    pass
                return {"id": str(doc["_id"]), "ok": False,
                        "why": "replacement is not a photo"}
            _api_one(token, "editMessageReplyMarkup", {
                "chat_id": channel_id, "message_id": new_id,
                "reply_markup": markup})
            bots.call("deleteMessage", chat_id=channel_id, message_id=pid)
            migrated.update_one({"_id": doc["_id"]}, {"$set": {
                "post_id": new_id, "button_id": new_id,
                "media_kind": "photo", "caption": caption,
                "reposted_at": time.time()}})
            return {"id": str(doc["_id"]), "ok": True, "old": pid,
                    "new": new_id, "title": _range_title(doc, files)}
        except Exception:
            # Never leave the temporary test button behind on the old post.
            try:
                bots.call("editMessageReplyMarkup", chat_id=channel_id,
                          message_id=pid, reply_markup=markup)
            except Exception:
                pass
            raise

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
                 "reposted_at": {"$exists": False},
                 "payload": {"$exists": True}}
        if after:
            query["post_id"] = {"$gt": after}
        series_only = request.args.get("mode", "") == "series"

        if action == "probe":
            """Look inside private DB-channel messages without changing them."""
            if request.args.get("apply") != "1":
                return jsonify({"ok": False, "error": "apply=1 required"})
            from_chat = int(request.args.get("chat") or
                            str(db_channel_id or "").strip() or channel_id)
            ids = [int(x) for x in request.args.get("ids", "").split(",")
                   if x.strip().isdigit()]
            out = []
            for mid in ids:
                rec = {"id": mid}
                try:
                    cp = bots.call("copyMessage", chat_id=from_chat,
                                   from_chat_id=from_chat, message_id=mid)
                    new = cp["message_id"]
                except Exception as exc:
                    rec["error"] = str(exc)[:120]
                    out.append(rec)
                    continue
                try:
                    shown = bots.call("editMessageCaption", chat_id=from_chat,
                                      message_id=new,
                                      caption=f"probe {mid} {time.time()}")
                    rec["media"] = ("photo" if shown.get("photo") else
                                    ",".join(k for k in ("video", "document",
                                                         "animation", "audio",
                                                         "text") if shown.get(k))
                                    or "?")
                    if shown.get("photo"):
                        rec["photo_file_id"] = shown["photo"][-1]["file_id"]
                except Exception as exc:
                    rec["error"] = str(exc)[:120]
                try:
                    bots.call("deleteMessage", chat_id=from_chat,
                              message_id=new)
                except Exception:
                    pass
                out.append(rec)
                time.sleep(0.3)
            return jsonify({"ok": True, "items": out})

        if action == "fixposter":
            """Replace rebuilt VIDEO posts with a photo in the poster slot."""
            if request.args.get("apply") != "1":
                q = {"post_id": {"$gt": int(after or 13559)},
                     "reposted_at": {"$exists": True}}
                docs = list(migrated.find(q).sort("post_id", 1).limit(limit))
                return jsonify({"ok": True, "dry_run": True,
                                "items": [{"id": str(d["_id"]),
                                           "post": d.get("post_id")}
                                          for d in docs]})
            q = {"post_id": {"$gt": int(after or 13559)},
                 "reposted_at": {"$exists": True}}
            docs = list(migrated.find(q).sort("post_id", 1).limit(limit))
            results = []
            photo_override = request.args.get("photo")
            for d in docs:
                try:
                    results.append(fix_video_post(d, photo_override))
                except Exception as exc:
                    results.append({"id": str(d["_id"]), "ok": False,
                                    "why": str(exc)[:200]})
                time.sleep(0.6)
            return jsonify({"ok": True, "done": len(results),
                            "results": results})

        if action == "inspect":
            """Prove one post really has a poster AND a bottom deep-link button."""
            pid = int(request.args.get("post") or 0)
            doc = migrated.find_one({"post_id": pid}) or \
                migrated.find_one({"post_id": str(pid)})
            if not doc:
                return jsonify({"ok": False, "error": "post not in DB"}), 404
            files = files_of(doc.get("payload")) if doc.get("payload") else []
            caption = _caption(doc, files)
            markup = _keyboard(doc.get("payload"), bots.username(),
                               other_channels, music_link)
            info = {"id": str(doc["_id"]), "post": pid,
                    "title": _range_title(doc, files),
                    "files": len(files),
                    "fields": {k: str(v)[:100] for k, v in doc.items()
                               if k in ("poster_file_id", "db_post_id",
                                        "payload", "media_kind", "src",
                                        "series_key")}}
            try:
                shown = bots.call("editMessageCaption", chat_id=channel_id,
                                  message_id=pid, caption=caption)
                info["media"] = ("photo" if shown.get("photo") else
                                 ",".join(k for k in ("video", "document",
                                                      "animation", "audio")
                                          if shown.get(k)) or "?")
                info["caption"] = (shown.get("caption") or "")[:120]
            except Exception as exc:
                info["caption_error"] = str(exc)
            if request.args.get("apply") == "1":
                # Two edits force Telegram to hand back the whole message both
                # times, so the buttons can actually be read, not assumed.
                temp = {"inline_keyboard": markup["inline_keyboard"] + [[
                    {"text": "🔄", "url": f"https://t.me/{bots.username()}"}]]}
                try:
                    a = bots.call("editMessageReplyMarkup", chat_id=channel_id,
                                  message_id=pid, reply_markup=temp)
                    b = bots.call("editMessageReplyMarkup", chat_id=channel_id,
                                  message_id=pid, reply_markup=markup)
                    def rows(msg):
                        if not isinstance(msg, dict):
                            return "not modified"
                        return [[bt.get("text") for bt in row] for row in
                                (msg.get("reply_markup") or {}).get("inline_keyboard", [])]
                    info["buttons_before"] = rows(a)
                    info["buttons_after"] = rows(b)
                    info["poster_after"] = bool((b or {}).get("photo")) \
                        if isinstance(b, dict) else None
                except Exception as exc:
                    info["markup_error"] = str(exc)
            return jsonify({"ok": True, "info": info})

        if action == "summary":
            items = [summarise(d, with_caption=False)
                     for d in migrated.find(query)]
            if series_only:
                items = [i for i in items if i["series"]]
            return jsonify({
                "ok": True,
                "total": len(items),
                "real_series": sum(1 for i in items if i["series"]),
                "no_button": sum(1 for i in items if not i["button_id"]),
                "split_post": sum(1 for i in items if i["button_id"]
                                  and i["post_id"]
                                  and int(i["button_id"]) != int(i["post_id"])),
                "no_poster": sum(1 for i in items if not i["poster"]),
                "no_synopsis": sum(1 for i in items if not i["synopsis_len"]),
                "items": items[:limit],
            })

        if action == "report":
            docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
            items = [summarise(d) for d in docs]
            if series_only:
                items = [i for i in items if i["series"]]
            return jsonify({"ok": True, "count": len(items), "items": items})

        if action != "fix":
            return jsonify({"ok": False, "error": "unknown action"}), 400

        if request.args.get("apply") != "1":
            docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
            items = [summarise(d) for d in docs]
            if series_only:
                items = [i for i in items if i["series"]]
            return jsonify({"ok": True, "dry_run": True,
                            "next_after": docs[-1].get("post_id") if docs else None,
                            "items": items})

        docs = list(migrated.find(query).sort("post_id", 1).limit(limit))
        results = []
        for d in docs:
            pid = d.get("post_id")
            btn = d.get("button_id")
            if series_only and not summarise(d, False)["series"]:
                continue
            if only_bad and pid and btn and int(btn) == int(pid) \
                    and d.get("media_kind") == "photo":
                continue
            try:
                if pid and btn and int(btn) == int(pid):
                    res = fix_photo(d)
                    if not res.get("ok"):
                        err = res.get("why")
                        res = rebuild(d)
                        res["fix_error"] = err
                    results.append(res)
                else:
                    results.append(rebuild(d))
            except Exception as exc:
                results.append({"id": str(d["_id"]), "ok": False,
                                "why": str(exc)[:200]})
            time.sleep(0.5)
        return jsonify({"ok": True, "done": len(results),
                        "scanned": len(docs),
                        "next_after": docs[-1].get("post_id") if docs else None,
                        "results": results})

    return app
