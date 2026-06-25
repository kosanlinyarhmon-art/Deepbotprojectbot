import os
import asyncio
import threading
import logging
import sys
import secrets
import re
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes, 
    ConversationHandler, 
    MessageHandler, 
    filters, 
    CallbackQueryHandler
)
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
batch_collection = db["batch_messages"]

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

# ---------- Special Link Helpers ----------
def save_special_link(special_id, messages, whitelisters=None, whitelist_enabled=False, protect_content=False, auto_expire=None):
    if whitelisters is None:
        whitelisters = []
    batch_collection.update_one(
        {"batch_id": f"special_{special_id}"},
        {"$set": {
            "messages": messages,
            "whitelisters": whitelisters,
            "whitelist_enabled": whitelist_enabled,
            "protect_content": protect_content,
            "auto_expire": auto_expire,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }},
        upsert=True
    )

def get_special_link(special_id):
    doc = batch_collection.find_one({"batch_id": f"special_{special_id}"})
    return doc if doc else None

def get_all_special_links():
    return list(batch_collection.find({"batch_id": {"$regex": "^special_"}}))

def delete_special_link(special_id):
    batch_collection.delete_one({"batch_id": f"special_{special_id}"})

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

# ---------- Start & Deep Link Handler ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args and len(context.args) > 0:
        payload = context.args[0]
        
        # ---------- Check for Special Link ----------
        if payload.startswith('special_'):
            special_id = payload.replace('special_', '')
            doc = get_special_link(special_id)
            
            if not doc:
                await update.message.reply_text("❌ ဤလင့်သည် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
                return
            
            # Check Auto Expire
            expire_time = doc.get('auto_expire')
            if expire_time:
                expire_dt = datetime.fromisoformat(expire_time)
                if datetime.now() > expire_dt:
                    await update.message.reply_text("❌ ဤ Link သည် သက်တမ်းကုန်သွားပါပြီ။")
                    return
            
            # Check Whitelisters
            if doc.get('whitelist_enabled', False):
                whitelisters = doc.get('whitelisters', [])
                if str(user_id) not in whitelisters and not is_admin(user_id):
                    await update.message.reply_text("❌ သင်သည် ဤ Link ကို ကြည့်ရှုခွင့် မရှိပါ။")
                    return
            
            # Check Channel membership
            if not await is_member(user_id, context):
                await update.message.reply_text(
                    f"❌ ခင်ဗျား Channel ကို မဝင်ရသေးပါ။\n\n👉 Channel သို့ဝင်ရန်: {INVITE_LINK}",
                    disable_web_page_preview=True
                )
                return
            
            add_user(user_id)
            increment_requests()
            
            messages = doc.get('messages', [])
            await update.message.reply_text(f"📦 စုစည်းထားသော အကြောင်းအရာ {len(messages)} ခုကို ပို့ပေးနေပါပြီ...")
            
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
                        await context.bot.send_voice(chat_id=user_id, voice=msg['file_id'])
                    else:
                        await context.bot.send_message(chat_id=user_id, text=msg.get('text', 'Unknown message type'))
                except Exception as e:
                    logger.error(f"Error sending special item: {e}")
            
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
        
        # ---------- Single File Deep Link ----------
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
        [InlineKeyboardButton("📦 Batch ပြုလုပ်ရန်", callback_data="menu_batch")],
        [InlineKeyboardButton("📦 Custom Batch", callback_data="menu_custom_batch")],
        [InlineKeyboardButton("🔗 Special Link", callback_data="menu_special_link")],
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
    elif data == "menu_batch":
        await query.edit_message_text("📦 `/batch` command ကို သုံးပါ။\n\nChannel ထဲက ပထမဆုံး မက်ဆေ့ချ်ကို Forward လုပ်ပြီး နောက်ဆက်တွဲများကို စုစည်းပါ။")
    elif data == "menu_custom_batch":
        await query.edit_message_text("📦 `/custom_batch` command ကို သုံးပါ။\n\nဖိုင်များကို တစ်ခုချင်းစီ ပို့ပြီး GENERATE LINK နှိပ်ပါ။")
    elif data == "menu_special_link":
        await query.edit_message_text("🔗 `/special_link` command ကို သုံးပါ။\n\nSpecial Link အသစ်ပြုလုပ်ရန်၊ ပြင်ဆင်ရန်၊ ဖျက်ရန်။")
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

# =====================================================
# 🆕 /special_link - Menu ပေါ်အောင် (ပုံ ၂ အတိုင်း)
# =====================================================
SPECIAL_MAIN, SPECIAL_CREATE, SPECIAL_COLLECT, SPECIAL_MODIFY, SPECIAL_MODIFY_SELECT, SPECIAL_EDIT_CONTENT, SPECIAL_EDIT_ADD, SPECIAL_EDIT_ADD_POS, SPECIAL_EDIT_REMOVE, SPECIAL_WHITELIST, SPECIAL_WHITELIST_ADD, SPECIAL_WHITELIST_REMOVE, SPECIAL_PROTECT, SPECIAL_EXPIRE, SPECIAL_EXPIRE_SET, SPECIAL_DELETE = range(500, 516)

# ---------- MAIN MENU (ပုံ ၂ အတိုင်း) ----------
async def special_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("➕ CREATE", callback_data="special_create")],
        [InlineKeyboardButton("✏️ MODIFY", callback_data="special_modify")],
        [InlineKeyboardButton("🗑️ DELETE", callback_data="special_delete")],
        [InlineKeyboardButton("❌ CLOSE", callback_data="special_close")]
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

# ---------- CREATE (ပုံ ၁ အတိုင်း) ----------
async def special_create_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    context.user_data['special_messages'] = []
    context.user_data['special_link_id'] = generate_payload()
    context.user_data['special_paused'] = False
    
    keyboard = [
        [InlineKeyboardButton("⏸️ PAUSE", callback_data="create_pause")],
        [InlineKeyboardButton("🔗 GENERATE LINK", callback_data="create_generate")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="create_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔗 **Send me the message you want to store**\n\n"
        "သိမ်းဆည်းလိုသော မက်ဆေ့ချ်ကို ပို့ပါ။\n"
        "(စာသား၊ ဗီဒီယို၊ ဓာတ်ပုံ၊ Document အားလုံး လက်ခံပါသည်)\n\n"
        "မက်ဆေ့ချ်အားလုံး ပို့ပြီးပါက 'GENERATE LINK' ခလုတ်ကို နှိပ်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_COLLECT

# ---------- Collect Messages (ပြင်ဆင်ပြီး - Forward Video အပါအဝင်) ----------
async def collect_special_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    # PAUSE ဖြစ်နေရင် မလက်ခံပါ
    if context.user_data.get('special_paused', False):
        await update.message.reply_text("⏸️ လက်ရှိ Pause ထားပါသည်။ ဆက်လက်လက်ခံရန် 'RESUME' ကိုနှိပ်ပါ။")
        return SPECIAL_COLLECT
    
    msg_data = None
    
    # ---------- ၁. Forward လုပ်ထားတဲ့ မက်ဆေ့ချ် (Video/Photo/Document အားလုံး) ----------
    if update.message.forward_from_chat:
        try:
            chat_id = update.message.forward_from_chat.id
            msg_id = update.message.forward_from_message_id
            forwarded_msg = await context.bot.get_messages(chat_id=chat_id, message_ids=msg_id)
            if forwarded_msg:
                if forwarded_msg.video:
                    msg_data = {
                        "type": "video", 
                        "file_id": forwarded_msg.video.file_id, 
                        "caption": forwarded_msg.caption or "Video"
                    }
                    logger.info(f"Forwarded Video detected: {forwarded_msg.video.file_id}")
                elif forwarded_msg.photo:
                    msg_data = {
                        "type": "photo", 
                        "file_id": forwarded_msg.photo[-1].file_id, 
                        "caption": forwarded_msg.caption or "Photo"
                    }
                elif forwarded_msg.document:
                    msg_data = {
                        "type": "document", 
                        "file_id": forwarded_msg.document.file_id, 
                        "caption": forwarded_msg.caption or "Document"
                    }
                elif forwarded_msg.audio:
                    msg_data = {
                        "type": "audio", 
                        "file_id": forwarded_msg.audio.file_id, 
                        "caption": forwarded_msg.caption or "Audio"
                    }
                elif forwarded_msg.voice:
                    msg_data = {
                        "type": "voice", 
                        "file_id": forwarded_msg.voice.file_id, 
                        "caption": "Voice"
                    }
                elif forwarded_msg.animation:
                    msg_data = {
                        "type": "animation", 
                        "file_id": forwarded_msg.animation.file_id, 
                        "caption": forwarded_msg.caption or "GIF"
                    }
                elif forwarded_msg.text:
                    msg_data = {"type": "text", "text": forwarded_msg.text}
        except Exception as e:
            logger.error(f"Forwarded message error: {e}")
            await update.message.reply_text("❌ Forward လုပ်ထားတဲ့ မက်ဆေ့ချ်ကို ဖတ်လို့မရပါ။")
            return SPECIAL_COLLECT
    
    # ---------- ၂. တိုက်ရိုက်ပို့တဲ့ Media (Video/Photo/Document) ----------
    if not msg_data:
        if update.message.video:
            msg_data = {
                "type": "video", 
                "file_id": update.message.video.file_id, 
                "caption": update.message.caption or "Video"
            }
            logger.info(f"Direct Video detected: {update.message.video.file_id}")
        elif update.message.photo:
            msg_data = {
                "type": "photo", 
                "file_id": update.message.photo[-1].file_id, 
                "caption": update.message.caption or "Photo"
            }
        elif update.message.document:
            msg_data = {
                "type": "document", 
                "file_id": update.message.document.file_id, 
                "caption": update.message.caption or "Document"
            }
        elif update.message.audio:
            msg_data = {
                "type": "audio", 
                "file_id": update.message.audio.file_id, 
                "caption": update.message.caption or "Audio"
            }
        elif update.message.voice:
            msg_data = {
                "type": "voice", 
                "file_id": update.message.voice.file_id, 
                "caption": "Voice"
            }
        elif update.message.animation:
            msg_data = {
                "type": "animation", 
                "file_id": update.message.animation.file_id, 
                "caption": update.message.caption or "GIF"
            }
        elif update.message.text:
            msg_data = {"type": "text", "text": update.message.text}
    
    if not msg_data:
        await update.message.reply_text("❌ ဤမက်ဆေ့ချ်အမျိုးအစားကို မသိမ်းဆည်းနိုင်ပါ။")
        return SPECIAL_COLLECT
    
    # ---------- သိမ်းဆည်းပါ ----------
    context.user_data['special_messages'].append(msg_data)
    count = len(context.user_data['special_messages'])
    
    type_names = {
        'text': '📝 စာသား',
        'video': '🎬 ဗီဒီယို',
        'photo': '🖼️ ဓာတ်ပုံ',
        'document': '📄 Document',
        'audio': '🎵 အသံ',
        'voice': '🎤 အသံမှတ်',
        'animation': '🎞️ GIF'
    }
    type_name = type_names.get(msg_data['type'], msg_data['type'])
    
    if msg_data['type'] == 'text':
        preview = msg_data['text'][:100] + "..." if len(msg_data['text']) > 100 else msg_data['text']
        stored_msg = f"📝 {preview}"
    else:
        stored_msg = f"{type_name}: {msg_data.get('caption', 'ဖိုင်')}"
    
    keyboard = [
        [InlineKeyboardButton("⏸️ PAUSE", callback_data="create_pause")],
        [InlineKeyboardButton("🔗 GENERATE LINK", callback_data="create_generate")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="create_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ **Stored Messages: {count}**\n\n"
        f"{stored_msg}\n\n"
        f"Want to add another message? Just send it!\n"
        f"နောက်ထပ် မက်ဆေ့ချ်ထည့်လိုပါက ဆက်ပို့ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_COLLECT

# ---------- PAUSE / RESUME ----------
async def create_pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    current_pause = context.user_data.get('special_paused', False)
    new_pause = not current_pause
    context.user_data['special_paused'] = new_pause
    
    status = "⏸️ Paused" if new_pause else "▶️ Resumed"
    button_text = "▶️ RESUME" if new_pause else "⏸️ PAUSE"
    
    keyboard = [
        [InlineKeyboardButton(button_text, callback_data="create_pause")],
        [InlineKeyboardButton("🔗 GENERATE LINK", callback_data="create_generate")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="create_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    count = len(context.user_data.get('special_messages', []))
    
    await query.edit_message_text(
        f"{status}\n\n"
        f"📦 Stored Messages: {count}\n\n"
        f"{'ဆက်လက်လက်ခံရန် RESUME ကိုနှိပ်ပါ။' if new_pause else 'မက်ဆေ့ချ်များကို ဆက်လက်ပို့နိုင်ပါသည်။'}",
        reply_markup=reply_markup
    )
    return SPECIAL_COLLECT

# ---------- GENERATE LINK (ပြင်ဆင်ပြီး) ----------
async def create_generate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    messages = context.user_data.get('special_messages', [])
    
    # Debug - messages ဘယ်လောက်ရှိလဲ စစ်ပါ
    logger.info(f"Special Link - Messages count: {len(messages)}")
    
    if not messages:
        await query.edit_message_text("⚠️ မက်ဆေ့ချ် အနည်းဆုံး တစ်ခုတော့ ပို့ပေးပါ။")
        return SPECIAL_COLLECT
    
    special_id = context.user_data.get('special_link_id')
    
    # Save to database
    save_special_link(special_id, messages)
    
    # Create Deep Link
    deep_link = create_deep_linked_url(BOT_USERNAME, f"special_{special_id}")
    
    # Clear state
    context.user_data.pop('special_messages', None)
    context.user_data.pop('special_link_id', None)
    context.user_data.pop('special_paused', None)
    
    # Create buttons
    keyboard = [
        [InlineKeyboardButton("✏️ MODIFY LINK", callback_data=f"modify_link_{special_id}")],
        [InlineKeyboardButton("📤 SHARE URL", callback_data=f"share_url_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Here is your special link:**\n\n"
        f"{deep_link}\n\n"
        f"📦 သိမ်းဆည်းထားသော မက်ဆေ့ချ်: {len(messages)} ခု",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

# ---------- CANCEL ----------
async def create_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    context.user_data.pop('special_messages', None)
    context.user_data.pop('special_link_id', None)
    context.user_data.pop('special_paused', None)
    
    await query.edit_message_text("❌ Special Link ပြုလုပ်ခြင်းကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

async def cancel_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('special_messages', None)
    context.user_data.pop('special_link_id', None)
    context.user_data.pop('special_paused', None)
    await update.message.reply_text("❌ Special Link ပြုလုပ်ခြင်းကို ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END

# ---------- CLOSE ----------
async def special_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔒 **Special Link Menu ကိုပိတ်လိုက်ပါပြီ။**")
    return ConversationHandler.END

# ---------- MODIFY ----------
async def special_modify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    # Get all special links
    special_links = get_all_special_links()
    
    if not special_links:
        await query.edit_message_text(
            "📭 **Special Link မတွေ့ပါ**\n\n"
            "Special Link တစ်ခုမှ မရှိသေးပါ။\n"
            "အသစ်ပြုလုပ်လိုပါက 'CREATE' ကိုနှိပ်ပါ။"
        )
        return SPECIAL_MAIN
    
    keyboard = []
    for doc in special_links[:20]:
        special_id = doc['batch_id'].replace('special_', '')
        msg_count = len(doc.get('messages', []))
        created = doc.get('created_at', datetime.now()).strftime('%Y-%m-%d %H:%M')
        keyboard.append([InlineKeyboardButton(f"📌 {special_id[:8]}... ({msg_count} msgs) - {created}", callback_data=f"modify_select_{special_id}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="special_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "✏️ **ပြင်ဆင်လိုသော Special Link ကို ရွေးပါ**\n\n"
        "သို့မဟုတ် ပြင်ဆင်လိုသော Link ကို တိုက်ရိုက်ပို့ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_MODIFY_SELECT

async def special_modify_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('modify_select_', '')
    context.user_data['modify_special_id'] = special_id
    
    doc = get_special_link(special_id)
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MAIN
    
    keyboard = [
        [InlineKeyboardButton("📝 Edit Content", callback_data=f"modify_edit_{special_id}")],
        [InlineKeyboardButton("👥 Whitelisters", callback_data=f"modify_whitelist_{special_id}")],
        [InlineKeyboardButton("🛡️ Protect Content", callback_data=f"modify_protect_{special_id}")],
        [InlineKeyboardButton("⏰ Auto Expire", callback_data=f"modify_expire_{special_id}")],
        [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"modify_delete_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_count = len(doc.get('messages', []))
    whitelist_count = len(doc.get('whitelisters', []))
    protect = "✅ Enabled" if doc.get('protect_content', False) else "❌ Disabled"
    expire = doc.get('auto_expire', 'Not set')
    
    await query.edit_message_text(
        f"✏️ **Modify Special Link**\n\n"
        f"🆔 ID: `{special_id}`\n"
        f"📦 မက်ဆေ့ချ်: {msg_count} ခု\n"
        f"👥 Whitelisters: {whitelist_count} ဦး\n"
        f"🛡️ Protect Content: {protect}\n"
        f"⏰ Auto Expire: {expire}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SPECIAL_MODIFY

# ---------- MODIFY: DELETE LINK ----------
async def modify_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_delete_', '')
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{special_id}")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data=f"modify_back_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ **သေချာပါသလား?**\n\n"
        f"Special Link `{special_id}` ကို ဖျက်တော့မည်။\n"
        f"ဖျက်လိုက်ပါက ပြန်ယူလို့မရနိုင်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_MODIFY

# ---------- MODIFY: Edit Content (Add/Remove) ----------
async def modify_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_edit_', '')
    context.user_data['edit_special_id'] = special_id
    doc = get_special_link(special_id)
    
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MODIFY
    
    messages = doc.get('messages', [])
    msg_list = ""
    for idx, msg in enumerate(messages, 1):
        if msg['type'] == 'text':
            preview = msg['text'][:50] + "..." if len(msg['text']) > 50 else msg['text']
            msg_list += f"{idx}. 📝 {preview}\n"
        else:
            msg_list += f"{idx}. 🎬 {msg.get('caption', msg['type'])}\n"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Message", callback_data=f"edit_add_{special_id}")],
        [InlineKeyboardButton("🗑️ Remove Message", callback_data=f"edit_remove_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"modify_back_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📝 **Edit Content**\n\n"
        f"📦 မက်ဆေ့ချ် {len(messages)} ခု\n\n"
        f"{msg_list}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_EDIT_CONTENT

# ---------- Edit: Add Message ----------
async def edit_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('edit_add_', '')
    context.user_data['edit_special_id'] = special_id
    
    keyboard = [
        [InlineKeyboardButton("📌 First", callback_data="add_pos_first")],
        [InlineKeyboardButton("📌 In Between", callback_data="add_pos_between")],
        [InlineKeyboardButton("📌 Last", callback_data="add_pos_last")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"edit_back_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "➕ **Add Message**\n\n"
        "မက်ဆေ့ချ်အသစ်ကို ဘယ်နေရာမှာ ထည့်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_EDIT_ADD

async def edit_add_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    position = query.data.replace('add_pos_', '')
    context.user_data['add_position'] = position
    
    if position == 'between':
        await query.edit_message_text(
            "🔢 မက်ဆေ့ချ်အသစ်ကို ဘယ်နေရာမှာ ထည့်ချင်လဲ?\n"
            "နေရာအမှတ် (ဥပမာ - 2 ဆိုရင် မက်ဆေ့ချ် #2 နဲ့ #3 ကြားမှာ ထည့်ပါမည်)\n\n"
            "နေရာအမှတ်ကို ပို့ပါ။"
        )
        return SPECIAL_EDIT_ADD_POS
    else:
        await query.edit_message_text(
            f"📝 ထည့်လိုသော မက်ဆေ့ချ်ကို ပို့ပါ။\n\n"
            f"({'ပထမနေရာမှာ' if position == 'first' else 'နောက်ဆုံးနေရာမှာ'} ထည့်ပါမည်)"
        )
        return SPECIAL_EDIT_ADD

async def edit_add_position_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    try:
        pos = int(update.message.text)
        context.user_data['add_position_num'] = pos
        await update.message.reply_text(
            f"📝 ထည့်လိုသော မက်ဆေ့ချ်ကို ပို့ပါ။\n\n"
            f"(မက်ဆေ့ချ် #{pos} နဲ့ #{pos+1} ကြားမှာ ထည့်ပါမည်)"
        )
        return SPECIAL_EDIT_ADD
    except:
        await update.message.reply_text("❌ နေရာအမှတ်ကို နံပါတ်ဖြင့် ပို့ပါ။")
        return SPECIAL_EDIT_ADD_POS

async def edit_add_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    special_id = context.user_data.get('edit_special_id')
    doc = get_special_link(special_id)
    
    if not doc:
        await update.message.reply_text("❌ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    messages = doc.get('messages', [])
    
    msg_data = None
    if update.message.text:
        msg_data = {"type": "text", "text": update.message.text}
    elif update.message.video:
        msg_data = {"type": "video", "file_id": update.message.video.file_id, "caption": update.message.caption or "Video"}
    elif update.message.photo:
        msg_data = {"type": "photo", "file_id": update.message.photo[-1].file_id, "caption": update.message.caption or "Photo"}
    elif update.message.document:
        msg_data = {"type": "document", "file_id": update.message.document.file_id, "caption": update.message.caption or "Document"}
    else:
        await update.message.reply_text("❌ ဤမက်ဆေ့ချ်အမျိုးအစားကို မထည့်နိုင်ပါ။")
        return SPECIAL_EDIT_ADD
    
    if not msg_data:
        await update.message.reply_text("❌ မက်ဆေ့ချ်ကို မသိမ်းဆည်းနိုင်ပါ။")
        return SPECIAL_EDIT_ADD
    
    position = context.user_data.get('add_position')
    pos_num = context.user_data.get('add_position_num', 0)
    
    if position == 'first':
        messages.insert(0, msg_data)
    elif position == 'last':
        messages.append(msg_data)
    else:  # between
        if 0 < pos_num <= len(messages):
            messages.insert(pos_num, msg_data)
        else:
            messages.append(msg_data)
    
    save_special_link(special_id, messages, doc.get('whitelisters', []), doc.get('whitelist_enabled', False), doc.get('protect_content', False), doc.get('auto_expire'))
    
    context.user_data.pop('add_position', None)
    context.user_data.pop('add_position_num', None)
    
    await update.message.reply_text(
        f"✅ မက်ဆေ့ချ်အသစ် ထည့်ပြီးပါပြီ။\n"
        f"📦 စုစုပေါင်း: {len(messages)} ခု\n\n"
        f"ဆက်လက်ပြင်ဆင်လိုပါက /special_link ကိုသုံးပါ။"
    )
    return ConversationHandler.END

# ---------- Edit: Remove Message ----------
async def edit_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('edit_remove_', '')
    context.user_data['edit_special_id'] = special_id
    
    await query.edit_message_text(
        "🗑️ **Remove Message**\n\n"
        "ဖျက်လိုသော မက်ဆေ့ချ်၏ နေရာအမှတ်ကို ပို့ပါ။\n"
        "(ဥပမာ - 3 ဆိုရင် မက်ဆေ့ချ် #3 ကို ဖျက်ပါမည်)\n\n"
        "တစ်ခုထက်ပိုဖျက်ချင်ရင် comma ခြားပြီး ပို့ပါ။\n"
        "(ဥပမာ - 1, 3, 4, 5)"
    )
    return SPECIAL_EDIT_REMOVE

async def edit_remove_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    special_id = context.user_data.get('edit_special_id')
    doc = get_special_link(special_id)
    
    if not doc:
        await update.message.reply_text("❌ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    messages = doc.get('messages', [])
    
    try:
        positions = [int(x.strip()) for x in update.message.text.split(',')]
        positions.sort(reverse=True)
        
        removed = []
        for pos in positions:
            if 1 <= pos <= len(messages):
                removed.append(messages.pop(pos - 1))
        
        if removed:
            save_special_link(special_id, messages, doc.get('whitelisters', []), doc.get('whitelist_enabled', False), doc.get('protect_content', False), doc.get('auto_expire'))
            await update.message.reply_text(
                f"✅ မက်ဆေ့ချ် {len(removed)} ခုကို ဖျက်ပြီးပါပြီ။\n"
                f"📦 ကျန်ရှိသော မက်ဆေ့ချ်: {len(messages)} ခု"
            )
        else:
            await update.message.reply_text("❌ ဖျက်ရန် မက်ဆေ့ချ် မတွေ့ပါ။")
    except:
        await update.message.reply_text("❌ နေရာအမှတ်ကို နံပါတ်ဖြင့် ပို့ပါ။ (ဥပမာ - 1, 3, 4)")
    
    return ConversationHandler.END

# ---------- Modify: Whitelisters ----------
async def modify_whitelist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_whitelist_', '')
    context.user_data['whitelist_special_id'] = special_id
    doc = get_special_link(special_id)
    
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MODIFY
    
    whitelisters = doc.get('whitelisters', [])
    is_enabled = doc.get('whitelist_enabled', False)
    
    status = "✅ Enabled" if is_enabled else "❌ Disabled"
    user_list = "\n".join([f"👤 `{uid}`" for uid in whitelisters]) if whitelisters else "(none)"
    
    keyboard = [
        [InlineKeyboardButton("🔘 Enable" if not is_enabled else "🔘 Disable", callback_data=f"whitelist_toggle_{special_id}")],
        [InlineKeyboardButton("➕ Add Whitelister", callback_data=f"whitelist_add_{special_id}")],
        [InlineKeyboardButton("🗑️ Remove Whitelister", callback_data=f"whitelist_remove_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"modify_back_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"👥 **Whitelisters**\n\n"
        f"Status: {status}\n"
        f"📋 Whitelisters:\n{user_list}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SPECIAL_WHITELIST

async def whitelist_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('whitelist_toggle_', '')
    doc = get_special_link(special_id)
    
    if doc:
        current = doc.get('whitelist_enabled', False)
        save_special_link(special_id, doc.get('messages', []), doc.get('whitelisters', []), not current, doc.get('protect_content', False), doc.get('auto_expire'))
        await query.edit_message_text(f"✅ Whitelister {'ဖွင့်ပြီးပါပြီ' if not current else 'ပိတ်ပြီးပါပြီ'}။")
    
    return ConversationHandler.END

async def whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    await query.edit_message_text(
        "➕ **Add Whitelister**\n\n"
        "ထည့်လိုသော User ID ကို ပို့ပါ။\n\n"
        "User ID ရယူရန် @MissRose_Bot ကို သုံးပါ။"
    )
    return SPECIAL_WHITELIST_ADD

async def whitelist_add_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    special_id = context.user_data.get('whitelist_special_id')
    doc = get_special_link(special_id)
    
    if not doc:
        await update.message.reply_text("❌ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    user_id = update.message.text.strip()
    whitelisters = doc.get('whitelisters', [])
    
    if user_id not in whitelisters:
        whitelisters.append(user_id)
        save_special_link(special_id, doc.get('messages', []), whitelisters, doc.get('whitelist_enabled', False), doc.get('protect_content', False), doc.get('auto_expire'))
        await update.message.reply_text(f"✅ Whitelister `{user_id}` ကို ထည့်ပြီးပါပြီ။")
    else:
        await update.message.reply_text(f"⚠️ Whitelister `{user_id}` ရှိပြီးသားပါ။")
    
    return ConversationHandler.END

async def whitelist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    await query.edit_message_text(
        "🗑️ **Remove Whitelister**\n\n"
        "ဖျက်လိုသော User ID ကို ပို့ပါ။"
    )
    return SPECIAL_WHITELIST_REMOVE

async def whitelist_remove_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    special_id = context.user_data.get('whitelist_special_id')
    doc = get_special_link(special_id)
    
    if not doc:
        await update.message.reply_text("❌ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    user_id = update.message.text.strip()
    whitelisters = doc.get('whitelisters', [])
    
    if user_id in whitelisters:
        whitelisters.remove(user_id)
        save_special_link(special_id, doc.get('messages', []), whitelisters, doc.get('whitelist_enabled', False), doc.get('protect_content', False), doc.get('auto_expire'))
        await update.message.reply_text(f"✅ Whitelister `{user_id}` ကို ဖျက်ပြီးပါပြီ။")
    else:
        await update.message.reply_text(f"⚠️ Whitelister `{user_id}` မတွေ့ပါ။")
    
    return ConversationHandler.END

# ---------- Modify: Protect Content ----------
async def modify_protect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_protect_', '')
    doc = get_special_link(special_id)
    
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MODIFY
    
    current = doc.get('protect_content', False)
    new_status = not current
    
    save_special_link(special_id, doc.get('messages', []), doc.get('whitelisters', []), doc.get('whitelist_enabled', False), new_status, doc.get('auto_expire'))
    
    status_text = "Enabled ✅" if new_status else "Disabled ❌"
    await query.edit_message_text(
        f"🛡️ **Protect Content**\n\n"
        f"Protect Content ကို {status_text} လုပ်ပြီးပါပြီ။\n\n"
        f"{'✅ ယခုမှစ၍ ဤ Link ထဲက Content တွေကို Forward လုပ်လို့မရတော့ပါ။ Screenshot လည်းရိုက်လို့မရတော့ပါ။' if new_status else '❌ Protect Content ကို ပိတ်ထားပါသည်။'}"
    )
    return ConversationHandler.END

# ---------- Modify: Auto Expire ----------
async def modify_expire_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_expire_', '')
    context.user_data['expire_special_id'] = special_id
    doc = get_special_link(special_id)
    
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MODIFY
    
    current_expire = doc.get('auto_expire')
    
    keyboard = [
        [InlineKeyboardButton("⏰ Set Expire", callback_data=f"expire_set_{special_id}")],
        [InlineKeyboardButton("🗑️ Remove Expire", callback_data=f"expire_remove_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"modify_back_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⏰ **Auto Expire**\n\n"
        f"လက်ရှိ Expire: {current_expire if current_expire else 'Not set'}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_EXPIRE

async def expire_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    await query.edit_message_text(
        "⏰ **Set Auto Expire**\n\n"
        "သက်တမ်းကုန်ချိန်ကို အောက်ပါပုံစံဖြင့် ပို့ပါ။\n\n"
        "📌 ဥပမာများ:\n"
        "• `1h` = ၁ နာရီ\n"
        "• `2d` = ၂ ရက်\n"
        "• `30m` = ၃၀ မိနစ်\n"
        "• `1w` = ၁ ပတ်\n\n"
        "သက်တမ်းကုန်သွားရင် ဘယ်သူမှ ဝင်လို့မရတော့ပါ။"
    )
    return SPECIAL_EXPIRE_SET

async def expire_set_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    special_id = context.user_data.get('expire_special_id')
    doc = get_special_link(special_id)
    
    if not doc:
        await update.message.reply_text("❌ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    expire_time = update.message.text.strip()
    
    match = re.match(r'^(\d+)([hdmw])$', expire_time.lower())
    if not match:
        await update.message.reply_text(
            "❌ မှန်ကန်သော ပုံစံဖြင့် ပို့ပါ။\n"
            "ဥပမာ: 1h, 2d, 30m, 1w"
        )
        return SPECIAL_EXPIRE_SET
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit == 'm':
        seconds = value * 60
    elif unit == 'h':
        seconds = value * 3600
    elif unit == 'd':
        seconds = value * 86400
    elif unit == 'w':
        seconds = value * 604800
    else:
        await update.message.reply_text("❌ မှန်ကန်သော ယူနစ်ကို သုံးပါ။ (m, h, d, w)")
        return SPECIAL_EXPIRE_SET
    
    expire_datetime = datetime.now() + timedelta(seconds=seconds)
    
    save_special_link(special_id, doc.get('messages', []), doc.get('whitelisters', []), doc.get('whitelist_enabled', False), doc.get('protect_content', False), expire_datetime.isoformat())
    
    await update.message.reply_text(
        f"✅ Auto Expire ကို သတ်မှတ်ပြီးပါပြီ။\n\n"
        f"📅 သက်တမ်းကုန်မည့်ရက်: {expire_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"ထိုရက်ကျော်သွားရင် ဤ Link ကို ဘယ်သူမှ ဝင်လို့မရတော့ပါ။"
    )
    return ConversationHandler.END

async def expire_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('expire_remove_', '')
    doc = get_special_link(special_id)
    
    if doc:
        save_special_link(special_id, doc.get('messages', []), doc.get('whitelisters', []), doc.get('whitelist_enabled', False), doc.get('protect_content', False), None)
        await query.edit_message_text("✅ Auto Expire ကို ဖယ်ရှားပြီးပါပြီ။")
    
    return ConversationHandler.END

# ---------- Modify: Back ----------
async def modify_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('modify_back_', '')
    context.user_data['modify_special_id'] = special_id
    
    doc = get_special_link(special_id)
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return SPECIAL_MAIN
    
    keyboard = [
        [InlineKeyboardButton("📝 Edit Content", callback_data=f"modify_edit_{special_id}")],
        [InlineKeyboardButton("👥 Whitelisters", callback_data=f"modify_whitelist_{special_id}")],
        [InlineKeyboardButton("🛡️ Protect Content", callback_data=f"modify_protect_{special_id}")],
        [InlineKeyboardButton("⏰ Auto Expire", callback_data=f"modify_expire_{special_id}")],
        [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"modify_delete_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_count = len(doc.get('messages', []))
    whitelist_count = len(doc.get('whitelisters', []))
    protect = "✅ Enabled" if doc.get('protect_content', False) else "❌ Disabled"
    expire = doc.get('auto_expire', 'Not set')
    
    await query.edit_message_text(
        f"✏️ **Modify Special Link**\n\n"
        f"🆔 ID: `{special_id}`\n"
        f"📦 မက်ဆေ့ချ်: {msg_count} ခု\n"
        f"👥 Whitelisters: {whitelist_count} ဦး\n"
        f"🛡️ Protect Content: {protect}\n"
        f"⏰ Auto Expire: {expire}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SPECIAL_MODIFY

async def edit_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('edit_back_', '')
    return await modify_back_callback(update, context)

# ---------- Share URL ----------
async def share_url_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('share_url_', '')
    deep_link = create_deep_linked_url(BOT_USERNAME, f"special_{special_id}")
    
    keyboard = [
        [InlineKeyboardButton("✏️ MODIFY LINK", callback_data=f"modify_link_{special_id}")],
        [InlineKeyboardButton("📤 SHARE URL", callback_data=f"share_url_{special_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Here is your special link:**\n\n"
        f"{deep_link}",
        reply_markup=reply_markup
    )
    return ConversationHandler.END

# ---------- Modify Link (from generate) ----------
async def modify_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_id = query.data.replace('modify_link_', '')
    context.user_data['modify_special_id'] = special_id
    
    doc = get_special_link(special_id)
    if not doc:
        await query.edit_message_text("❌ ဤ Special Link ကို ရှာမတွေ့ပါ။")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("📝 Edit Content", callback_data=f"modify_edit_{special_id}")],
        [InlineKeyboardButton("👥 Whitelisters", callback_data=f"modify_whitelist_{special_id}")],
        [InlineKeyboardButton("🛡️ Protect Content", callback_data=f"modify_protect_{special_id}")],
        [InlineKeyboardButton("⏰ Auto Expire", callback_data=f"modify_expire_{special_id}")],
        [InlineKeyboardButton("🗑️ Delete Link", callback_data=f"modify_delete_{special_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_count = len(doc.get('messages', []))
    whitelist_count = len(doc.get('whitelisters', []))
    protect = "✅ Enabled" if doc.get('protect_content', False) else "❌ Disabled"
    expire = doc.get('auto_expire', 'Not set')
    
    await query.edit_message_text(
        f"✏️ **Modify Special Link**\n\n"
        f"🆔 ID: `{special_id}`\n"
        f"📦 မက်ဆေ့ချ်: {msg_count} ခု\n"
        f"👥 Whitelisters: {whitelist_count} ဦး\n"
        f"🛡️ Protect Content: {protect}\n"
        f"⏰ Auto Expire: {expire}\n\n"
        f"ဘာလုပ်ချင်လဲ ရွေးပါ။",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SPECIAL_MODIFY

# ---------- DELETE ----------
async def special_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin များသာ သုံးနိုင်ပါသည်။")
        return ConversationHandler.END
    
    special_links = get_all_special_links()
    if not special_links:
        await query.edit_message_text("📭 **Special Link မတွေ့ပါ**")
        return SPECIAL_MAIN
    
    keyboard = []
    for doc in special_links[:20]:
        special_id = doc['batch_id'].replace('special_', '')
        msg_count = len(doc.get('messages', []))
        keyboard.append([InlineKeyboardButton(f"🗑️ {special_id[:8]}... ({msg_count} msgs)", callback_data=f"delete_select_{special_id}")])
    
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
    
    special_id = query.data.replace('delete_select_', '')
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{special_id}")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="special_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ **သေချာပါသလား?**\n\n"
        f"Special Link `{special_id}` ကို ဖျက်တော့မည်။\n"
        f"ဖျက်လိုက်ပါက ပြန်ယူလို့မရနိုင်ပါ။",
        reply_markup=reply_markup
    )
    return SPECIAL_DELETE

async def special_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    special_id = query.data.replace('confirm_delete_', '')
    delete_special_link(special_id)
    
    await query.edit_message_text(f"✅ **Special Link `{special_id}` ကို အောင်မြင်စွာ ဖျက်ပြီးပါပြီ။**")
    return ConversationHandler.END

# ---------- BACK ----------
async def special_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ CREATE", callback_data="special_create")],
        [InlineKeyboardButton("✏️ MODIFY", callback_data="special_modify")],
        [InlineKeyboardButton("🗑️ DELETE", callback_data="special_delete")],
        [InlineKeyboardButton("❌ CLOSE", callback_data="special_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔗 **Special Link Management**\n\n"
        "Do you want to create a new special link, or modify an existing one, or delete it?\n\n"
        "သင်သည် Special Link အသစ်ပြုလုပ်လိုသလား၊\n"
        "ရှိပြီးသားတစ်ခုကို ပြင်ဆင်လိုသလား၊ သို့မဟုတ် ဖျက်လိုသလား?",
        reply_markup=reply_markup
    )
    return SPECIAL_MAIN

# ---------- Admin Commands ----------
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
        ("custom_batch", "ဖိုင်များကို တစ်ခုချင်းပို့ပြီး စုစည်းရန်"),
        ("special_link", "Special Link Management (CREATE/MODIFY/DELETE)"),
        ("stats", "စာရင်းအင်းကြည့်ရန်"),
        ("broadcast", "အသုံးပြုသူအားလုံးကို စာပို့ရန်"),
        ("menu", "Admin Menu ပြသရန်"),
        ("mute", "Maintenance mode ဖွင့်ရန်"),
        ("unmute", "Maintenance mode ပိတ်ရန်")
    ])

# ---------- Application ----------
application = Application.builder().token(TOKEN).build()

# --- Existing Handlers ---
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

# --- Special Link Handler (ပုံ ၂ အတိုင်း Menu ပါ) ---
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
        SPECIAL_COLLECT: [
            MessageHandler(filters.ALL, collect_special_messages),
            CallbackQueryHandler(create_pause_callback, pattern="create_pause"),
            CallbackQueryHandler(create_generate_callback, pattern="create_generate"),
            CallbackQueryHandler(create_cancel_callback, pattern="create_cancel")
        ],
        SPECIAL_MODIFY_SELECT: [
            CallbackQueryHandler(special_modify_select, pattern="^modify_select_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_MODIFY: [
            CallbackQueryHandler(modify_edit_callback, pattern="^modify_edit_"),
            CallbackQueryHandler(modify_whitelist_callback, pattern="^modify_whitelist_"),
            CallbackQueryHandler(modify_protect_callback, pattern="^modify_protect_"),
            CallbackQueryHandler(modify_expire_callback, pattern="^modify_expire_"),
            CallbackQueryHandler(modify_delete_callback, pattern="^modify_delete_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_EDIT_CONTENT: [
            CallbackQueryHandler(edit_add_callback, pattern="^edit_add_"),
            CallbackQueryHandler(edit_remove_callback, pattern="^edit_remove_"),
            CallbackQueryHandler(edit_back_callback, pattern="^edit_back_")
        ],
        SPECIAL_EDIT_ADD: [
            CallbackQueryHandler(edit_add_position, pattern="^add_pos_"),
            MessageHandler(filters.ALL, edit_add_collect)
        ],
        SPECIAL_EDIT_ADD_POS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_add_position_number)
        ],
        SPECIAL_EDIT_REMOVE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_remove_collect)
        ],
        SPECIAL_WHITELIST: [
            CallbackQueryHandler(whitelist_toggle, pattern="^whitelist_toggle_"),
            CallbackQueryHandler(whitelist_add, pattern="^whitelist_add_"),
            CallbackQueryHandler(whitelist_remove, pattern="^whitelist_remove_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_")
        ],
        SPECIAL_WHITELIST_ADD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, whitelist_add_collect)
        ],
        SPECIAL_WHITELIST_REMOVE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, whitelist_remove_collect)
        ],
        SPECIAL_EXPIRE: [
            CallbackQueryHandler(expire_set, pattern="^expire_set_"),
            CallbackQueryHandler(expire_remove, pattern="^expire_remove_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_")
        ],
        SPECIAL_EXPIRE_SET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, expire_set_collect)
        ],
        SPECIAL_DELETE: [
            CallbackQueryHandler(special_delete_confirm, pattern="^delete_select_"),
            CallbackQueryHandler(special_delete_execute, pattern="^confirm_delete_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_special)],
)

# --- Modify Link Callback (from generate) ---
application.add_handler(CallbackQueryHandler(modify_link_callback, pattern="^modify_link_"))
application.add_handler(CallbackQueryHandler(share_url_callback, pattern="^share_url_"))

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

# --- Add Special Link Handler ---
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
