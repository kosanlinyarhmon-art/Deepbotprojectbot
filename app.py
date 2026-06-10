import os
import asyncio
import threading
import json
import logging
import sys
import secrets
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.helpers import create_deep_linked_url

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
    return "File Share Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
OTHER_CHANNELS = [link for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link] if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(id) for id in os.environ.get("ADMIN_ID", "").split(",") if id] if os.environ.get("ADMIN_ID") else []

DB_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": [], "total_requests": 0, "file_store": {}}

def save_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Member check failed: {e}")
        return False

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

def generate_payload():
    return secrets.token_urlsafe(16)

# ---------- Start Command (Admin only sees menu) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text(
            "🎬 မင်္ဂလာပါ Admin။\n\n"
            "အောက်ပါ Command များကို သုံးနိုင်ပါသည်။\n"
            "/network - Channel Post အသစ်ဖန်တီးရန် (ပုံ + စာ + Video)\n"
            "/link - Video ပို့ပါက Download Link ထုတ်ပေးမည်\n"
            "/stats - စာရင်းအင်းကြည့်ရန်\n"
            "/broadcast <message> - အသုံးပြုသူအားလုံးကို စာပို့ရန်\n"
            "/mute - Maintenance mode ဖွင့်ရန်\n"
            "/unmute - Maintenance mode ပိတ်ရန်"
        )
    else:
        await update.message.reply_text(
            "🎬 မင်္ဂလာပါ။\n"
            "ဤ Bot သည် Admin မှ Channel အတွက် Post များဖန်တီးရန် သုံးပါသည်။\n"
            "အကူအညီလိုပါက Admin ကို ဆက်သွယ်ပါ။"
        )

# ---------- Handle Deep Link (from channel post button) ----------
async def deep_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # If there is a payload
    if context.args and len(context.args) > 0:
        payload = context.args[0]
        logger.info(f"Deep link clicked by {user_id} with payload: {payload}")
        data = load_data()
        file_id = data["file_store"].get(payload)
        if file_id:
            # Check membership
            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 [Channel သို့ဝင်ရန်]({INVITE_LINK})",
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                return
            # Send download link
            try:
                file_obj = await context.bot.get_file(file_id)
                file_path = file_obj.file_path
                download_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                await update.message.reply_text(
                    f"🎬 **သင့်ဇာတ်ကား Download Link**\n\n"
                    f"🔗 [Click Here to Download]({download_link})\n\n"
                    f"⚠️ ဒီ Link သည် **၅ မိနစ်** အတွင်း သက်တမ်းကုန်မည်။ ချက်ချင်း Download လုပ်ပါ။",
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                # Update stats
                if user_id not in data["users"]:
                    data["users"].append(user_id)
                data["total_requests"] += 1
                save_data(data)
                # Invite other channels with buttons
                if OTHER_CHANNELS:
                    keyboard = []
                    if len(OTHER_CHANNELS) >= 1:
                        keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=OTHER_CHANNELS[0])])
                    if len(OTHER_CHANNELS) >= 2:
                        keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=OTHER_CHANNELS[1])])
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(
                        "🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Error sending download link: {e}")
                await update.message.reply_text(f"❌ Download link ထုတ်ရာတွင် အမှားရှိသည်။ ကျေးဇူးပြု၍ နောက်မှထပ်စမ်းပါ။\nError: {str(e)}")
        else:
            logger.warning(f"Invalid payload: {payload}")
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
    else:
        # Normal /start without payload
        await start(update, context)

# ---------- /link Command: Admin sends video, bot replies with download link ----------
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await update.message.reply_text("📤 Video file တစ်ခု ပို့ပေးပါ။")
    context.user_data['waiting_for_video'] = True

async def handle_video_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('waiting_for_video'):
        video = update.message.video
        if video:
            try:
                file_id = video.file_id
                file_obj = await context.bot.get_file(file_id)
                file_path = file_obj.file_path
                download_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                await update.message.reply_text(
                    f"🔗 **သင်၏ Download Link**\n\n`{download_link}`\n\nဤလင့်ကို ကူးယူ၍ အသုံးပြုနိုင်ပါသည်။",
                    parse_mode="Markdown"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Link ထုတ်ရာတွင် အမှား: {e}")
            finally:
                context.user_data.pop('waiting_for_video', None)
        else:
            await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")

# ---------- /network Command (formerly /newpost) ----------
POSTER, CAPTION, VIDEO_FILE = range(3)

async def network_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား ရေးပေးပါ...")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['caption'] = update.message.text
    await update.message.reply_text("🎬 Video File ကို ပို့ပေးပါ...")
    return VIDEO_FILE

async def receive_video_for_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO_FILE
    video = update.message.video
    payload = generate_payload()
    data = load_data()
    data["file_store"][payload] = video.file_id
    save_data(data)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)
    reply_markup = InlineKeyboardMarkup([[button]])
    poster = context.user_data['poster']
    caption_text = context.user_data['caption']
    await update.message.reply_photo(
        photo=poster,
        caption=caption_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ အဆင်သင့်ပါပြီ။ ဒီ Message ကို **Forward** လုပ်ပြီး Channel မှာ တင်လိုက်ပါ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Admin Commands ----------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <message> - ပြန်လွှင့်ရန် စာသားထည့်ပါ။")
        return
    data = load_data()
    count = 0
    for uid in data["users"]:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပြန်လွှင့်ခြင်း ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    data = load_data()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {len(data['users'])}\n🎬 တောင်းဆိုမှုအရေအတွက်: {data['total_requests']}")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode **ဖွင့်** ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode **ပိတ်** ထားပါသည်။")

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⏳ အချိန်ဇယား (လုပ်ဆောင်ဆဲ)")
async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("📋 အချိန်ဇယားစာရင်း (လုပ်ဆောင်ဆဲ)")
async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("❌ အချိန်ဇယားဖျက်ရန် (လုပ်ဆောင်ဆဲ)")
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("🗑️ ဖိုင်ဖျက်ရန် (လုပ်ဆောင်ဆဲ)")
async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⚠️ အားလုံးဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# Conversation for /network
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('network', network_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO_FILE: [MessageHandler(filters.VIDEO, receive_video_for_post)],
    },
    fallbacks=[CommandHandler('cancel', cancel_conv)],
)

application.add_handler(CommandHandler("start", deep_link_handler))  # Handles both deep link and normal start
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

# ---------- Polling ----------
def run_bot():
    while True:
        try:
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
