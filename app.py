import os
import asyncio
import threading
import json
import logging
import sys
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

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
OTHER_CHANNELS = os.environ.get("OTHER_CHANNELS", "").split(",") if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(os.environ.get("ADMIN_ID", "0"))]

# သင်၏ Video File IDs များကို ဤနေရာတွင် ထည့်ပါ
MOVIE_FILE_IDS = [
    "FILE_ID_1",
    "FILE_ID_2",
    "FILE_ID_3"
]

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
    
    try:
        sent_videos = []
        for file_id in MOVIE_FILE_IDS:
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
        
        if OTHER_CHANNELS and OTHER_CHANNELS[0]:
            text = "🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**\n\n"
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                text += f"{idx}. [Channel {idx}]({link})\n"
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"❌ ဇာတ်ကားပို့ရာတွင် အမှား: {str(e)}")

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
    await update.message.reply_text(f"Broadcast ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    data = load_data()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 Users: {len(data['users'])}\n🎬 Requests: {data['total_requests']}")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = True
    await update.message.reply_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    if not is_admin(update.effective_user.id): return
    maintenance_mode = False
    await update.message.reply_text("🔊 Maintenance mode ပိတ်ပါပြီ။")

# ကျန်သည့် admin command အလွတ်များ
async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⏳ /schedule - demo")
async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("📋 Schedule list - demo")
async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("❌ Cancel schedule - demo")
async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("/delete - demo")
async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("/deleteall - demo")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("Cancelled")

# ---------- Bot Polling Loop ----------
def run_bot():
    while True:
        try:
            application = (
                Application.builder()
                .token(TOKEN)
                .build()
            )
            # Handlers အားလုံးထည့်ပါ
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CallbackQueryHandler(movie_callback, pattern="get_movie"))
            application.add_handler(CommandHandler("schedule", schedule))
            application.add_handler(CommandHandler("listschedule", listschedule))
            application.add_handler(CommandHandler("cancelschedule", cancelschedule))
            application.add_handler(CommandHandler("broadcast", broadcast))
            application.add_handler(CommandHandler("stats", stats))
            application.add_handler(CommandHandler("delete", delete_file))
            application.add_handler(CommandHandler("deleteall", deleteall))
            application.add_handler(CommandHandler("cancel", cancel))
            application.add_handler(CommandHandler("mute", mute))
            application.add_handler(CommandHandler("unmute", unmute))
            
            logger.info("Starting bot polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10 seconds...")
            import time
            time.sleep(10)

# ---------- Main Entry Point ----------
if __name__ == "__main__":
    # Bot ကို background thread ထဲ စတင်မည်
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Flask server ကို main thread ထဲ တိုက်ရိုက် run မည် (Render သိရန်)
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port)
