import os
import asyncio
import threading
import logging
import sys
import secrets
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient

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
        users_collection.insert_one({"user_id": user_id})

def get_all_users():
    return [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]

def save_file_info(payload, file_id, file_name):
    file_store_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        return {"file_id": doc["file_id"], "file_name": doc["file_name"]}
    return None

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
OTHER_CHANNELS = [link.strip() for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link.strip()] if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_ID", "").split(",") if id.strip()] if os.environ.get("ADMIN_ID") else []

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

def generate_payload():
    return secrets.token_urlsafe(16)

async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ---------- Start & Deep Link Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        file_info = get_file_info(payload)

        if file_info:
            file_id = file_info["file_id"]
            file_name = file_info["file_name"]

            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 Channel သို့ဝင်ရန်: {INVITE_LINK}",
                    disable_web_page_preview=True
                )
                return

            try:
                await update.message.reply_text(f"🎬 {file_name} ပို့ပေးနေပါပြီ...")
                video_msg = await context.bot.send_video(
                    chat_id=user_id,
                    video=file_id,
                    caption=f"🎬 သင့်ဇာတ်ကား - {file_name}"
                )

                warning_text = (
                    "⚠️ ⚠️ ⚠️ အရေးကြီးပါတယ် ⚠️ ⚠️ ⚠️\n\n"
                    "ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n\n"
                    "ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n\n"
                    "ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n\n"
                    "Channel ရေရှည်တည်တံ့ဖို့အတွက် Support ပေးချင်ပါက Wave Pay (09767011991) ကို ကူညီနိုင်ပါတယ်။\n\n"
                    "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n"
                    "!!! IMPORTANT !!!\n"
                    "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
                    "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
                )
                warn_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=warning_text
                )

                async def delete_after():
                    await asyncio.sleep(300)
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                        await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                    except:
                        pass
                asyncio.create_task(delete_after())

                add_user(user_id)
                increment_requests()

                if OTHER_CHANNELS:
                    keyboard = []
                    if len(OTHER_CHANNELS) >= 1:
                        keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=OTHER_CHANNELS[0])])
                    if len(OTHER_CHANNELS) >= 2:
                        keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=OTHER_CHANNELS[1])])
                    if keyboard:
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
                            reply_markup=reply_markup,
                            parse_mode="Markdown"
                        )
            except Exception as e:
                await context.bot.send_message(chat_id=user_id, text=f"❌ Video ပို့ရာတွင် အမှား: {str(e)}")
        else:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
    else:
        if is_admin(user_id):
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ Admin**\n\n"
                "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n\n"
                "/newpost - 🆕 ပို့စ်အသစ်ဖန်တီးရန် (ပုံ + စာ + Video)\n"
                "/link - 🔗 Video ပို့ပါက Deep Link ထုတ်ပေးမည်\n"
                "/stats - 📊 စာရင်းအင်းကြည့်ရန်\n"
                "/broadcast - 📢 အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
                "/mute - 🔇 Maintenance mode ဖွင့်ရန်\n"
                "/unmute - 🔊 Maintenance mode ပိတ်ရန်",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel အတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။",
                parse_mode="Markdown"
            )

# ---------- /link Command ----------
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 Video file တစ်ခု ပို့ပေးပါ။")
    context.user_data['waiting_for_video_link'] = True

async def handle_video_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if context.user_data.get('waiting_for_video_link'):
        video = update.message.video
        if video:
            try:
                payload = generate_payload()
                file_name = video.file_name or "ဇာတ်ကား"
                save_file_info(payload, video.file_id, file_name)
                deep_link = create_deep_linked_url(BOT_USERNAME, payload)

                await update.message.reply_text(
                    f"🔗 သင်၏ Deep Link\n\n"
                    f"{deep_link}\n\n"
                    f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် {file_name} ကို ချက်ချင်းရရှိမည်။\n"
                    f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
            context.user_data.pop('waiting_for_video_link', None)
        else:
            await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")

# ---------- /newpost Command ----------
POSTER, CAPTION, VIDEO_FILE = range(3)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကားအတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
    return POSTER

async def receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား ရေးပေးပါ... (စာလုံးရေ ကန့်သတ်ချက်မရှိ၊ Telegram Premium အပြည့်အဝထောက်ပံ့သည်)")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No length check anymore - let Telegram API handle it
    context.user_data['caption'] = update.message.text
    await update.message.reply_text("🎬 Video File ကို ပို့ပေးပါ...")
    return VIDEO_FILE

async def receive_video_for_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith('video/'):
        video = update.message.document

    if not video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ (video file သို့မဟုတ် video document)။")
        return VIDEO_FILE

    try:
        file_name = getattr(video, 'file_name', None)
        if not file_name:
            file_name = "ဇာတ်ကား"

        payload = generate_payload()
        save_file_info(payload, video.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)
        button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)
        reply_markup = InlineKeyboardMarkup([[button]])

        poster = context.user_data.get('poster')
        caption_text = context.user_data.get('caption')

        if not poster or not caption_text:
            await update.message.reply_text("ပုံ သို့မဟုတ် စာသား မှားယွင်းနေပါသည်။ /newpost ကို ထပ်မံစတင်ပါ။")
            return ConversationHandler.END

        # Try to send photo with caption (may fail if caption too long, but we let it try)
        await update.message.reply_photo(
            photo=poster,
            caption=caption_text,
            reply_markup=reply_markup
        )
        await update.message.reply_text("✅ အဆင်သင့်ပါပြီ။ ဒီ Message ကို Forward လုပ်ပြီး Channel မှာ တင်လိုက်ပါ။")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Post ဖန်တီးရာတွင် အမှား: {str(e)}")
        return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Other Admin Commands ----------
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

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_requests = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_requests}", parse_mode="Markdown")

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

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ အချိန်ဇယား (လုပ်ဆောင်ဆဲ)")

async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📋 အချိန်ဇယားစာရင်း (လုပ်ဆောင်ဆဲ)")

async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("❌ အချိန်ဇယားဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🗑️ ဖိုင်ဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚠️ အားလုံးဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO_FILE: [
            MessageHandler(filters.VIDEO, receive_video_for_post),
            MessageHandler(filters.Document.VIDEO, receive_video_for_post)
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_conv)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)
application.add_handler(CommandHandler("link", link_command))
application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_link))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))

# ---------- Polling (FIXED for Python 3.14) ----------
def run_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("Starting bot polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10s")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
