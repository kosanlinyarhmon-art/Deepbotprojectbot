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
BOT_USERNAME = os.environ.get("BOT_USERNAME")  # e.g., "wznmoviefileshare_bot" without @
CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
OTHER_CHANNELS = [link for link in os.environ.get("OTHER_CHANNELS", "").split(",") if link] if os.environ.get("OTHER_CHANNELS") else []
ADMIN_IDS = [int(id) for id in os.environ.get("ADMIN_ID", "").split(",") if id] if os.environ.get("ADMIN_ID") else []

DB_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": [], "total_requests": 0, "file_store": {}}  # file_store: {payload: file_id}

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

# ---------- Generate Unique Payload ----------
def generate_payload():
    return secrets.token_urlsafe(16)  # e.g., "BQADAQADaBIAAtb6QUX0lxhUpbvjmBYE"

# ---------- Start Command (Deep Link Handler) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Check if there is a payload (deep link)
    if context.args and len(context.args) > 0:
        payload = context.args[0]
        data = load_data()
        file_id = data["file_store"].get(payload)
        if file_id:
            # Send file to user
            await update.message.reply_text("🎬 သင့်ဇာတ်ကား ပို့ပေးနေပါပြီ...⏳")
            try:
                video_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption="🎬 သင့်ဇာတ်ကား")
                # Warning message
                warn_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ **သတိပေးချက်**\n\nဤဇာတ်ကားကို **၅ မိနစ်** အတွင်း ဖျက်ပါမည်။\nကျေးဇူးပြု၍ **Forward** လုပ်ပြီး သိမ်းထားပါ။",
                    parse_mode="Markdown"
                )
                # Auto delete after 5 minutes
                async def delete_after():
                    await asyncio.sleep(300)
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                        await context.bot.delete_message(chat_id=user_id, message_id=video_msg.message_id)
                    except:
                        pass
                asyncio.create_task(delete_after())

                # Invite other channels
                if OTHER_CHANNELS:
                    text = "🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**\n\n"
                    for idx, link in enumerate(OTHER_CHANNELS, 1):
                        text += f"{idx}. [Channel {idx}]({link})\n"
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)

                # Update stats
                data = load_data()
                if user_id not in data["users"]:
                    data["users"].append(user_id)
                data["total_requests"] += 1
                save_data(data)

                # Optional: remove payload after use (one-time link)
                # del data["file_store"][payload]
                # save_data(data)
            except Exception as e:
                await context.bot.send_message(chat_id=user_id, text=f"❌ ဖိုင်ပို့ရာတွင် အမှား: {str(e)}")
        else:
            await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
    else:
        # Normal /start without payload
        await update.message.reply_text(
            "🎬 မင်္ဂလာပါ။\n"
            "ကျွန်ုပ်သည် File Share Bot ဖြစ်ပါသည်။\n"
            "ဇာတ်ကား Video တစ်ခုကို ကျွန်ုပ်ထံ ပို့ပါ၊ သင်မျှဝေနိုင်သော လင့်ကို ရရှိမည်ဖြစ်သည်။"
        )

# ---------- Handle Video Files from Users (to generate shareable link) ----------
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    video = update.message.video
    if not video:
        await update.message.reply_text("ကျေးဇူးပြု၍ Video file တစ်ခု ပို့ပေးပါ။")
        return

    # Generate unique payload
    payload = generate_payload()
    data = load_data()
    data["file_store"][payload] = video.file_id
    save_data(data)

    # Create shareable deep link
    deep_link = f"https://t.me/{BOT_USERNAME}?start={payload}"
    await update.message.reply_text(
        f"🔗 **သင်၏ မျှဝေနိုင်သော လင့်**\n\n"
        f"`{deep_link}`\n\n"
        f"ဤလင့်ကို ကူးယူ၍ Channel သို့မဟုတ် အခြားနေရာများတွင် မျှဝေနိုင်ပါသည်။\n"
        f"အသုံးပြုသူများ လင့်ကိုနှိပ်ပါက ဗီဒီယိုကို ၎င်းတို့၏ Chat တွင် ရရှိမည်ဖြစ်သည်။",
        parse_mode="Markdown"
    )

# ---------- Admin: /newpost Command (ပုံ+စာ+Video ကို ပေါင်းပြီး Preview ထုတ်ရန်) ----------
POSTER, CAPTION, VIDEO_FILE = range(3)

async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return ConversationHandler.END
    await update.message.reply_text("📸 Channel အတွက် ပုံတစ်ပုံ ပို့ပေးပါ...")
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
    await update.message.reply_text("🎬 ဇာတ်ကား Video File ကို ပို့ပေးပါ... (ဒီ Video သည် Channel Post တွင် ပါဝင်မည်မဟုတ်ပါ၊ သို့သော် သင်သိမ်းဆည်းရန်)။ သင်ပို့သော Video အတွက် မျှဝေနိုင်သော လင့်ကိုလည်း ကျွန်ုပ်ထုတ်ပေးပါမည်။")
    return VIDEO_FILE

async def receive_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")
        return VIDEO_FILE

    # Generate shareable link for this video as well (optional)
    video = update.message.video
    payload = generate_payload()
    data = load_data()
    data["file_store"][payload] = video.file_id
    save_data(data)
    deep_link = f"https://t.me/{BOT_USERNAME}?start={payload}"

    # Save for later use (if needed)
    context.user_data['deep_link'] = deep_link

    poster = context.user_data['poster']
    caption_text = context.user_data['caption']

    # Create button that will be used in channel post
    # Note: We can put the deep link directly as URL button? No, URL button can't have dynamic callback.
    # So we use callback data "get_movie" that will trigger the same file? But that file is not tied to a specific user.
    # Better approach: In channel post, we put a button that sends the deep link text? Or we just put the deep link as text?
    # But the requirement from the image: user clicks button and gets file in private chat.
    # So we can make callback button that sends the deep link to user? That's extra step.
    # Alternatively, we can make the button open a URL that contains the deep link? Telegram URL buttons can't have t.me links? They can.
    # Actually, InlineKeyboardButton can have url parameter. We can put the deep_link as URL.
    # So when user clicks the button, it opens the deep link in Telegram and starts the bot.
    # That's perfect!

    url_button = InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)
    reply_markup = InlineKeyboardMarkup([[url_button]])

    # Send preview
    await update.message.reply_photo(
        photo=poster,
        caption=caption_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"✅ အဆင်သင့်ပါပြီ။ ဤ Message ကို **Forward** လုပ်ပြီး သင့် Channel မှာ တင်လိုက်ပါ။\n\n"
        f"မှတ်ချက် - ခလုတ်ကို နှိပ်ပါက အောက်ပါ လင့်သို့ သွားပါမည် -\n`{deep_link}`",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Other Admin Commands ----------
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

# Conversation for /newpost
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        VIDEO_FILE: [MessageHandler(filters.VIDEO, receive_video_file)],
    },
    fallbacks=[CommandHandler('cancel', cancel_conv)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_video))
application.add_handler(conv_handler)
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
            logger.exception(f"Bot polling crashed: {e}. Restarting in 10 seconds...")
            import time
            time.sleep(10)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
