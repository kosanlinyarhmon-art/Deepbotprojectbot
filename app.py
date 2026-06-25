import os
import asyncio
import threading
import logging
import sys
import secrets
import re
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph

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

# New collections for new features
batch_collection = db["batch_messages"]       # for /batch, /custom_batch, /special_link
short_links_collection = db["short_links"]    # for /shortener

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
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now()})

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

# ========== Helper functions for new features ==========

def save_batch_messages(batch_id, messages_data):
    """Save a list of messages under a batch_id"""
    batch_collection.update_one(
        {"batch_id": batch_id},
        {"$set": {"messages": messages_data, "created_at": datetime.now()}},
        upsert=True
    )

def get_batch_messages(batch_id):
    doc = batch_collection.find_one({"batch_id": batch_id})
    return doc["messages"] if doc else None

def save_short_link(short_code, original_url):
    short_links_collection.update_one(
        {"short_code": short_code},
        {"$set": {"original_url": original_url, "created_at": datetime.now()}},
        upsert=True
    )

def get_original_url(short_code):
    doc = short_links_collection.find_one({"short_code": short_code})
    return doc["original_url"] if doc else None

def extract_message_data(msg):
    """Extract message data from a Telegram message object"""
    if msg.text:
        return {"type": "text", "text": msg.text}
    elif msg.video:
        return {"type": "video", "file_id": msg.video.file_id, "caption": msg.caption or "Video"}
    elif msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or "Photo"}
    elif msg.document:
        return {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption or "Document"}
    elif msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id, "caption": msg.caption or "Audio"}
    elif msg.voice:
        return {"type": "voice", "file_id": msg.voice.file_id, "caption": "Voice"}
    elif msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id, "caption": msg.caption or "GIF"}
    else:
        return {"type": "text", "text": f"Message ID: {msg.message_id}"}

# ========== End helper functions ==========

# ---------- Telegram Configuration ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
INVITE_LINK = os.environ.get("INVITE_LINK")
MUSIC_CHANNEL_LINK = os.environ.get("MUSIC_CHANNEL_LINK", "")
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

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name=BOT_USERNAME or 'MovieBot')
except:
    pass

async def create_telegraph_page(title: str, content_text: str) -> str:
    try:
        html_content = content_text.replace('\n', '<br>')
        response = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html_content}</p>",
            author_name="WZN Cinema Hub Movies"
        )
        return response['url']
    except Exception as e:
        logger.error(f"Telegraph error: {e}")
        return None

# ---------- Start & Deep Link Handler (UPDATED) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        
        # ===== Handle shortener links =====
        if payload.startswith('short_'):
            short_code = payload[6:]
            original_url = get_original_url(short_code)
            if original_url:
                await update.message.reply_text(f"🔗 မူရင်း Link ပါ။\n\n{original_url}")
            else:
                await update.message.reply_text("❌ ဤအတိုချုံ့ထားသော Link သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        
        # ===== Handle batch / special links =====
        if payload.startswith('batch_') or payload.startswith('special_'):
            batch_id = payload
            messages = get_batch_messages(batch_id)
            if not messages:
                # If not found, fallback to single file
                file_info = get_file_info(payload)
                if file_info:
                    messages = [{"type": "video", "file_id": file_info["file_id"], "caption": file_info["file_name"]}]
                else:
                    await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
                    return
            
            # Check channel membership
            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 Channel သို့ဝင်ရန်: {INVITE_LINK}",
                    disable_web_page_preview=True
                )
                return
            
            add_user(user_id)
            increment_requests()
            
            await update.message.reply_text(f"📦 စုစည်းထားသော အကြောင်းအရာ {len(messages)} ခုကို ပို့ပေးနေပါပြီ...")
            
            # Send each message
            for msg in messages:
                try:
                    if msg['type'] == 'text':
                        await context.bot.send_message(chat_id=user_id, text=msg['text'])
                    elif msg['type'] == 'video':
                        await context.bot.send_video(chat_id=user_id, video=msg['file_id'], caption=msg.get('caption', ''))
                    elif msg['type'] == 'photo':
                        await context.bot.send_photo(chat_id=user_id, photo=msg['file_id'], caption=msg.get('caption', ''))
                    elif msg['type'] == 'document':
                        await context.bot.send_document(chat_id=user_id, document=msg['file_id'], caption=msg.get('caption', ''))
                    elif msg['type'] == 'audio':
                        await context.bot.send_audio(chat_id=user_id, audio=msg['file_id'], caption=msg.get('caption', ''))
                    elif msg['type'] == 'voice':
                        await context.bot.send_voice(chat_id=user_id, voice=msg['file_id'], caption=msg.get('caption', ''))
                    elif msg['type'] == 'animation':
                        await context.bot.send_animation(chat_id=user_id, animation=msg['file_id'], caption=msg.get('caption', ''))
                    else:
                        await context.bot.send_message(chat_id=user_id, text=msg.get('text', 'Unknown message type'))
                except Exception as e:
                    logger.error(f"Error sending batch item: {e}")
                    await context.bot.send_message(chat_id=user_id, text=f"❌ မက်ဆေ့ချ်တစ်ခုပို့ရာတွင် အမှား: {str(e)}")
            
            # Send channel buttons
            keyboard = []
            if OTHER_CHANNELS:
                for idx, link in enumerate(OTHER_CHANNELS, 1):
                    if idx == 1:
                        keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                    elif idx == 2:
                        keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                    elif idx == 3:
                        keyboard.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
                    else:
                        keyboard.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
            if MUSIC_CHANNEL_LINK:
                keyboard.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])
            
            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🎉 **အခြားဇာတ်ကားများအတွက် အောက်ပါ Channel များသို့ ဝင်ရောက်ပါ**",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            return
        
        # ===== Existing single file deep link =====
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
                    "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n!!! IMPORTANT !!!\n"
                    "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
                    "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
                )
                warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)

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

                keyboard = []
                if OTHER_CHANNELS:
                    for idx, link in enumerate(OTHER_CHANNELS, 1):
                        if idx == 1:
                            keyboard.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                        elif idx == 2:
                            keyboard.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                        elif idx == 3:
                            keyboard.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
                        else:
                            keyboard.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
                if MUSIC_CHANNEL_LINK:
                    keyboard.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

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
            await show_menu(update, context)
        else:
            await update.message.reply_text(
                "🎬 **မင်္ဂလာပါ**\n\n"
                "ဤ Bot သည် Channel အတွက် ဇာတ်ကားများ ဖြန့်ဝေရန် သုံးပါသည်။\n"
                "ဇာတ်ကားရယူရန် Channel ရှိ Post အောက်က ခလုတ်ကို နှိပ်ပါ။",
                parse_mode="Markdown"
            )

# ---------- Admin Menu (UPDATED) ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 ပို့စ်အသစ်", callback_data="menu_newpost")],
        [InlineKeyboardButton("🔗 Video → Deep Link", callback_data="menu_link")],
        [InlineKeyboardButton("📦 Custom Batch", callback_data="menu_custom_batch")],
        [InlineKeyboardButton("🔗 Special Link", callback_data="menu_special_link")],
        [InlineKeyboardButton("🔗 URL အတိုချုံ့ရန်", callback_data="menu_shortener")],
        [InlineKeyboardButton("📊 စာရင်းအင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("📢 ပြန်လွှင့်ခြင်း", callback_data="menu_broadcast")],
        [InlineKeyboardButton("⏰ Schedule ပြုလုပ်ရန်", callback_data="menu_schedule")],
        [InlineKeyboardButton("📋 Schedule စာရင်း", callback_data="menu_listschedule")],
        [InlineKeyboardButton("❌ Schedule ဖျက်ရန်", callback_data="menu_cancelschedule")],
        [InlineKeyboardButton("🗑️ ဖိုင်ဖျက်ရန် (ID)", callback_data="menu_delete")],
        [InlineKeyboardButton("⚠️ ဖိုင်အားလုံးဖျက်ရန်", callback_data="menu_deleteall")],
        [InlineKeyboardButton("🔇 Maintenance mode ဖွင့်", callback_data="menu_mute")],
        [InlineKeyboardButton("🔊 Maintenance mode ပိတ်", callback_data="menu_unmute")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🤖 **Admin Menu**\n\nအောက်ပါခလုတ်များကို နှိပ်ပါ။", reply_markup=reply_markup, parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global maintenance_mode
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return

    data = query.data
    if data == "menu_newpost":
        await query.edit_message_text("📸 `/newpost` command ကို သုံးပါ။ (Post ဖန်တီးရန်)")
    elif data == "menu_link":
        await query.edit_message_text("🔗 `/link` command ကို သုံးပါ။ (Video ပို့ပါက Deep Link ရမည်)")
    elif data == "menu_custom_batch":
        await query.edit_message_text("📦 `/custom_batch` command ကို သုံးပါ။\n\nမတူညီသော မက်ဆေ့ချ်များကို စိတ်ကြိုက်ရွေးချယ်ပြီး စုစည်းပါ။")
    elif data == "menu_special_link":
        await query.edit_message_text("🔗 `/special_link` command ကို သုံးပါ။\n\nSpecial Link အသစ်ပြုလုပ်ရန်၊ ပြင်ဆင်ရန်၊ ဖျက်ရန်။")
    elif data == "menu_shortener":
        await query.edit_message_text("🔗 `/shortener <URL>` command ကို သုံးပါ။\n\nURL ကို အတိုချုံ့ပေးပါမည်။")
    elif data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_files = file_store_collection.count_documents({})
        await query.edit_message_text(
            f"📊 **စာရင်းအင်း**\n\n"
            f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
            f"🎬 ဖိုင်အရေအတွက်: {total_files}",
            parse_mode="Markdown"
        )
    elif data == "menu_broadcast":
        await query.edit_message_text("📢 `/broadcast <message>` ဖြင့် အသုံးပြုသူအားလုံးကို စာပို့နိုင်ပါသည်။")
    elif data == "menu_schedule":
        await query.edit_message_text("⏰ `/schedule` command ကို သုံးပါ။ (အဆင့်လိုက်မေးပါမည်)")
    elif data == "menu_listschedule":
        await query.edit_message_text("📋 `/listschedule` ဖြင့် schedule စာရင်းကြည့်ပါ။")
    elif data == "menu_cancelschedule":
        await query.edit_message_text("❌ `/cancelschedule <id>` ဖြင့် schedule ဖျက်ပါ။")
    elif data == "menu_delete":
        await query.edit_message_text("🗑️ `/delete <file_id>` ဖြင့် ဖိုင်ဖျက်ပါ။")
    elif data == "menu_deleteall":
        await query.edit_message_text("⚠️ `/deleteall` ဖြင့် ဖိုင်အားလုံးဖျက်ပါ။ (အတည်ပြုမေးမည်)")
    elif data == "menu_mute":
        maintenance_mode = True
        await query.edit_message_text("🔇 Maintenance mode ဖွင့်ထားပါသည်။")
    elif data == "menu_unmute":
        maintenance_mode = False
        await query.edit_message_text("🔊 Maintenance mode ပိတ်ထားပါသည်။")

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
                    f"သင်၏ ဇာတ်ကားရယူရန် လင့်\n\n"
                    f"{deep_link}\n\n"
                    f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် ({file_name}) ကို ချက်ချင်းရရှိမည်။\n"
                    f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Deep Link ထုတ်ရာတွင် အမှား: {str(e)}")
            context.user_data.pop('waiting_for_video_link', None)
        else:
            await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ။")

# ---------- /newpost Command ----------
POSTER, CAPTION, VIDEO_FILE, WAITING_VIDEO = range(4)

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
    context.user_data['caption_parts'] = []
    await update.message.reply_text("✍️ ဇာတ်ကားအကြောင်း စာသား (ဇာတ်ညွှန်း) ရေးပေးပါ...\n(စာသားရှည်ပါက ၂ ခါခွဲပို့နိုင်ပါသည်။ ပြီးပါက 'a' ရိုက်ပါ။)")
    return CAPTION

async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.lower() == 'a':
        caption_parts = context.user_data.get('caption_parts', [])
        if not caption_parts:
            await update.message.reply_text("⚠️ ဇာတ်ညွှန်း စာသား မရှိသေးပါ။ စာသား ပို့ပေးပါ။")
            return CAPTION
        full_caption = "\n\n".join(caption_parts)
        context.user_data['caption_full'] = full_caption
        context.user_data['telegraph_url'] = None

        if len(full_caption) > 1024:
            title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                page_url = await create_telegraph_page(title, full_caption)
                if page_url:
                    context.user_data['telegraph_url'] = page_url
                    await update.message.reply_text(f"✅ Telegraph စာမျက်နှာ ဖန်တီးပြီးပါပြီ။\n\nဇာတ်ညွှန်းအပြည့်အစုံကို ဤလင့်တွင် ဖတ်ရှုနိုင်ပါသည်။\n{page_url}")
                else:
                    await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် အမှားရှိသည်။ စာသားကို ဆက်လက်အသုံးပြုပါမည်။")
            except Exception as e:
                logger.error(f"Telegraph error: {e}")
                await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် ချို့ယွင်းချက်ရှိသည်။")

        await update.message.reply_text("🎬 Video File ကို ပို့ပေးပါ...")
        return WAITING_VIDEO
    else:
        caption_parts = context.user_data.get('caption_parts', [])
        caption_parts.append(text)
        context.user_data['caption_parts'] = caption_parts
        await update.message.reply_text(f"✅ ဇာတ်ညွှန်းအပိုင်း {len(caption_parts)} ကို လက်ခံရရှိပါပြီ။\n\nနောက်ထပ်အပိုင်းရှိလျှင် ထပ်ပို့ပါ။ ပြီးပါက 'a' ကို ရိုက်ပါ။")
        return CAPTION

async def receive_video_after_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = None
    if update.message.video:
        video = update.message.video
    elif update.message.document and update.message.document.mime_type.startswith('video/'):
        video = update.message.document

    if not video:
        await update.message.reply_text("Video file တစ်ခု ပို့ပေးပါ (video file သို့မဟုတ် video document)။")
        return WAITING_VIDEO

    try:
        file_name = getattr(video, 'file_name', None)
        if not file_name:
            file_name = "ဇာတ်ကား"

        payload = generate_payload()
        save_file_info(payload, video.file_id, file_name)
        deep_link = create_deep_linked_url(BOT_USERNAME, payload)

        buttons = []
        buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)])
        synopsis_url = context.user_data.get('telegraph_url')
        if synopsis_url:
            buttons.append([InlineKeyboardButton("📖 ဇာတ်ညွှန်းအပြည့်အစုံဖတ်ရန်", url=synopsis_url)])
        if OTHER_CHANNELS:
            for idx, link in enumerate(OTHER_CHANNELS, 1):
                if idx == 1:
                    buttons.append([InlineKeyboardButton("🎬 ဇာတ်ကားချန်နယ်", url=link)])
                elif idx == 2:
                    buttons.append([InlineKeyboardButton("👥 လူကြီးချန်နယ်", url=link)])
                elif idx == 3:
                    buttons.append([InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url=link)])
                else:
                    buttons.append([InlineKeyboardButton(f"Channel {idx}", url=link)])
        if MUSIC_CHANNEL_LINK:
            buttons.append([InlineKeyboardButton("🎵 သီချင်း/တရားတော် 🙏", url=MUSIC_CHANNEL_LINK)])

        reply_markup = InlineKeyboardMarkup(buttons)

        poster = context.user_data.get('poster')
        caption_full = context.user_data.get('caption_full', '')
        telegraph_url = context.user_data.get('telegraph_url')

        if not poster:
            await update.message.reply_text("ပုံ မတွေ့ပါ။ /newpost ကို ထပ်မံစတင်ပါ။")
            return ConversationHandler.END

        if telegraph_url:
            preview = caption_full[:300] + "..." if len(caption_full) > 300 else caption_full
            photo_caption = f"📝 ဇာတ်ကားအကျဉ်းချုပ်\n\n{preview}"
        else:
            truncated = caption_full[:1000] + "..." if len(caption_full) > 1000 else caption_full
            photo_caption = f"📝 ဇာတ်ကားအကြောင်း\n\n{truncated}"

        await update.message.reply_photo(photo=poster, caption=photo_caption, reply_markup=reply_markup)
        await update.message.reply_text(
            f"သင်၏ ဇာတ်ကားရယူရန် လင့်\n\n"
            f"{deep_link}\n\n"
            f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် ({file_name}) ကို ချက်ချင်းရရှိမည်။\n"
            f"မှတ်ချက် - Channel Member များသာ ရယူနိုင်ပါမည်။"
        )
        await update.message.reply_text("✅ **Post ဖန်တီးပြီးပါပြီ။**\n\nဤ Post ကို Forward လုပ်ပြီး Channel မှာ တင်လိုက်ပါ။")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Post ဖန်တီးရာတွင် အမှား: {str(e)}")
        return ConversationHandler.END

async def cancel_newpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# =============================================================================
# NEW COMMANDS: /custom_batch and /special_link
# =============================================================================

# ---------- /custom_batch (UPDATED) ----------
CUSTOM_BATCH_COLLECT = 300

async def custom_batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    context.user_data['custom_batch_list'] = []
    context.user_data['custom_batch_state'] = 'collecting'
    
    await update.message.reply_text(
        "📦 **Custom Batch ပြုလုပ်ရန်**\n\n"
        "မတူညီသော မက်ဆေ့ချ်များကို အောက်ပါအတိုင်း ထည့်သွင်းပါ။\n\n"
        "➡️ **စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ Document အားလုံး လက်ခံပါသည်။**\n"
        "➡️ မက်ဆေ့ချ်တစ်ခုချင်းစီကို တစ်ခါတည်း ပို့ပါ။\n"
        "➡️ တိုက်ရိုက်ပို့ပါ၊ Forward လုပ်ပါ၊ သို့မဟုတ် Link ပို့ပါ။\n\n"
        "မက်ဆေ့ချ်အားလုံး ထည့်ပြီးပါက `done` လို့ ရိုက်ပါ။"
    )
    return CUSTOM_BATCH_COLLECT

async def collect_custom_batch_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('custom_batch_state') != 'collecting':
        return
    
    if not is_admin(update.effective_user.id):
        return
    
    # `done` ဆိုရင် စုစည်းပြီး Link ထုတ်ပေးမယ်
    if update.message.text and update.message.text.lower() == 'done':
        messages = context.user_data.get('custom_batch_list', [])
        if not messages:
            await update.message.reply_text("⚠️ မက်ဆေ့ချ် အနည်းဆုံး တစ်ခုတော့ ထည့်သွင်းပေးပါ။")
            return CUSTOM_BATCH_COLLECT
        
        batch_id = f"batch_{generate_payload()}"
        save_batch_messages(batch_id, messages)
        deep_link = create_deep_linked_url(BOT_USERNAME, batch_id)
        
        context.user_data.pop('custom_batch_list', None)
        context.user_data.pop('custom_batch_state', None)
        
        await update.message.reply_text(
            f"✅ **Custom Batch ပြုလုပ်ပြီးပါပြီ။**\n\n"
            f"📦 သိမ်းဆည်းထားသော မက်ဆေ့ချ်: {len(messages)} ခု\n\n"
            f"🔗 **မျှဝေရန် Link:**\n{deep_link}\n\n"
            f"📌 ပါဝင်သော အမျိုးအစားများ:\n"
            f"{', '.join(set([msg['type'] for msg in messages]))}"
        )
        return ConversationHandler.END
    
    # Collect message (ALL TYPES)
    msg_data = None
    chat_id = None
    msg_id = None
    
    # ---------- Forwarded Message ----------
    if update.message.forward_from_chat:
        chat_id = update.message.forward_from_chat.id
        msg_id = update.message.forward_from_message_id
        try:
            forwarded_msg = await context.bot.get_messages(chat_id=chat_id, message_ids=msg_id)
            if forwarded_msg:
                msg_data = extract_message_data(forwarded_msg)
        except Exception as e:
            logger.error(f"Forwarded message fetch error: {e}")
            await update.message.reply_text("❌ Forward လုပ်ထားတဲ့ မက်ဆေ့ချ်ကို ဖတ်လို့မရပါ။")
            return CUSTOM_BATCH_COLLECT
    
    # ---------- Link in Text ----------
    elif update.message.text:
        text = update.message.text
        # Private Channel (t.me/c/123456789/123)
        match = re.search(r't\.me/c/(\d+)/(\d+)', text)
        if match:
            channel_id = match.group(1)
            msg_id = int(match.group(2))
            chat_id = int(f"-100{channel_id}")
        else:
            # Public Channel (t.me/username/123)
            match = re.search(r't\.me/([^/]+)/(\d+)', text)
            if match:
                username = match.group(1)
                msg_id = int(match.group(2))
                try:
                    chat = await context.bot.get_chat(f"@{username}")
                    chat_id = chat.id
                except Exception as e:
                    logger.error(f"Username not found: {e}")
                    await update.message.reply_text(f"❌ `@{username}` Channel ကို ရှာမတွေ့ပါ။")
                    return CUSTOM_BATCH_COLLECT
        
        if chat_id and msg_id:
            try:
                msg = await context.bot.get_messages(chat_id=chat_id, message_ids=msg_id)
                if msg:
                    msg_data = extract_message_data(msg)
            except Exception as e:
                logger.error(f"Message fetch from link error: {e}")
                await update.message.reply_text("❌ ဤ Link မှ မက်ဆေ့ချ်ကို ဖတ်လို့မရပါ။")
                return CUSTOM_BATCH_COLLECT
    
    # ---------- Direct Message (TEXT, VIDEO, PHOTO, DOCUMENT, AUDIO, VOICE) ----------
    else:
        if update.message.text:
            msg_data = {"type": "text", "text": update.message.text}
        elif update.message.video:
            msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
        elif update.message.photo:
            msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
        elif update.message.document:
            msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
        elif update.message.audio:
            msg_data = {"type": "audio", "file_id": update.message.audio.file_id, "caption": update.message.caption or "Audio"}
        elif update.message.voice:
            msg_data = {"type": "voice", "file_id": update.message.voice.file_id, "caption": "Voice"}
        elif update.message.animation:
            msg_data = {"type": "animation", "file_id": update.message.animation.file_id, "caption": update.message.caption or "GIF"}
        else:
            await update.message.reply_text("❌ ဤမက်ဆေ့ချ်အမျိုးအစားကို မသိမ်းဆည်းနိုင်ပါ။")
            return CUSTOM_BATCH_COLLECT
    
    if msg_data:
        context.user_data['custom_batch_list'].append(msg_data)
        await update.message.reply_text(
            f"✅ မက်ဆေ့ချ် #{len(context.user_data['custom_batch_list'])} ကို သိမ်းဆည်းပြီးပါပြီ။\n"
            f"📦 စုစုပေါင်း: {len(context.user_data['custom_batch_list'])} ခု\n"
            f"📌 အမျိုးအစား: {msg_data['type']}\n\n"
            f"ဆက်လက်ထည့်သွင်းနိုင်ပါသည်။ ပြီးပါက `done` ရိုက်ပါ။"
        )
    else:
        await update.message.reply_text("❌ မက်ဆေ့ချ်ကို သိမ်းဆည်းရာတွင် အမှားရှိသည်။")
    
    return CUSTOM_BATCH_COLLECT

async def cancel_custom_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('custom_batch_list', None)
    context.user_data.pop('custom_batch_state', None)
    await update.message.reply_text("❌ Custom Batch ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

# ---------- /special_link (FULL) ----------
SPECIAL_MAIN, SPECIAL_CREATE, SPECIAL_COLLECT, SPECIAL_MODIFY, SPECIAL_DELETE = range(200, 205)

async def special_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("➕ CREATE (အသစ်ပြုလုပ်ရန်)", callback_data="special_create")],
        [InlineKeyboardButton("✏️ MODIFY (ပြင်ဆင်ရန်)", callback_data="special_modify")],
        [InlineKeyboardButton("🗑️ DELETE (ဖျက်ရန်)", callback_data="special_delete")],
        [InlineKeyboardButton("❌ CLOSE (ပိတ်ရန်)", callback_data="special_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔗 **Special Link Management**\n\n"
        "Do you want to create a new special link, or modify an existing one, or delete it?\n\n"
        "သင်သည် Special Link အသစ်ပြုလုပ်လိုသလား၊\n"
        "ရှိပြီးသားတစ်ခုကို ပြင်ဆင်လိုသလား၊ သို့မဟုတ် ဖျက်လိုသလား?\n\n"
        "To know more click [here](https://mdbotz-tutorial.github.io/)",
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    return SPECIAL_MAIN

# ---------- SPECIAL: CREATE ----------
async def special_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    context.user_data['special_messages'] = []
    context.user_data['special_link_id'] = generate_payload()
    context.user_data['special_state'] = 'creating'
    
    await query.edit_message_text(
        "📝 **Special Link အသစ်ပြုလုပ်ရန်**\n\n"
        "သိမ်းဆည်းလိုသော မက်ဆေ့ချ်များကို အောက်ပါအတိုင်း ပို့ပါ။\n"
        "➡️ စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ Document အားလုံး လက်ခံပါသည်။\n"
        "➡️ မက်ဆေ့ချ်တစ်ခုချင်းစီကို တစ်ခါတည်း ပို့ပါ။\n\n"
        "မက်ဆေ့ချ်အားလုံး ထည့်ပြီးပါက `done` လို့ ရိုက်ပါ။"
    )
    return SPECIAL_COLLECT

async def collect_special_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('special_state') != 'creating':
        return
    
    if not is_admin(update.effective_user.id):
        return
    
    # `done` ဆိုရင် စုစည်းပြီး Link ထုတ်ပေးမယ်
    if update.message.text and update.message.text.lower() == 'done':
        messages = context.user_data.get('special_messages', [])
        if not messages:
            await update.message.reply_text("⚠️ မက်ဆေ့ချ် အနည်းဆုံး တစ်ခုတော့ ထည့်သွင်းပေးပါ။")
            return SPECIAL_COLLECT
        
        special_id = context.user_data.get('special_link_id')
        batch_id = f"special_{special_id}"
        
        # Save to database
        save_batch_messages(batch_id, messages)
        
        # Create Telegraph Page
        content_parts = []
        for idx, msg in enumerate(messages, 1):
            if msg['type'] == 'text':
                content_parts.append(f"**{idx}.** {msg['text']}")
            else:
                content_parts.append(f"**{idx}.** [{msg['type']}]: {msg.get('caption', 'Media')}")
        
        full_content = "\n\n---\n\n".join(content_parts)
        title = f"Special Collection - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        try:
            page_url = await create_telegraph_page(title, full_content)
        except:
            page_url = None
        
        # Create Deep Link
        deep_link = create_deep_linked_url(BOT_USERNAME, batch_id)
        
        # Clear state
        context.user_data.pop('special_messages', None)
        context.user_data.pop('special_link_id', None)
        context.user_data.pop('special_state', None)
        
        # Send response
        response_text = (
            f"✅ **Special Link ဖန်တီးပြီးပါပြီ။**\n\n"
            f"📦 သိမ်းဆည်းထားသော မက်ဆေ့ချ်: {len(messages)} ခု\n\n"
            f"🔗 **မျှဝေရန် Link:**\n{deep_link}\n\n"
            f"📄 **Telegraph Page:**\n{page_url if page_url else 'ဖန်တီးရာတွင် အမှားရှိသည်'}\n\n"
            f"✏️ ပြင်ဆင်လိုပါက /special_link ကိုသုံးပြီး MODIFY ကိုနှိပ်ပါ။\n"
            f"🆔 Special ID: `{special_id}`"
        )
        await update.message.reply_text(response_text, parse_mode="Markdown")
        return ConversationHandler.END
    
    # Collect message (ALL TYPES)
    msg_data = None
    if update.message.text:
        msg_data = {"type": "text", "text": update.message.text}
    elif update.message.video:
        msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
    elif update.message.photo:
        msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
    elif update.message.document:
        msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
    elif update.message.audio:
        msg_data = {"type": "audio", "file_id": update.message.audio.file_id, "caption": update.message.caption or "Audio"}
    elif update.message.voice:
        msg_data = {"type": "voice", "file_id": update.message.voice.file_id, "caption": "Voice"}
    elif update.message.animation:
        msg_data = {"type": "animation", "file_id": update.message.animation.file_id, "caption": update.message.caption or "GIF"}
    else:
        await update.message.reply_text("❌ ဤမက်ဆေ့ချ်အမျိုးအစားကို မသိမ်းဆည်းနိုင်ပါ။")
        return SPECIAL_COLLECT
    
    if msg_data:
        context.user_data['special_messages'].append(msg_data)
        await update.message.reply_text(
            f"✅ မက်ဆေ့ချ် #{len(context.user_data['special_messages'])} ကို သိမ်းဆည်းပြီးပါပြီ။\n"
            f"📦 စုစုပေါင်း: {len(context.user_data['special_messages'])} ခု\n\n"
            f"ဆက်လက်ထည့်သွင်းနိုင်ပါသည်။ ပြီးပါက `done` ရိုက်ပါ။"
        )
    else:
        await update.message.reply_text("❌ မက်ဆေ့ချ်ကို သိမ်းဆည်းရာတွင် အမှားရှိသည်။")
    
    return SPECIAL_COLLECT

# ---------- SPECIAL: MODIFY ----------
async def special_modify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    # Get all special links from database
    special_links = batch_collection.find({"batch_id": {"$regex": "^special_"}})
    links_list = list(special_links)
    
    if not links_list:
        await query.edit_message_text(
            "📭 **Special Link မတွေ့ပါ**\n\n"
            "Special Link တစ်ခုမှ မရှိသေးပါ။\n"
            "အသစ်ပြုလုပ်လိုပါက 'CREATE' ကိုနှိပ်ပါ။"
        )
        return SPECIAL_MAIN
    
    # Show list of special links
    keyboard = []
    for doc in links_list[:20]:  # Limit to 20
        special_id = doc['batch_id'].replace('special_', '')
        msg_count = len(doc.get('messages', []))
        created = doc.get('created_at', datetime.now()).strftime('%Y-%m-%d %H:%M')
        button_text = f"📌 {special_id[:8]}... ({msg_count} msgs) - {created}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"modify_{special_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="special_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "✏️ **ပြင်ဆင်လိုသော Special Link ကို ရွေးပါ**\n\n"
        "အောက်ပါစာရင်းမှ ပြင်ဆင်လိုသော Link ကို နှိပ်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_MODIFY

async def special_modify_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('modify_', '')
    batch_id = f"special_{special_id}"
    
    doc = batch_collection.find_one({"batch_id": batch_id})
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MAIN
    
    messages = doc.get('messages', [])
    context.user_data['modify_special_id'] = special_id
    context.user_data['modify_messages'] = messages.copy()
    
    # Show current messages and options
    msg_list = ""
    for idx, msg in enumerate(messages, 1):
        if msg['type'] == 'text':
            preview = msg['text'][:50] + "..." if len(msg['text']) > 50 else msg['text']
            msg_list += f"{idx}. 📝 {preview}\n"
        else:
            msg_list += f"{idx}. 🎬 {msg.get('caption', msg['type'])}\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add More", callback_data="modify_add")],
        [InlineKeyboardButton("🗑️ Remove Message", callback_data="modify_remove")],
        [InlineKeyboardButton("🔄 Regenerate Link", callback_data="modify_regenerate")],
        [InlineKeyboardButton("🔙 Back", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✏️ **ပြင်ဆင်နေသည်: {special_id}**\n\n"
        f"📦 မက်ဆေ့ချ် {len(messages)} ခု\n\n"
        f"{msg_list}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_MODIFY

async def modify_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data['modify_state'] = 'adding'
    await query.edit_message_text(
        "➕ **မက်ဆေ့ချ်အသစ်ထည့်ရန်**\n\n"
        "ထည့်သွင်းလိုသော မက်ဆေ့ချ်ကို ပို့ပါ။\n"
        "(စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ Document အားလုံး လက်ခံပါသည်)\n\n"
        "ပြီးပါက `done_add` ရိုက်ပါ။"
    )
    return SPECIAL_MODIFY

async def modify_add_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('modify_state') != 'adding':
        return
    
    if update.message.text and update.message.text.lower() == 'done_add':
        # Save updated messages
        special_id = context.user_data.get('modify_special_id')
        messages = context.user_data.get('modify_messages', [])
        batch_id = f"special_{special_id}"
        
        batch_collection.update_one(
            {"batch_id": batch_id},
            {"$set": {"messages": messages, "updated_at": datetime.now()}},
            upsert=True
        )
        
        context.user_data.pop('modify_state', None)
        
        await update.message.reply_text(
            f"✅ မက်ဆေ့ချ် {len(messages)} ခုကို သိမ်းဆည်းပြီးပါပြီ။\n"
            f"ပြန်လည်ပြင်ဆင်ရန် /special_link ကိုသုံးပါ။"
        )
        return ConversationHandler.END
    
    # Collect message
    msg_data = None
    if update.message.text:
        msg_data = {"type": "text", "text": update.message.text}
    elif update.message.video:
        msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
    elif update.message.photo:
        msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
    elif update.message.document:
        msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
    elif update.message.audio:
        msg_data = {"type": "audio", "file_id": update.message.audio.file_id, "caption": update.message.caption or "Audio"}
    elif update.message.voice:
        msg_data = {"type": "voice", "file_id": update.message.voice.file_id, "caption": "Voice"}
    elif update.message.animation:
        msg_data = {"type": "animation", "file_id": update.message.animation.file_id, "caption": update.message.caption or "GIF"}
    else:
        await update.message.reply_text("❌ ဤမက်ဆေ့ချ်အမျိုးအစားကို မထည့်နိုင်ပါ။")
        return SPECIAL_MODIFY
    
    if msg_data:
        context.user_data['modify_messages'].append(msg_data)
        await update.message.reply_text(
            f"✅ မက်ဆေ့ချ် #{len(context.user_data['modify_messages'])} ကို ထည့်ပြီးပါပြီ။\n"
            f"ဆက်ထည့်လိုပါက ထပ်ပို့ပါ။ ပြီးပါက `done_add` ရိုက်ပါ။"
        )
    else:
        await update.message.reply_text("❌ မက်ဆေ့ချ်ကို သိမ်းဆည်းရာတွင် အမှားရှိသည်။")
    
    return SPECIAL_MODIFY

async def modify_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    messages = context.user_data.get('modify_messages', [])
    if not messages:
        await query.edit_message_text("❌ ဖယ်ရှားရန် မက်ဆေ့ချ် မရှိပါ။")
        return SPECIAL_MODIFY
    
    # Show list to remove
    keyboard = []
    for idx, msg in enumerate(messages, 1):
        if msg['type'] == 'text':
            preview = msg['text'][:30] + "..." if len(msg['text']) > 30 else msg['text']
            button_text = f"{idx}. 📝 {preview}"
        else:
            button_text = f"{idx}. 🎬 {msg.get('caption', msg['type'])}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"remove_{idx}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="modify_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🗑️ **ဖယ်ရှားလိုသော မက်ဆေ့ချ်ကို ရွေးပါ**",
        reply_markup=reply_markup
    )
    return SPECIAL_MODIFY

async def modify_remove_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    idx = int(query.data.replace('remove_', '')) - 1
    messages = context.user_data.get('modify_messages', [])
    
    if 0 <= idx < len(messages):
        removed = messages.pop(idx)
        context.user_data['modify_messages'] = messages
        
        # Update database
        special_id = context.user_data.get('modify_special_id')
        batch_id = f"special_{special_id}"
        batch_collection.update_one(
            {"batch_id": batch_id},
            {"$set": {"messages": messages, "updated_at": datetime.now()}},
            upsert=True
        )
        
        await query.edit_message_text(
            f"✅ မက်ဆေ့ချ် #{idx+1} ကို ဖယ်ရှားပြီးပါပြီ။\n"
            f"📦 ကျန်ရှိသော မက်ဆေ့ချ်: {len(messages)} ခု\n\n"
            f"ဆက်လက်ဖယ်ရှားလိုပါက ထပ်ရွေးပါ။"
        )
    else:
        await query.edit_message_text("❌ မှားယွင်းသော ရွေးချယ်မှုပါ။")
    
    return SPECIAL_MODIFY

async def modify_regenerate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    messages = context.user_data.get('modify_messages', [])
    special_id = context.user_data.get('modify_special_id')
    
    if not messages:
        await query.edit_message_text("❌ Regenerate လုပ်ရန် မက်ဆေ့ချ် မရှိပါ။")
        return SPECIAL_MODIFY
    
    # Create Telegraph Page
    content_parts = []
    for idx, msg in enumerate(messages, 1):
        if msg['type'] == 'text':
            content_parts.append(f"**{idx}.** {msg['text']}")
        else:
            content_parts.append(f"**{idx}.** [{msg['type']}]: {msg.get('caption', 'Media')}")
    
    full_content = "\n\n---\n\n".join(content_parts)
    title = f"Special Collection - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    try:
        page_url = await create_telegraph_page(title, full_content)
        deep_link = create_deep_linked_url(BOT_USERNAME, f"special_{special_id}")
        
        await query.edit_message_text(
            f"✅ **Special Link ကို Regenerate လုပ်ပြီးပါပြီ။**\n\n"
            f"🔗 **မျှဝေရန် Link:**\n{deep_link}\n\n"
            f"📄 **Telegraph Page:**\n{page_url if page_url else 'ဖန်တီးရာတွင် အမှားရှိသည်'}\n\n"
            f"📦 မက်ဆေ့ချ် {len(messages)} ခု",
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Regenerate လုပ်ရာတွင် အမှားရှိသည်: {str(e)}")
    
    return SPECIAL_MODIFY

# ---------- SPECIAL: DELETE ----------
async def special_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    # Get all special links
    special_links = batch_collection.find({"batch_id": {"$regex": "^special_"}})
    links_list = list(special_links)
    
    if not links_list:
        await query.edit_message_text(
            "📭 **Special Link မတွေ့ပါ**\n\n"
            "ဖျက်ရန် Special Link မရှိပါ။"
        )
        return SPECIAL_MAIN
    
    keyboard = []
    for doc in links_list[:20]:
        special_id = doc['batch_id'].replace('special_', '')
        msg_count = len(doc.get('messages', []))
        button_text = f"🗑️ {special_id[:8]}... ({msg_count} msgs)"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_{special_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="special_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🗑️ **ဖျက်လိုသော Special Link ကို ရွေးပါ**\n\n"
        "သတိပြုရန် - ဖျက်လိုက်ပါက ပြန်ယူလို့မရနိုင်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_DELETE

async def special_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('delete_', '')
    batch_id = f"special_{special_id}"
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{special_id}")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ **သေချာပါသလား?**\n\n"
        f"Special Link `{special_id}` ကို ဖျက်တော့မည်။\n"
        f"ဖျက်လိုက်ပါက ပြန်ယူလို့မရနိုင်ပါ။\n\n"
        f"ဆက်လက်လုပ်ဆောင်မည်ဆိုပါက 'Yes, Delete' ကိုနှိပ်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_DELETE

async def special_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('confirm_delete_', '')
    batch_id = f"special_{special_id}"
    
    result = batch_collection.delete_one({"batch_id": batch_id})
    
    if result.deleted_count > 0:
        await query.edit_message_text(
            f"✅ **Special Link ကို ဖျက်ပြီးပါပြီ။**\n\n"
            f"🆔 ID: `{special_id}`\n"
            f"အောင်မြင်စွာ ဖျက်ဆီးခဲ့ပါသည်။"
        )
    else:
        await query.edit_message_text(f"❌ Special Link `{special_id}` ကို ရှာမတွေ့ပါ။")
    
    return ConversationHandler.END

# ---------- SPECIAL: Close and Back ----------
async def special_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔒 **Special Link Menu ကိုပိတ်လိုက်ပါပြီ။**")
    return ConversationHandler.END

async def special_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ CREATE (အသစ်ပြုလုပ်ရန်)", callback_data="special_create")],
        [InlineKeyboardButton("✏️ MODIFY (ပြင်ဆင်ရန်)", callback_data="special_modify")],
        [InlineKeyboardButton("🗑️ DELETE (ဖျက်ရန်)", callback_data="special_delete")],
        [InlineKeyboardButton("❌ CLOSE (ပိတ်ရန်)", callback_data="special_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔗 **Special Link Management**\n\n"
        "Do you want to create a new special link, or modify an existing one, or delete it?\n\n"
        "သင်သည် Special Link အသစ်ပြုလုပ်လိုသလား၊\n"
        "ရှိပြီးသားတစ်ခုကို ပြင်ဆင်လိုသလား၊ သို့မဟုတ် ဖျက်လိုသလား?",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SPECIAL_MAIN

async def cancel_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('special_messages', None)
    context.user_data.pop('special_link_id', None)
    context.user_data.pop('special_state', None)
    context.user_data.pop('modify_special_id', None)
    context.user_data.pop('modify_messages', None)
    context.user_data.pop('modify_state', None)
    await update.message.reply_text("❌ Special Link လုပ်ဆောင်ချက်ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

# ---------- /shortener (NEW) ----------
async def shortener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return
    
    if not context.args:
        await update.message.reply_text("❌ /shortener <URL> ပုံစံဖြင့် သုံးပါ။\nဥပမာ: /shortener https://example.com/long/url")
        return
    
    url = context.args[0]
    if not re.match(r'^https?://', url):
        await update.message.reply_text("❌ URL သည် http:// သို့မဟုတ် https:// ဖြင့် စတင်ရပါမည်။")
        return
    
    short_code = secrets.token_urlsafe(6)
    save_short_link(short_code, url)
    short_link = create_deep_linked_url(BOT_USERNAME, f"short_{short_code}")
    
    await update.message.reply_text(
        f"✅ အတိုချုံ့ပြီးပါပြီ။\n\n"
        f"🔗 **အတိုချုံ့ထားသော Link:**\n{short_link}\n\n"
        f"📌 မူရင်း URL: {url}"
    )

# =============================================================================
# END OF NEW COMMANDS
# =============================================================================

# ---------- Existing Admin Commands ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_collection.count_documents({})
    total_files = file_store_collection.count_documents({})
    await update.message.reply_text(
        f"📊 **စာရင်းအင်း**\n\n"
        f"👥 အသုံးပြုသူဦးရေ: {total_users}\n"
        f"🎬 ဖိုင်အရေအတွက်: {total_files}",
        parse_mode="Markdown"
    )

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

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⏳ Schedule ပြုလုပ်ရန် (လုပ်ဆောင်ဆဲ)")

async def listschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📋 Schedule စာရင်း (လုပ်ဆောင်ဆဲ)")

async def cancelschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("❌ Schedule ဖျက်ရန် (လုပ်ဆောင်ဆဲ)")

async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🗑️ /delete <file_id> ဖြင့် ဖိုင်ဖျက်ပါ။")

async def deleteall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("⚠️ ဖိုင်အားလုံးဖျက်ရန် (အတည်ပြုရန် /done ရိုက်ပါ)")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")

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

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ သင်သည် Admin မဟုတ်ပါ။")
        return
    await show_menu(update, context)

# ---------- Set Bot Commands (UPDATED) ----------
async def set_commands(application: Application):
    await application.bot.set_my_commands([
        ("start", "Bot ကိုစတင်ရန်"),
        ("newpost", "ပို့စ်အသစ်ဖန်တီးရန် (ပုံ+စာ+Video)"),
        ("link", "Video အတွက် Deep Link ထုတ်ရန်"),
        ("custom_batch", "မတူညီသော မက်ဆေ့ချ်များကို စုစည်းရန်"),
        ("special_link", "Special Link ပြုလုပ်ရန်/ပြင်ဆင်ရန်/ဖျက်ရန်"),
        ("shortener", "URL အတိုချုံ့ရန်"),
        ("stats", "စာရင်းအင်းကြည့်ရန်"),
        ("broadcast", "အသုံးပြုသူအားလုံးကို စာပို့ရန်"),
        ("menu", "Admin Menu ပြသရန်"),
        ("mute", "Maintenance mode ဖွင့်ရန်"),
        ("unmute", "Maintenance mode ပိတ်ရန်")
    ])

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# ---------- Conversation Handlers ----------
newpost_handler = ConversationHandler(
    entry_points=[CommandHandler('newpost', newpost_start)],
    states={
        POSTER: [MessageHandler(filters.PHOTO, receive_poster)],
        CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption)],
        WAITING_VIDEO: [
            MessageHandler(filters.VIDEO, receive_video_after_caption),
            MessageHandler(filters.Document.ALL, receive_video_after_caption)
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_newpost)],
)

custom_batch_handler = ConversationHandler(
    entry_points=[CommandHandler('custom_batch', custom_batch_start)],
    states={
        CUSTOM_BATCH_COLLECT: [
            MessageHandler(filters.ALL, collect_custom_batch_messages),
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_custom_batch)],
)

special_link_handler = ConversationHandler(
    entry_points=[CommandHandler('special_link', special_link_start)],
    states={
        SPECIAL_MAIN: [
            CallbackQueryHandler(special_create_callback, pattern="special_create"),
            CallbackQueryHandler(special_modify_callback, pattern="special_modify"),
            CallbackQueryHandler(special_delete_callback, pattern="special_delete"),
            CallbackQueryHandler(special_close_callback, pattern="special_close"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_CREATE: [
            CallbackQueryHandler(special_create_callback, pattern="special_create")
        ],
        SPECIAL_COLLECT: [
            MessageHandler(filters.ALL, collect_special_messages),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_MODIFY: [
            CallbackQueryHandler(special_modify_select, pattern="^modify_"),
            CallbackQueryHandler(modify_add_callback, pattern="modify_add"),
            CallbackQueryHandler(modify_remove_callback, pattern="modify_remove"),
            CallbackQueryHandler(modify_regenerate_callback, pattern="modify_regenerate"),
            CallbackQueryHandler(special_back_callback, pattern="special_back"),
            CallbackQueryHandler(special_back_callback, pattern="modify_back"),
            MessageHandler(filters.ALL, modify_add_collect),
        ],
        SPECIAL_DELETE: [
            CallbackQueryHandler(special_delete_confirm, pattern="^delete_"),
            CallbackQueryHandler(special_delete_execute, pattern="^confirm_delete_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_special)],
)

# ---------- Add Handlers ----------
application.add_handler(CommandHandler("start", start))
application.add_handler(newpost_handler)
application.add_handler(CommandHandler("link", link_command))
application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, handle_video_for_link))
application.add_handler(CommandHandler("menu", menu_command))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("broadcast", broadcast))
application.add_handler(CommandHandler("schedule", schedule))
application.add_handler(CommandHandler("listschedule", listschedule))
application.add_handler(CommandHandler("cancelschedule", cancelschedule))
application.add_handler(CommandHandler("delete", delete_file))
application.add_handler(CommandHandler("deleteall", deleteall))
application.add_handler(CommandHandler("cancel", cancel))
application.add_handler(CommandHandler("mute", mute))
application.add_handler(CommandHandler("unmute", unmute))
application.add_handler(CommandHandler("shortener", shortener_command))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

# Add NEW handlers
application.add_handler(custom_batch_handler)
application.add_handler(special_link_handler)

# ---------- Polling ----------
def run_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(set_commands(application))
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
