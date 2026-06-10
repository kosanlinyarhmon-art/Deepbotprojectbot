import os
import asyncio
import json
import logging
import sys
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Flask ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "Movie Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

# ---------- Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")          # Channel Username (e.g., @mycinema)
INVITE_LINK = os.environ.get("INVITE_LINK")        # Main Channel Invite Link
OTHER_CHANNELS = os.environ.get("OTHER_CHANNELS", "").split(",") if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(os.environ.get("ADMIN_ID", "0"))]

DB_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": [], "files": {}}  # files: { file_id: { "link": "...", "caption": "...", "expires": timestamp } }

def save_data(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

# ---------- Helpers ----------
async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ---------- Send File with Auto-Delete ----------
async def send_movie_with_autodelete(chat_id: int, file_id: str, context: ContextTypes.DEFAULT_TYPE, caption: str = ""):
    """Send video file and delete after 5 minutes"""
    try:
        msg = await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
        warn_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ **သတိပေးချက်**\n\nဤဇာတ်ကားကို **၅ မိနစ်** အတွင်း အလိုအလျောက် ဖျက်ပါမည်။\nကျေးဇူးပြု၍ **Forward** လုပ်ပြီး သင်၏ Saved Messages တွင် သိမ်းဆည်းထားပါ။",
            parse_mode="Markdown"
        )
        async def delete_job():
            await asyncio.sleep(300)  # 5 min
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                await context.bot.delete_message(chat_id=chat_id, message_id=warn_msg.message_id)
            except:
                pass
        asyncio.create_task(delete_job())
        return msg
    except Exception as e:
        logger.error(f"Send movie error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ ဇာတ်ကားပို့ရာတွင် အမှား: {str(e)}")
        return None

# ---------- Promote Other Channels ----------
async def send_promotion(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if not OTHER_CHANNELS or not OTHER_CHANNELS[0]:
        return
    keyboard = []
    for idx, link in enumerate(OTHER_CHANNELS, 1):
        keyboard.append([InlineKeyboardButton(f"📢 Channel {idx} သို့ဝင်ရန်", url=link)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=chat_id,
        text="🎉 **အခြားဇာတ်ကားသစ်များ ရယူရန် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎬 **မင်္ဂလာပါ**\n\n"
        "ကျွန်ုပ်သည် Movie Bot ဖြစ်ပါသည်။\n"
        "ကျွန်ုပ်၏ Channel တွင် ဇာတ်ကား Post များအောက်ရှိ 'ဇာတ်ကားရယူရန်' ခလုတ်ကို နှိပ်၍ ရုပ်ရှင်များ ရယူနိုင်ပါသည်။\n\n"
        "📌 **သတိပြုရန်** - ရုပ်ရှင်များကို ၅ မိနစ်အတွင်း အလိုအလျောက် ဖျက်ပါမည်။ ချက်ချင်း Forward သိမ်းဆည်းပါ။"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ---------- /link (Admin only) ----------
async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင့်တွင် အခွင့်အရေးမရှိပါ။")
        return
    data = load_data()
    if not data["files"]:
        await update.message.reply_text("📂 သိမ်းဆည်းထားသော ဖိုင်များ မရှိသေးပါ။")
        return
    msg = "📁 **ဖိုင်များစာရင်း**\n\n"
    for fid, info in data["files"].items():
        msg += f"🔹 File ID: `{fid}`\n   Link: {info['link']}\n   Caption: {info.get('caption', '')}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------- /newpost (Admin only) ----------
# This command will store the last received photo, text, and video to combine later
# Usage: Send /newpost, then send a photo, then a text caption, then a video file.
# Bot will remember them and finally generate a preview message with inline button.
user_temp_data = {}  # { user_id: {"photo": file_id, "text": str, "video": file_id} }

async def newpost_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အခွင့်အရေးမရှိပါ။")
        return
    user_temp_data[update.effective_user.id] = {}
    await update.message.reply_text(
        "📝 **Post အသစ်ပြုလုပ်ရန် အဆင့်များ**\n\n"
        "1️⃣ ပုံတစ်ပုံ ပို့ပါ (သို့မဟုတ် /skip နှိပ်ပါ)\n"
        "2️⃣ စာသားပို့ပါ (သို့မဟုတ် /skip)\n"
        "3️⃣ ဗီဒီယိုဖိုင် ပို့ပါ\n\n"
        "ထို့နောက် Bot က ခလုတ်ပါသော Message အကြမ်းဖျင်းကို ပြသပေးမည်။"
    )

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in user_temp_data and user_temp_data[uid] is not None:
        # mark skipped field as None
        pass
    await update.message.reply_text("ကျော်လိုက်ပါပြီ။")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_temp_data:
        return
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        user_temp_data[uid]["photo"] = file_id
        await update.message.reply_text("✅ ပုံကို သိမ်းဆည်းပြီးပါပြီ။ ယခု စာသားပို့ပါ (သို့မဟုတ် /skip)")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_temp_data:
        return
    user_temp_data[uid]["text"] = update.message.text
    await update.message.reply_text("✅ စာသားကို သိမ်းဆည်းပြီးပါပြီ။ ယခု ဗီဒီယိုဖိုင် ပို့ပါ (မဖြစ်မနေ ပို့ရန်)")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_temp_data:
        return
    if update.message.video:
        file_id = update.message.video.file_id
        user_temp_data[uid]["video"] = file_id
        # Generate link for this video
        file_obj = await context.bot.get_file(file_id)
        file_path = file_obj.file_path
        download_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        # Store link in database
        data = load_data()
        data["files"][file_id] = {"link": download_link, "caption": user_temp_data[uid].get("text", ""), "expires": None}
        save_data(data)
        # Build final preview message
        photo_id = user_temp_data[uid].get("photo")
        caption = user_temp_data[uid].get("text", "")
        preview_text = f"{caption}\n\n👇 **ဇာတ်ကားရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ**"
        keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", callback_data=f"get_movie_{file_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if photo_id:
            # Send photo with caption and button
            await update.message.reply_photo(photo=photo_id, caption=preview_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(preview_text, reply_markup=reply_markup, parse_mode="Markdown")
        # Clear temp data
        del user_temp_data[uid]
        await update.message.reply_text("✅ Post အကြိုပုံစံ အထက်ပါအတိုင်း ဖြစ်ပါသည်။ သင့် Channel တွင် ဤ Message ကို Copy ကူးပြီး လွှင့်တင်နိုင်ပါသည်။")

# ---------- Callback for "get_movie" ----------
async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    # Extract file_id from callback data (format: "get_movie_FILE_ID")
    file_id = query.data.replace("get_movie_", "")
    data = load_data()
    if file_id not in data["files"]:
        await query.edit_message_text("❌ ဤဇာတ်ကားလင့်သည် သက်တမ်းကုန်သွားပြီ သို့မဟုတ် မရှိတော့ပါ။")
        return

    # Check if user is member of main channel
    if not await is_member(user_id, context):
        await query.edit_message_text(
            f"❌ ခင်ဗျား ကျွန်ုပ်တို့၏ Channel ကို မဝင်ရသေးပါ။\n\n👉 [Channel သို့ဝင်ရန်]({INVITE_LINK})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    # Send movie with auto-delete
    await query.edit_message_text("✅ Member ဖြစ်ပါသည်။ ဇာတ်ကား ပို့ပေးနေပါပြီ...⏳")
    await send_movie_with_autodelete(user_id, file_id, context, caption=data["files"][file_id].get("caption", ""))
    
    # Send promotion for other channels
    await send_promotion(user_id, context)

# ---------- Main Bot Setup ----------
def setup_application():
    application = Application.builder().token(TOKEN).build()
    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link", link_command))
    application.add_handler(CommandHandler("newpost", newpost_command))
    application.add_handler(CommandHandler("skip", skip))
    # Handlers for newpost sequence
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_IDS), handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_text))
    application.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_IDS), handle_video))
    # Callback
    application.add_handler(CallbackQueryHandler(movie_callback, pattern="^get_movie_"))
    return application

# ---------- Run Bot (Main Thread) ----------
def run_bot():
    application = setup_application()
    logger.info("Starting bot polling...")
    application.run_polling()

# ---------- Run Flask (Background Thread) ----------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ---------- Main ----------
if __name__ == "__main__":
    import threading
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
