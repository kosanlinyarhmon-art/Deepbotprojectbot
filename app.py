import os
import asyncio
import threading
import json
import logging
import sys
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

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

# ---------- Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
OTHER_CHANNELS = [link for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link] if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(id) for id in os.environ.get("ADMIN_ID", "").split(",") if id] if os.environ.get("ADMIN_ID") else []

DB_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": [], "total_requests": 0}

def save_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

maintenance_mode = False

# ---------- User Callback (ခလုတ်နှိပ်ရင် Download Link ထုတ်ပေးမယ်) ----------
async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if maintenance_mode:
        await query.edit_message_text("⚠️ Bot သည် ပြုပြင်ထိန်းသိမ်းမှုမုဒ်တွင် ရှိပါသည်။")
        return

    # Channel member ဟုတ်မဟုတ် စစ်ဆေးပါ
    if not await is_member(user_id, context):
        await query.edit_message_text(
            f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 [Channel သို့ဝင်ရန်]({INVITE_LINK})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    # အသုံးပြုသူ စာရင်းသွင်းပါ
    data = load_data()
    if user_id not in data["users"]:
        data["users"].append(user_id)
    data["total_requests"] += 1
    save_data(data)

    await query.edit_message_text("✅ ဇာတ်ကား Download Link ပြင်ဆင်နေပါပြီ...⏳")

    # သိမ်းထားတဲ့ movie file ID ကို ပြန်ယူပါ
    movie_file_id = context.bot_data.get('current_movie_file')
    if not movie_file_id:
        await context.bot.send_message(chat_id=user_id, text="❌ ဇာတ်ကားဖိုင် မတွေ့ပါ။ နောက်မှ ထပ်စမ်းပါ။")
        return

    try:
        # File ID ကနေ file path ရယူပါ
        file_obj = await context.bot.get_file(movie_file_id)
        file_path = file_obj.file_path
        download_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

        # Link ကို စာသားအနေနဲ့ ပို့ပါ
        link_message = await context.bot.send_message(
            chat_id=user_id,
            text=f"🎬 **သင့်ဇာတ်ကား Download Link**\n\n🔗 [Click Here to Download]({download_link})\n\n⚠️ ဒီ Link သည် **၅ မိနစ်** အတွင်း သက်တမ်းကုန်မည်။ ချက်ချင်း Download လုပ်ပါ။",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

        # ၅ မိနစ်နောက် Link message ကို ဖျက်ပါ
        async def delete_link():
            await asyncio.sleep(300)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=link_message.message_id)
            except:
                pass
        asyncio.create_task(delete_link())

        # အခြား Channel ၂ ခု ဖိတ်ခေါ်ရန်
        if OTHER_CHANNELS:
            text = "🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**\n\n"
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                text += f"{idx}. [Channel {idx}]({link})\n"
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"❌ Download Link ထုတ်ရာတွင် အမှား: {str(e)}")

# ---------- /newpost Command (ပုံ + စာ + Video ၁ခု) ----------
POSTER, CAPTION, VIDEO = range(3)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ဇာတ်ကားအတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
    return POSTER

async def receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ကျေးဇူးပြု၍ ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား (ဖော်ပြချက်) ရေးပေးပါ...")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['caption'] = update.message.text
    await update.message.reply_text("🎬 ဇာတ်ကား Video File (တစ်ခုတည်း) ကို ပို့ပေးပါ...")
    return VIDEO

async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO
    # Video file ID ကို bot_data ထဲမှာ သိမ်းပါ (နောက်မှ callback သုံးဖို့)
    context.application.bot_data['current_movie_file'] = update.message.video.file_id

    poster = context.user_data['poster']
    caption_text = context.user_data['caption']

    # Button တည်ဆောက်ပါ
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", callback_data="get_movie")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Preview ပို့ပါ (ပုံ + စာ + ခလုတ်)
    await update.message.reply_photo(
        photo=poster,
        caption=caption_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ အဆင်သင့်ပါပြီ။ ဒီ Message ကို **Forward** လုပ်ပြီး သင့် Channel မှာ တင်လိုက်ပါ။")
    # အချက်အလက်များ ရှင်းပါ
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Admin Commands (မြန်မာလို) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 မင်္ဂလာပါ။\n"
        "ကျွန်ုပ်သည် Movie Bot ဖြစ်ပါသည်။\n"
        "Channel ထဲရှိ 'ဇာတ်ကားရယူရန်' ခလုတ်ကို နှိပ်၍ Download Link ရယူနိုင်ပါသည်။"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <message>")
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
    await update.message.reply_text("🔇 ပြုပြင်ထိန်းသိမ်းမုဒ် **ဖွင့်** ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = False
    await update.message.reply_text("🔊 ပြုပြင်ထိန်းသိမ်းမုဒ် **ပိတ်** ထားပါသည်။")

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⏳ အချိန်ဇယားသတ်မှတ်ရန် (လုပ်ဆောင်ဆဲ)")
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

# ---------- Main Application ----------
application = Application.builder().token(TOKEN).build()

# Conversation handler for /newpost
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO: [MessageHandler(filters.VIDEO, receive_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_conv)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)
application.add_handler(CallbackQueryHandler(movie_callback, pattern="get_movie"))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))

# ---------- Bot Polling Loop ----------
def run_bot():
    while True:
        try:
            logger.info("Starting bot polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10 seconds...")
            import time
            time.sleep(10)

# ---------- Flask Background ----------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port)

# ---------- Main ----------
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
