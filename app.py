import os
import asyncio
import threading
import json
import logging
import sys
from flask import Flask, request
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
    return {"users": [], "total_requests": 0, "schedules": []}

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

# ---------- User Callback ----------
async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if maintenance_mode:
        await query.edit_message_text("⚠️ Bot သည် ပြုပြင်ထိန်းသိမ်းမှုမုဒ်တွင် ရှိပါသည်။")
        return

    if not await is_member(user_id, context):
        await query.edit_message_text(
            f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 [Channel သို့ဝင်ရန်]({INVITE_LINK})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    data = load_data()
    if user_id not in data["users"]:
        data["users"].append(user_id)
    data["total_requests"] += 1
    save_data(data)

    await query.edit_message_text("✅ Member ဖြစ်ပါသည်။ ဇာတ်ကားများ ပို့ပေးနေပါပြီ...⏳")

    # Retrieve movie files from context.user_data
    movie_files = context.user_data.get('movie_files', [])
    if not movie_files:
        await context.bot.send_message(chat_id=user_id, text="❌ ဇာတ်ကားဖိုင်များ မတွေ့ပါ။ နောက်မှ ထပ်စမ်းပါ။")
        return

    try:
        sent_videos = []
        for file_id in movie_files:
            msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption="🎬 သင့်ဇာတ်ကား")
            sent_videos.append(msg.message_id)

        warn_msg = await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ **သတိပေးချက်**\n\nဤဇာတ်ကားများကို **၅ မိနစ်** အတွင်း ဖျက်ပါမည်။\nကျေးဇူးပြု၍ **Forward** လုပ်ပြီး သိမ်းထားပါ။",
            parse_mode="Markdown"
        )

        async def delete_after():
            await asyncio.sleep(300)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                for msg_id in sent_videos:
                    await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except:
                pass
        asyncio.create_task(delete_after())

        if OTHER_CHANNELS:
            text = "🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**\n\n"
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                text += f"{idx}. [Channel {idx}]({link})\n"
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"❌ ဇာတ်ကားပို့ရာတွင် အမှား: {str(e)}")

# ---------- /newpost Command with ConversationHandler ----------
POSTER, CAPTION, VIDEO1, VIDEO2, VIDEO3 = range(5)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("ဇာတ်ကားအတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
    return POSTER

async def receive_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ကျေးဇူးပြု၍ ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("ဇာတ်ကားအကြောင်း စာသား (ဖော်ပြချက်) ရေးပေးပါ...")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['caption'] = update.message.text
    await update.message.reply_text("Video File (movie part 1) ကို ပို့ပေးပါ...")
    return VIDEO1

async def receive_video1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO1
    context.user_data['movie_files'] = [update.message.video.file_id]
    await update.message.reply_text("Video File (movie part 2) ကို ပို့ပေးပါ...")
    return VIDEO2

async def receive_video2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO2
    context.user_data['movie_files'].append(update.message.video.file_id)
    await update.message.reply_text("Video File (movie part 3) ကို ပို့ပေးပါ...")
    return VIDEO3

async def receive_video3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO3
    context.user_data['movie_files'].append(update.message.video.file_id)

    # Build preview message
    poster = context.user_data['poster']
    caption_text = context.user_data['caption']
    movie_files = context.user_data['movie_files']

    # Create button
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", callback_data="get_movie")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send preview (photo with caption and button)
    await update.message.reply_photo(
        photo=poster,
        caption=caption_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ Preview message ကို forward လုပ်ပြီး Channel တွင် တင်နိုင်ပါပြီ။")
    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Admin Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 မင်္ဂလာပါ။\n"
        "ကျွန်ုပ်သည် Movie Bot ဖြစ်ပါသည်။\n"
        "Channel ထဲရှိ 'ဇာတ်ကားရယူရန်' ခလုတ်ကို နှိပ်၍ ရုပ်ရှင်များ ရယူနိုင်ပါသည်။"
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
    await update.message.reply_text("🔇 ပြုပြင်ထိန်းသိမ်းမုဒ် **ဖွင့်** ထားပါသည်။ အသုံးပြုသူများ ဇာတ်ကားမရယူနိုင်ပါ။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = False
    await update.message.reply_text("🔊 ပြုပြင်ထိန်းသိမ်းမုဒ် **ပိတ်** ထားပါသည်။ အသုံးပြုသူများ ပုံမှန်ရယူနိုင်ပါပြီ။")

# Other Admin commands
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

# ---------- Main Application Setup ----------
application = Application.builder().token(TOKEN).build()

# Conversation handler for /newpost
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO1: [MessageHandler(filters.VIDEO, receive_video1)],
        VIDEO2: [MessageHandler(filters.VIDEO, receive_video2)],
        VIDEO3: [MessageHandler(filters.VIDEO, receive_video3)],
    },
    fallbacks=[CommandHandler('cancel', cancel)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(conv_handler)  # /newpost command
application.add_handler(CallbackQueryHandler(movie_callback, pattern="get_movie"))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CommandHandler("cancel", cancel))  # General cancel
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

# ---------- Flask Background Thread ----------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port)

# ---------- Main Entry Point ----------
if __name__ == "__main__":
    # Flask ကို background thread မှာ run
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Bot ကို main thread မှာ run
    run_bot()
