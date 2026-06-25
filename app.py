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

# ---- New Collections for new commands ----
batch_collection = db["batch_messages"]       # for /batch and /universal_link
short_links_collection = db["short_links"]    # for /shortener
# Note: /special_link uses Telegraph directly and doesn't store in DB persistently (except user data)

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

# ---------- Helpers for New Commands ----------

# 1. Batch / Universal storage
def save_batch_messages(batch_id, messages_data):
    # messages_data is a list of dicts: {type, text, file_id, caption}
    batch_collection.update_one(
        {"batch_id": batch_id},
        {"$set": {"messages": messages_data, "created_at": datetime.now()}},
        upsert=True
    )

def get_batch_messages(batch_id):
    doc = batch_collection.find_one({"batch_id": batch_id})
    return doc["messages"] if doc else None

def save_universal_messages(payload, messages_data):
    # Reusing the same collection but with a different flag or just same structure
    batch_collection.update_one(
        {"batch_id": f"uni_{payload}"},
        {"$set": {"messages": messages_data, "created_at": datetime.now(), "type": "universal"}},
        upsert=True
    )

def get_universal_messages(payload):
    doc = batch_collection.find_one({"batch_id": f"uni_{payload}"})
    return doc["messages"] if doc else None

# 2. Shortener
def save_short_link(short_code, original_url):
    short_links_collection.update_one(
        {"short_code": short_code},
        {"$set": {"original_url": original_url, "created_at": datetime.now()}},
        upsert=True
    )

def get_original_url(short_code):
    doc = short_links_collection.find_one({"short_code": short_code})
    return doc["original_url"] if doc else None

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
        
        # --- 1. Check for SHORTENER link (short_xxx) ---
        if payload.startswith('short_'):
            short_code = payload[6:]
            original_url = get_original_url(short_code)
            if original_url:
                await update.message.reply_text(
                    f"🔗 မူရင်း Link ပါ။\n\n{original_url}"
                )
            else:
                await update.message.reply_text("❌ ဤအတိုချုံ့ထားသော Link သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        
        # --- 2. Check for BATCH / UNIVERSAL link ---
        if payload.startswith('batch_') or payload.startswith('uni_'):
            batch_id = payload
            messages = get_batch_messages(batch_id) if payload.startswith('batch_') else get_universal_messages(payload[4:])
            if not messages:
                # fallback to old file check
                file_info = get_file_info(payload)
                if file_info:
                    messages = [{"type": "video", "file_id": file_info["file_id"], "caption": file_info["file_name"]}]
                else:
                    await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
                    return
            
            # Check channel membership for batch/uni links
            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 Channel သို့ဝင်ရန်: {INVITE_LINK}",
                    disable_web_page_preview=True
                )
                return
            
            add_user(user_id)
            increment_requests()
            
            await update.message.reply_text(f"📦 စုစည်းထားသော အကြောင်းအရာ {len(messages)} ခုကို ပို့ပေးနေပါပြီ...")
            
            # Send each message in batch
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
                    else:
                        await context.bot.send_message(chat_id=user_id, text=msg.get('text', 'Unknown message type'))
                except Exception as e:
                    logger.error(f"Error sending batch item: {e}")
            
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
        
        # --- 3. Existing single file deep link ---
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

# ---------- Admin Menu ----------
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🆕 ပို့စ်အသစ်", callback_data="menu_newpost")],
        [InlineKeyboardButton("🔗 Video → Deep Link", callback_data="menu_link")],
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

# ---------- 🆕 NEW COMMANDS IMPLEMENTATION ----------

# ========== 1. /batch ==========
async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return
    
    # Get count from args, default 5
    limit = 5
    if context.args and context.args[0].isdigit():
        limit = int(context.args[0])
        if limit > 50:
            limit = 50
    
    await update.message.reply_text(f"⏳ Channel ထဲက နောက်ဆုံး {limit} ခုကို စုစည်းနေပါသည်...")
    
    try:
        messages = []
        async for msg in context.bot.get_chat_history(chat_id=CHANNEL_ID, limit=limit):
            msg_data = {"type": "text", "text": f"Message ID: {msg.message_id}"}
            if msg.text:
                msg_data["text"] = msg.text
            elif msg.caption:
                msg_data["text"] = msg.caption
            
            if msg.video:
                msg_data["type"] = "video"
                msg_data["file_id"] = msg.video.file_id
                msg_data["caption"] = msg.caption or "Video"
            elif msg.photo:
                msg_data["type"] = "photo"
                msg_data["file_id"] = msg.photo[-1].file_id
                msg_data["caption"] = msg.caption or "Photo"
            elif msg.document:
                msg_data["type"] = "document"
                msg_data["file_id"] = msg.document.file_id
                msg_data["caption"] = msg.caption or "Document"
            elif msg.text:
                msg_data["type"] = "text"
                msg_data["text"] = msg.text
            else:
                continue  # skip unsupported types
            
            messages.append(msg_data)
        
        if not messages:
            await update.message.reply_text("❌ Channel ထဲမှာ မက်ဆေ့ချ် မတွေ့ပါ။")
            return
        
        batch_id = f"batch_{generate_payload()}"
        save_batch_messages(batch_id, messages)
        deep_link = create_deep_linked_url(BOT_USERNAME, batch_id)
        
        await update.message.reply_text(
            f"✅ မက်ဆေ့ချ် {len(messages)} ခုကို စုစည်းပြီးပါပြီ။\n\n"
            f"🔗 ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် အားလုံးကို ရရှိမည်။\n{deep_link}"
        )
    except Exception as e:
        logger.error(f"Batch error: {e}")
        await update.message.reply_text(f"❌ Batch ပြုလုပ်ရာတွင် အမှားရှိသည်: {str(e)}")

# ========== 2. /custom_batch (Conversation) ==========
CUSTOM_BATCH_COLLECT = 10

async def custom_batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    context.user_data['custom_batch_list'] = []
    await update.message.reply_text(
        "📦 Custom Batch အတွက် မက်ဆေ့ချ်များကို Forward (သို့) ရိုက်ထည့်ပေးပါ။\n"
        "ပြီးပါက `done` လို့ ရိုက်ပေးပါ။"
    )
    return CUSTOM_BATCH_COLLECT

async def collect_custom_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() == 'done':
        # Finish collecting
        messages = context.user_data.get('custom_batch_list', [])
        if not messages:
            await update.message.reply_text("⚠️ မက်ဆေ့ချ် အနည်းဆုံး တစ်ခုတော့ ပို့ပေးပါ။")
            return CUSTOM_BATCH_COLLECT
        
        batch_id = f"batch_{generate_payload()}"
        save_batch_messages(batch_id, messages)
        deep_link = create_deep_linked_url(BOT_USERNAME, batch_id)
        context.user_data.pop('custom_batch_list', None)
        
        await update.message.reply_text(
            f"✅ Custom Batch ပြီးပါပြီ။ မက်ဆေ့ချ် {len(messages)} ခု သိမ်းဆည်းထားပါသည်။\n\n"
            f"🔗 ဤလင့်ကို သုံးပါ။\n{deep_link}"
        )
        return ConversationHandler.END
    
    # Capture message
    msg_data = None
    if update.message.text:
        msg_data = {"type": "text", "text": update.message.text}
    elif update.message.video:
        msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
    elif update.message.photo:
        msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
    elif update.message.document:
        msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
    
    if msg_data:
        context.user_data['custom_batch_list'].append(msg_data)
        await update.message.reply_text(f"✅ မက်ဆေ့ချ် {len(context.user_data['custom_batch_list'])} ကို လက်ခံရရှိပါပြီ။ ဆက်ပို့ပါ (သို့) `done` ရိုက်ပါ။")
    else:
        await update.message.reply_text("❌ စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ သို့မဟုတ် Document ကိုသာ လက်ခံပါသည်။")
    
    return CUSTOM_BATCH_COLLECT

async def cancel_custom_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('custom_batch_list', None)
    await update.message.reply_text("Custom Batch ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

# ========== 3. /shortener ==========
async def shortener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return
    
    if not context.args:
        await update.message.reply_text("❌ /shortener <URL> ပုံစံဖြင့် သုံးပါ။\nဥပမာ: /shortener https://example.com/long/url")
        return
    
    url = context.args[0]
    # Basic URL validation
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

# ========== 4. /special_link (Telegraph + Editable) ==========
SPECIAL_LINK_COLLECT = 20

async def special_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    context.user_data['special_texts'] = []
    await update.message.reply_text(
        "📝 Special Link (Telegraph) အတွက် စာသားများကို ပို့ပေးပါ။\n"
        "(ပုံ/ဗီဒီယို မပါဘဲ စာသားသက်သက်သာ လက်ခံပါမည်)\n"
        "ပြီးပါက `done` ရိုက်ပေးပါ။"
    )
    return SPECIAL_LINK_COLLECT

async def collect_special_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() == 'done':
        texts = context.user_data.get('special_texts', [])
        if not texts:
            await update.message.reply_text("⚠️ စာသား အနည်းဆုံး တစ်ခုတော့ ပို့ပေးပါ။")
            return SPECIAL_LINK_COLLECT
        
        full_content = "\n\n---\n\n".join(texts)
        title = f"Special Collection - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        try:
            page_url = await create_telegraph_page(title, full_content)
            if page_url:
                # Try to get edit link (Telegraph doesn't give a direct "edit link" easily without token,
                # but we can tell them to edit via telegraph account if they login with same account)
                # We'll just return the page url. They can edit if they are logged in.
                await update.message.reply_text(
                    f"✅ **Special Link (Telegraph Page) ဖန်တီးပြီးပါပြီ။**\n\n"
                    f"🔗 **ကြည့်ရှုရန် Link:**\n{page_url}\n\n"
                    f"✏️ ဤစာမျက်နှာကို ပြင်ဆင်လိုပါက **Telegraph** အကောင့်ဖြင့် ဝင်ပြီး Edit လုပ်နိုင်ပါသည်။ "
                    f"(သို့မဟုတ် ကျွန်ုပ်၏ Dashboard မှတဆင့် စီမံခန့်ခွဲနိုင်ပါသည်)"
                )
            else:
                await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် အမှားရှိသည်။")
        except Exception as e:
            logger.error(f"Special link error: {e}")
            await update.message.reply_text(f"❌ ချို့ယွင်းချက်ရှိသည်: {str(e)}")
        
        context.user_data.pop('special_texts', None)
        return ConversationHandler.END
    
    if update.message.text:
        context.user_data['special_texts'].append(update.message.text)
        await update.message.reply_text(f"✅ စာသား {len(context.user_data['special_texts'])} ကို လက်ခံရရှိပါပြီ။ ဆက်ပို့ပါ (သို့) `done` ရိုက်ပါ။")
    else:
        await update.message.reply_text("❌ စာသားသာ လက်ခံပါသည်။ (ပုံ/ဗီဒီယို မပို့ပါနှင့်)")
    
    return SPECIAL_LINK_COLLECT

async def cancel_special_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('special_texts', None)
    await update.message.reply_text("Special Link ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

# ========== 5. /universal_link (MongoDB based, accessible from all clones) ==========
UNIVERSAL_LINK_COLLECT = 30

async def universal_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    context.user_data['universal_list'] = []
    await update.message.reply_text(
        "🌐 Universal Link အတွက် မက်ဆေ့ချ်များကို Forward (သို့) ရိုက်ထည့်ပေးပါ။\n"
        "(ဒီ Link ကို ဘယ် Bot Clone ကမဆို သုံးလို့ရမည်)\n"
        "ပြီးပါက `done` ရိုက်ပေးပါ။"
    )
    return UNIVERSAL_LINK_COLLECT

async def collect_universal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() == 'done':
        messages = context.user_data.get('universal_list', [])
        if not messages:
            await update.message.reply_text("⚠️ မက်ဆေ့ချ် အနည်းဆုံး တစ်ခုတော့ ပို့ပေးပါ။")
            return UNIVERSAL_LINK_COLLECT
        
        payload = generate_payload()
        save_universal_messages(payload, messages)
        deep_link = create_deep_linked_url(BOT_USERNAME, f"uni_{payload}")
        context.user_data.pop('universal_list', None)
        
        await update.message.reply_text(
            f"✅ **Universal Link** ပြုလုပ်ပြီးပါပြီ။\n\n"
            f"🔗 ဤလင့်ကို ဘယ် Bot Clone ကမဆို အလုပ်လုပ်ပါမည်။\n{deep_link}\n\n"
            f"📦 သိမ်းဆည်းထားသော မက်ဆေ့ချ် {len(messages)} ခု။"
        )
        return ConversationHandler.END
    
    msg_data = None
    if update.message.text:
        msg_data = {"type": "text", "text": update.message.text}
    elif update.message.video:
        msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
    elif update.message.photo:
        msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
    elif update.message.document:
        msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
    
    if msg_data:
        context.user_data['universal_list'].append(msg_data)
        await update.message.reply_text(f"✅ မက်ဆေ့ချ် {len(context.user_data['universal_list'])} ကို လက်ခံရရှိပါပြီ။ ဆက်ပို့ပါ (သို့) `done` ရိုက်ပါ။")
    else:
        await update.message.reply_text("❌ စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ သို့မဟုတ် Document ကိုသာ လက်ခံပါသည်။")
    
    return UNIVERSAL_LINK_COLLECT

async def cancel_universal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('universal_list', None)
    await update.message.reply_text("Universal Link ကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

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

# ---------- Set Bot Commands ----------
async def set_commands(application: Application):
    await application.bot.set_my_commands([
        ("start", "Bot ကိုစတင်ရန်"),
        ("newpost", "ပို့စ်အသစ်ဖန်တီးရန် (ပုံ+စာ+Video)"),
        ("link", "Video အတွက် Deep Link ထုတ်ရန်"),
        ("batch", "Channel ထဲက မက်ဆေ့ချ်များကို စုစည်းရန်"),
        ("custom_batch", "မတူညီသော မက်ဆေ့ချ်များကို စုစည်းရန်"),
        ("shortener", "URL အတိုချုံ့ရန်"),
        ("special_link", "Telegraph Page + Editable Link ထုတ်ရန်"),
        ("universal_link", "Clone အားလုံးသုံးနိုင်သော Link ထုတ်ရန်"),
        ("stats", "စာရင်းအင်းကြည့်ရန်"),
        ("broadcast", "အသုံးပြုသူအားလုံးကို စာပို့ရန်"),
        ("menu", "Admin Menu ပြသရန်"),
        ("mute", "Maintenance mode ဖွင့်ရန်"),
        ("unmute", "Maintenance mode ပိတ်ရန်")
    ])

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# --- Conversation Handlers ---
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
        CUSTOM_BATCH_COLLECT: [MessageHandler(filters.TEXT | filters.VIDEO | filters.PHOTO | filters.Document.ALL, collect_custom_batch)],
    },
    fallbacks=[CommandHandler('cancel', cancel_custom_batch)],
)

special_link_handler = ConversationHandler(
    entry_points=[CommandHandler('special_link', special_link_start)],
    states={
        SPECIAL_LINK_COLLECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_special_link)],
    },
    fallbacks=[CommandHandler('cancel', cancel_special_link)],
)

universal_link_handler = ConversationHandler(
    entry_points=[CommandHandler('universal_link', universal_link_start)],
    states={
        UNIVERSAL_LINK_COLLECT: [MessageHandler(filters.TEXT | filters.VIDEO | filters.PHOTO | filters.Document.ALL, collect_universal)],
    },
    fallbacks=[CommandHandler('cancel', cancel_universal)],
)

# --- Add Handlers ---
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
application.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

# --- Add NEW Handlers ---
application.add_handler(CommandHandler("batch", batch_command))
application.add_handler(custom_batch_handler)
application.add_handler(CommandHandler("shortener", shortener_command))
application.add_handler(special_link_handler)
application.add_handler(universal_link_handler)

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
