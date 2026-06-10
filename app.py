import os
import asyncio
import threading
import json
import logging
import sys
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

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
CHANNEL_ID = os.environ.get("CHANNEL_ID")          # Channel username or ID (e.g., @mychannel)
INVITE_LINK = os.environ.get("INVITE_LINK")        # Main channel invite link
OTHER_CHANNELS = os.environ.get("OTHER_CHANNELS", "").split(",") if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(os.environ.get("ADMIN_ID", "0"))]

# Temporary storage for admin's post data (photo, caption, video)
admin_post_data = {}

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

# ---------- Helper: Send movie with auto-delete ----------
async def send_movie_with_autodelete(user_id: int, video_file_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Send video and schedule deletion after 5 minutes"""
    try:
        # Send video
        video_msg = await context.bot.send_video(chat_id=user_id, video=video_file_id, caption="🎬 သင့်ဇာတ်ကား")
        
        # Send warning message
        warn_msg = await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ **သတိပေးချက်**\n\nဤဇာတ်ကားသည် **၅ မိနစ်** အတွင်း အလိုအလျောက် ပျက်ပါမည်။\nကျေးဇူးပြု၍ Saved Messages တွင် **Forward** လုပ်ပြီး သိမ်းဆည်းထားပါ။",
            parse_mode="Markdown"
        )
        
        # Schedule deletion
        async def delete_after():
            await asyncio.sleep(300)  # 5 minutes
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                logger.info(f"Deleted movie and warning for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to delete: {e}")
        
        asyncio.create_task(delete_after())
        return True
    except Exception as e:
        logger.error(f"Error sending movie: {e}")
        return False

# ---------- Callback: Movie request button ----------
async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if maintenance_mode:
        await query.edit_message_text("⚠️ Bot သည် ပြုပြင်ထိန်းသိမ်းမှုမုဒ်တွင် ရှိပါသည်။")
        return

    # Member check
    if not await is_member(user_id, context):
        await query.edit_message_text(
            f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 [Channel သို့ဝင်ရန်]({INVITE_LINK})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    # Get video file ID from callback data
    # Format: "get_movie|file_id"
    data = query.data
    if "|" in data:
        _, video_file_id = data.split("|")
    else:
        await query.edit_message_text("❌ ဇာတ်ကား အချက်အလက် မှားယွင်းနေပါသည်။")
        return

    # Update stats
    db = load_data()
    if user_id not in db["users"]:
        db["users"].append(user_id)
    db["total_requests"] += 1
    save_data(db)

    await query.edit_message_text("✅ အောင်မြင်ပါသည်။ ဇာတ်ကား ပို့ပေးနေပါပြီ...⏳")

    # Send movie with auto-delete
    success = await send_movie_with_autodelete(user_id, video_file_id, context)
    
    if success:
        # After sending, invite to other channels with buttons
        if OTHER_CHANNELS and len(OTHER_CHANNELS) >= 2:
            keyboard = []
            for idx, link in enumerate(OTHER_CHANNELS[:2], 1):
                keyboard.append([InlineKeyboardButton(f"📢 Channel {idx}", url=link)])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 **နောက်ထပ် ဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်လိုက်ပါ**\n\nထိုနေရာများတွင်လည်း အလားတူ ဇာတ်ကားများ ရရှိနိုင်ပါသည်။",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    else:
        await context.bot.send_message(chat_id=user_id, text="❌ ဇာတ်ကားပို့ရာတွင် ချို့ယွင်းမှု ရှိသည်။ နောက်မှ ထပ်ကြိုးစားပါ။")

# ---------- Admin: Receive photo, caption, video and create channel post ----------
async def admin_collect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    admin_post_data[user_id] = {"step": "waiting_caption"}
    await update.message.reply_text("📸 ပုံကို လက်ခံရရှိပါပြီ။\n\nယခု ထိုဇာတ်ကားအတွက် **စာသား (caption)** ကို ပို့ပေးပါ။")

async def admin_collect_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or user_id not in admin_post_data:
        return
    caption = update.message.text
    admin_post_data[user_id]["caption"] = caption
    admin_post_data[user_id]["step"] = "waiting_video"
    await update.message.reply_text("✅ စာသားကို လက်ခံရရှိပါပြီ။\n\nယခု **ဇာတ်ကား Video ဖိုင်** ကို ပို့ပေးပါ။")

async def admin_collect_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or user_id not in admin_post_data:
        return
    if not update.message.video:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ **Video ဖိုင်** (MP4 စသည်) ကို ပို့ပေးပါ။")
        return
    video_file = update.message.video
    video_file_id = video_file.file_id
    
    # Retrieve stored data
    photo_file_id = admin_post_data[user_id].get("photo")
    caption = admin_post_data[user_id].get("caption")
    
    if not photo_file_id or not caption:
        await update.message.reply_text("❌ အချက်အလက် မပြည့်စုံပါ။ /cancel ဖြင့် ပယ်ဖျက်ပြီး ထပ်မံစတင်ပါ။")
        return
    
    # Create channel post with inline button
    button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", callback_data=f"get_movie|{video_file_id}")
    reply_markup = InlineKeyboardMarkup([[button]])
    
    try:
        # Send photo with caption and button to channel
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_file_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ **အောင်မြင်ပါသည်။**\n\nChannel တွင် ဇာတ်ကား Post ကို ဖန်တီးပေးလိုက်ပါပြီ။\nအသုံးပြုသူများ ခလုတ်နှိပ်ပါက ဇာတ်ကား ရရှိမည်ဖြစ်သည်။")
    except Exception as e:
        await update.message.reply_text(f"❌ Channel Post ဖန်တီးရာတွင် အမှား: {str(e)}")
    finally:
        # Clear temporary data
        del admin_post_data[user_id]

# ---------- Cancel admin post creation ----------
async def cancel_admin_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if user_id in admin_post_data:
        del admin_post_data[user_id]
        await update.message.reply_text("လုပ်ဆောင်ချက်ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    else:
        await update.message.reply_text("လက်ရှိ လုပ်ဆောင်နေသော တစ်စုံတစ်ရာ မရှိပါ။")

# ---------- Admin Commands (All in Burmese) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **ဇာတ်ကား Bot မှ ကြိုဆိုပါတယ်**\n\n"
        "ဤ Bot သည် Channel အတွက် ဇာတ်ကားများ ဖြန့်ချိရန် ရည်ရွယ်ပါသည်။\n\n"
        "📌 **အသုံးပြုသူများအတွက်** – Channel ရှိ Post အောက်က 'ဇာတ်ကားရယူရန်' ခလုတ်ကို နှိပ်ပါ။\n"
        "📌 **Admin အတွက်** – /newpost ဖြင့် ပုံ၊ စာသား၊ ဗီဒီယို သုံးမျိုးတွဲကာ Channel Post ဖန်တီးနိုင်ပါသည်။",
        parse_mode="Markdown"
    )

async def newpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("ဤ command ကို Admin များသာ သုံးနိုင်ပါသည်။")
        return
    if user_id in admin_post_data:
        await update.message.reply_text("လက်ရှိ Post တစ်ခုကို ဖန်တီးနေဆဲဖြစ်သည်။ ပထမ /cancel ဖြင့် ပယ်ဖျက်ပါ။")
        return
    admin_post_data[user_id] = {"step": "waiting_photo"}
    await update.message.reply_text("📝 **ဇာတ်ကား Post အသစ်ဖန်တီးခြင်း**\n\nကျေးဇူးပြု၍ **ဇာတ်ကားပုံ (Photo)** ကို ပို့ပေးပါ။")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("📢 /broadcast <မက်ဆေ့ချ်>")
        return
    data = load_data()
    count = 0
    for uid in data["users"]:
        try:
            await context.bot.send_message(chat_id=uid, text=message)
            count += 1
        except:
            pass
    await update.message.reply_text(f"ပြန်လွှင့်မှု ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")

# ---------- Message handlers for admin post creation ----------
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or user_id not in admin_post_data:
        return
    
    state = admin_post_data[user_id].get("step")
    
    if state == "waiting_photo":
        if update.message.photo:
            photo = update.message.photo[-1]
            admin_post_data[user_id]["photo"] = photo.file_id
            admin_post_data[user_id]["step"] = "waiting_caption"
            await update.message.reply_text("📸 ပုံကို လက်ခံရရှိပါပြီ။\n\nယခု **စာသား (caption)** ကို ပို့ပါ။")
        else:
            await update.message.reply_text("❌ ပုံ (Photo) ကိုသာ ပို့ပါ။")
    elif state == "waiting_caption":
        if update.message.text:
            admin_post_data[user_id]["caption"] = update.message.text
            admin_post_data[user_id]["step"] = "waiting_video"
            await update.message.reply_text("✅ စာသားရရှိပါပြီ။\n\nယခု **ဇာတ်ကား Video** ကို ပို့ပါ။")
        else:
            await update.message.reply_text("❌ စာသားကို စာလုံးဖြင့် ရိုက်ထည့်ပါ။")
    elif state == "waiting_video":
        if update.message.video:
            video_file_id = update.message.video.file_id
            photo_file_id = admin_post_data[user_id].get("photo")
            caption = admin_post_data[user_id].get("caption")
            
            # Create channel post
            button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", callback_data=f"get_movie|{video_file_id}")
            reply_markup = InlineKeyboardMarkup([[button]])
            
            try:
                await context.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=photo_file_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
                await update.message.reply_text("✅ **Post ဖန်တီးခြင်း အောင်မြင်ပါသည်။**\n\nChannel တွင် ဝင်ရောက်ကြည့်ရှုနိုင်ပါပြီ။")
            except Exception as e:
                await update.message.reply_text(f"❌ Channel Post ပို့ရာတွင် အမှား: {str(e)}")
            finally:
                del admin_post_data[user_id]
        else:
            await update.message.reply_text("❌ Video ဖိုင် (MP4) ကို ပို့ပါ။")

# ---------- Bot Polling Loop ----------
def run_bot():
    while True:
        try:
            application = Application.builder().token(TOKEN).build()
            # Admin commands
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("newpost", newpost))
            application.add_handler(CommandHandler("cancel", cancel))
            application.add_handler(CommandHandler("broadcast", broadcast))
            application.add_handler(CommandHandler("stats", stats))
            application.add_handler(CommandHandler("mute", mute))
            application.add_handler(CommandHandler("unmute", unmute))
            application.add_handler(CommandHandler("schedule", schedule))
            application.add_handler(CommandHandler("listschedule", listschedule))
            application.add_handler(CommandHandler("cancelschedule", cancelschedule))
            application.add_handler(CommandHandler("delete", delete_file))
            application.add_handler(CommandHandler("deleteall", deleteall))
            # Callback for movie button
            application.add_handler(CallbackQueryHandler(movie_callback, pattern="^get_movie"))
            # Message handler for admin post creation
            application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.TEXT & ~filters.COMMAND, handle_admin_messages))
            
            logger.info("Starting bot polling...")
            application.run_polling()
        except Exception as e:
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10 seconds...")
            import time
            time.sleep(10)

# ---------- Main ----------
if __name__ == "__main__":
    # Flask server in background thread
    def run_flask():
        port = int(os.environ.get("PORT", 5000))
        logger.info(f"Starting Flask server on port {port}")
        app.run(host="0.0.0.0", port=port)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Bot in main thread
    run_bot()
