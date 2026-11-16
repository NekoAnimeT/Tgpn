import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)

# ---------------------------------------------------------
# VERSION & ENV
# ---------------------------------------------------------
BOT_VERSION = "2.3.0"

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", ""))
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN در فایل .env تنظیم نشده است!")
if not ADMIN_IDS:
    raise ValueError("❌ ADMIN_IDS یا ADMIN_ID در فایل .env تنظیم نشده است!")

# Conversation States
(
    WAITING_POST_ID,
    WAITING_POST_CONTENT,
    WAITING_BROADCAST_MSG,
    WAITING_CHANNEL_ADD,
    WAITING_PROMO_CONTENT,
    WAITING_WELCOME_TEXT,
    WAITING_BAN_ID,
    WAITING_FLOOD_LIMIT,
    WAITING_LOG_CHANNEL,
    WAITING_PROMO_LINK,
    WAITING_DOWNLOAD_LIMIT,
) = range(11)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Anti-flood memory store: {user_id: [timestamps]}
_flood_tracker = defaultdict(list)

# مسیر دیتابیس
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_candidate = os.getenv("DB_DIR", BASE_DIR)
try:
    os.makedirs(_candidate, exist_ok=True)
    test_path = os.path.join(_candidate, ".write_test")
    with open(test_path, "w") as f:
        f.write("ok")
    os.remove(test_path)
    DB_DIR = _candidate
except Exception:
    DB_DIR = "/tmp"
    os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, "bot_database.db")
logger.info(f"Database path: {DB_PATH}")

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------
def get_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0
        )
    ''')
    # migration: add is_banned if missing
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except Exception:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            caption TEXT,
            is_active INTEGER DEFAULT 1,
            clicks_count INTEGER DEFAULT 0,
            downloads_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME
        )
    ''')
    try:
        cursor.execute("ALTER TABLE posts ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE posts ADD COLUMN last_used_at DATETIME")
    except Exception:
        pass
    # keep old columns for compatibility
    try:
        cursor.execute("ALTER TABLE posts ADD COLUMN file_type TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE posts ADD COLUMN file_id TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE posts ADD COLUMN download_limit INTEGER DEFAULT 0")
    except Exception:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            file_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (post_id) REFERENCES posts(post_id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS link_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            user_id INTEGER,
            action TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            invite_link TEXT,
            title TEXT,
            is_active INTEGER DEFAULT 1,
            block_count INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute("ALTER TABLE channels ADD COLUMN block_count INTEGER DEFAULT 0")
    except Exception:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            file_type TEXT,
            file_id TEXT,
            caption TEXT,
            is_active INTEGER DEFAULT 0,
            show_count INTEGER DEFAULT 0,
            forward_chat_id TEXT,
            forward_message_id INTEGER,
            pin_enabled INTEGER DEFAULT 0
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO promo (id, is_active) VALUES (1, 0)")
    for col, typ in [
        ("show_count", "INTEGER DEFAULT 0"),
        ("forward_chat_id", "TEXT"),
        ("forward_message_id", "INTEGER"),
        ("pin_enabled", "INTEGER DEFAULT 0"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE promo ADD COLUMN {col} {typ}")
        except Exception:
            pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_pins (
            user_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            pinned_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # defaults
    defaults = {
        "welcome_text": "👋 **به ربات دانلود فایل خوش آمدید!**\n\nبرای دریافت فایل‌ها روی لینک اختصاصی که دریافت کردید کلیک کنید.",
        "flood_limit": "10",
        "flood_seconds": "60",
        "log_channel_id": "",
        "default_download_limit": "0",
        "daily_report_hour": "21",
    }
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def db_add_user(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def db_is_banned(user_id: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0])

def db_set_ban(user_id: int, banned: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (banned, user_id))
    conn.commit()
    conn.close()

def db_get_users_count() -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def db_get_banned_count() -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    count = cursor.fetchone()[0]
    conn.close()
    return count

def db_get_all_users() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

def check_flood(user_id: int) -> bool:
    """True = allowed, False = flooded"""
    if is_admin(user_id):
        return True
    limit = int(get_setting("flood_limit", "10") or "10")
    seconds = int(get_setting("flood_seconds", "60") or "60")
    now = time.time()
    # clean old
    _flood_tracker[user_id] = [t for t in _flood_tracker[user_id] if now - t < seconds]
    if len(_flood_tracker[user_id]) >= limit:
        return False
    _flood_tracker[user_id].append(now)
    return True

# ---------------------------------------------------------
# FORCED JOIN CHECKER
# ---------------------------------------------------------
async def check_user_joined(app, user_id: int) -> tuple[bool, list]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, invite_link, title FROM channels WHERE is_active = 1")
    channels = cursor.fetchall()
    conn.close()

    not_joined = []
    for ch_id, invite_link, title in channels:
        try:
            member = await app.bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append((invite_link, title or ch_id, ch_id))
                # increment block count
                conn2 = get_connection()
                c2 = conn2.cursor()
                c2.execute("UPDATE channels SET block_count = block_count + 1 WHERE channel_id = ?", (ch_id,))
                conn2.commit()
                conn2.close()
        except Exception:
            not_joined.append((invite_link, title or ch_id, ch_id))

    return (len(not_joined) == 0), not_joined

# ---------------------------------------------------------
# START & FILE DELIVERY
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_add_user(user_id)

    if db_is_banned(user_id):
        await update.message.reply_text("🚫 شما از استفاده از این ربات محروم شده‌اید.")
        return

    if not check_flood(user_id):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
        return

    args = context.args
    if args:
        target_post_id = args[0]
        await process_file_delivery(update, context, user_id, target_post_id)
        return

    welcome = get_setting("welcome_text")
    await update.message.reply_text(welcome, parse_mode="Markdown")

async def process_file_delivery(update, context, user_id, post_id):
    if db_is_banned(user_id):
        msg = "🚫 شما از استفاده از این ربات محروم شده‌اید."
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    is_joined, unjoined_channels = await check_user_joined(context.application, user_id)

    if not is_joined:
        keyboard = []
        for invite_link, title, _ch_id in unjoined_channels:
            btn_text = f"📢 عضویت در {title}" if title else "📢 عضویت در کانال"
            url = invite_link if invite_link.startswith("http") else f"https://{invite_link}"
            keyboard.append([InlineKeyboardButton(btn_text, url=url)])
        keyboard.append([InlineKeyboardButton("✅ عضو شدم / دریافت فایل", callback_data=f"verify_join_{post_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        msg_text = "🔒 **برای دریافت فایل ابتدا باید در کانال‌های زیر عضو شوید:**"

        if update.callback_query:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
        return

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT caption, is_active FROM posts WHERE post_id = ?", (post_id,))
    post = cursor.fetchone()

    if not post:
        # fallback old schema single file
        cursor.execute("SELECT file_type, file_id, caption, is_active FROM posts WHERE post_id = ?", (post_id,))
        old = cursor.fetchone()
        if not old:
            msg = "❌ پست یا فایل مورد نظر پیدا نشد یا حذف شده است."
            if update.callback_query:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            conn.close()
            return
        # migrate old to post_files if needed
        file_type, file_id, caption, is_active = old
        post = (caption, is_active)

    caption, is_active = post

    # click event
    cursor.execute("UPDATE posts SET clicks_count = clicks_count + 1, last_used_at = CURRENT_TIMESTAMP WHERE post_id = ?", (post_id,))
    cursor.execute("INSERT INTO link_events (post_id, user_id, action) VALUES (?, ?, 'click')", (post_id, user_id))
    conn.commit()

    if not is_active:
        msg = "⚠️ این لینک در حال حاضر توسط مدیریت غیرفعال شده است."
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
        conn.close()
        return

    # محدودیت دانلود هر کاربر برای این لینک
    cursor.execute("SELECT download_limit FROM posts WHERE post_id = ?", (post_id,))
    lim_row = cursor.fetchone()
    post_limit = lim_row[0] if lim_row and lim_row[0] is not None else None
    if post_limit is None:
        post_limit = 0
    if post_limit == 0:
        try:
            post_limit = int(get_setting("default_download_limit", "0") or "0")
        except Exception:
            post_limit = 0

    if post_limit > 0 and not is_admin(user_id):
        cursor.execute(
            "SELECT COUNT(*) FROM link_events WHERE post_id = ? AND user_id = ? AND action = 'download'",
            (post_id, user_id),
        )
        user_dl = cursor.fetchone()[0] or 0
        if user_dl >= post_limit:
            msg = (
                f"🚫 شما به سقف دانلود این لینک رسیده‌اید.\n"
                f"حد مجاز: {post_limit} بار | دانلود شما: {user_dl}"
            )
            if update.callback_query:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            conn.close()
            return

    # get files
    cursor.execute("SELECT file_type, file_id FROM post_files WHERE post_id = ? ORDER BY sort_order, id", (post_id,))
    files = cursor.fetchall()

    # fallback to old single file columns
    if not files:
        cursor.execute("SELECT file_type, file_id FROM posts WHERE post_id = ?", (post_id,))
        row = cursor.fetchone()
        if row and row[0] and row[1]:
            files = [row]

    if not files:
        msg = "❌ فایلی برای این لینک ثبت نشده است."
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
        conn.close()
        return

    try:
        # media group for multiple photos/videos
        media_group = []
        other_files = []
        for i, (ft, fid) in enumerate(files):
            is_last = (i == len(files) - 1)
            cap = caption if is_last and len(files) > 0 else None
            if ft == 'photo':
                media_group.append(InputMediaPhoto(media=fid, caption=cap if not media_group or is_last else None))
            elif ft == 'video':
                media_group.append(InputMediaVideo(media=fid, caption=cap if not media_group or is_last else None))
            else:
                other_files.append((ft, fid, cap if is_last else None))

        if len(media_group) >= 2:
            # fix caption only on last
            for m in media_group[:-1]:
                m.caption = None
            if caption:
                media_group[-1].caption = caption
            await context.bot.send_media_group(chat_id=user_id, media=media_group)
        elif len(media_group) == 1:
            m = media_group[0]
            if isinstance(m, InputMediaPhoto):
                await context.bot.send_photo(chat_id=user_id, photo=m.media, caption=caption or "")
            else:
                await context.bot.send_video(chat_id=user_id, video=m.media, caption=caption or "")
        else:
            # single or other types
            for i, (ft, fid, cap) in enumerate([(f[0], f[1], caption if i == len(files) - 1 else None) for i, f in enumerate(files)]):
                if ft == 'photo':
                    await context.bot.send_photo(chat_id=user_id, photo=fid, caption=cap or "")
                elif ft == 'document':
                    await context.bot.send_document(chat_id=user_id, document=fid, caption=cap or "")
                elif ft == 'video':
                    await context.bot.send_video(chat_id=user_id, video=fid, caption=cap or "")
                elif ft == 'audio':
                    await context.bot.send_audio(chat_id=user_id, audio=fid, caption=cap or "")
                elif ft == 'voice':
                    await context.bot.send_voice(chat_id=user_id, voice=fid, caption=cap or "")
                elif ft == 'animation':
                    await context.bot.send_animation(chat_id=user_id, animation=fid, caption=cap or "")
                else:
                    await context.bot.send_document(chat_id=user_id, document=fid, caption=cap or "")

        cursor.execute("UPDATE posts SET downloads_count = downloads_count + 1 WHERE post_id = ?", (post_id,))
        cursor.execute("INSERT INTO link_events (post_id, user_id, action) VALUES (?, ?, 'download')", (post_id, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error sending file: {e}")
        conn.close()
        return

    # promo
    cursor.execute(
        "SELECT file_type, file_id, caption, is_active, forward_chat_id, forward_message_id, pin_enabled "
        "FROM promo WHERE id = 1"
    )
    promo = cursor.fetchone()

    if promo and promo[3] == 1:
        p_type, p_file_id, p_caption = promo[0], promo[1], promo[2]
        fwd_chat, fwd_msg, pin_on = promo[4], promo[5], promo[6]
        sent_msg = None
        try:
            if fwd_chat and fwd_msg:
                sent_msg = await context.bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=int(fwd_chat),
                    message_id=int(fwd_msg),
                )
            elif p_type == 'photo' and p_file_id:
                sent_msg = await context.bot.send_photo(chat_id=user_id, photo=p_file_id, caption=p_caption or "")
            elif p_type == 'document' and p_file_id:
                sent_msg = await context.bot.send_document(chat_id=user_id, document=p_file_id, caption=p_caption or "")
            elif p_type == 'video' and p_file_id:
                sent_msg = await context.bot.send_video(chat_id=user_id, video=p_file_id, caption=p_caption or "")
            elif p_type == 'animation' and p_file_id:
                sent_msg = await context.bot.send_animation(chat_id=user_id, animation=p_file_id, caption=p_caption or "")
            elif p_caption:
                sent_msg = await context.bot.send_message(chat_id=user_id, text=p_caption)

            cursor.execute("UPDATE promo SET show_count = show_count + 1 WHERE id = 1")
            conn.commit()

            if pin_on and sent_msg:
                try:
                    await context.bot.pin_chat_message(
                        chat_id=user_id,
                        message_id=sent_msg.message_id,
                        disable_notification=True,
                    )
                    cursor.execute(
                        "INSERT OR REPLACE INTO promo_pins (user_id, message_id) VALUES (?, ?)",
                        (user_id, sent_msg.message_id),
                    )
                    conn.commit()
                except Exception as pe:
                    logger.error(f"Pin promo error: {pe}")
        except Exception as e:
            logger.error(f"Error sending promo: {e}")

    conn.close()

async def handle_verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.replace("verify_join_", "")
    user_id = query.from_user.id
    await process_file_delivery(update, context, user_id, post_id)

# ---------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    keyboard = [
        [InlineKeyboardButton("📊 آمار کلی", callback_data="admin_stats")],
        [
            InlineKeyboardButton("➕ ساخت پست", callback_data="admin_add_post"),
            InlineKeyboardButton("📂 مدیریت پست‌ها", callback_data="admin_manage_posts")
        ],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("💎 تبلیغات زیروند", callback_data="admin_promo_settings")],
        [InlineKeyboardButton("🔒 جوین اجباری", callback_data="admin_channels")],
        [
            InlineKeyboardButton("👋 پیام خوش‌آمد", callback_data="admin_welcome"),
            InlineKeyboardButton("🚫 بن کاربر", callback_data="admin_ban")
        ],
        [
            InlineKeyboardButton("🛡 آنتی‌فلود", callback_data="admin_flood"),
            InlineKeyboardButton("💾 بک‌آپ دیتابیس", callback_data="admin_backup")
        ],
        [InlineKeyboardButton("📡 کانال لاگ", callback_data="admin_log_channel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg_text = (
        f"🛠 **پنل مدیریت ربات**\n"
        f"📦 نسخه: `{BOT_VERSION}`\n\n"
        f"یکی از بخش‌های زیر را انتخاب کنید:"
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total_users = db_get_users_count()
    banned = db_get_banned_count()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(clicks_count),0), COALESCE(SUM(downloads_count),0) FROM posts")
    row = cursor.fetchone()
    total_posts = row[0] or 0
    total_clicks = row[1] or 0
    total_downloads = row[2] or 0

    # last 24h events
    cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='click' AND created_at >= datetime('now', '-1 day')")
    clicks_24h = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='download' AND created_at >= datetime('now', '-1 day')")
    downloads_24h = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='click' AND created_at >= datetime('now', '-7 day')")
    clicks_7d = cursor.fetchone()[0] or 0

    cursor.execute("SELECT COUNT(*) FROM channels WHERE is_active = 1")
    active_channels = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COALESCE(SUM(block_count),0) FROM channels")
    total_blocks = cursor.fetchone()[0] or 0
    conn.close()

    flood_limit = get_setting("flood_limit", "10")
    flood_sec = get_setting("flood_seconds", "60")

    text = (
        f"📊 **آمار کلی ربات**  |  v`{BOT_VERSION}`\n\n"
        f"👤 کاربران: `{total_users}` (بن‌شده: `{banned}`)\n"
        f"📁 پست‌ها: `{total_posts}`\n"
        f"👆 کلیک کل: `{total_clicks}` | ۲۴س: `{clicks_24h}` | ۷روز: `{clicks_7d}`\n"
        f"📥 دانلود کل: `{total_downloads}` | ۲۴س: `{downloads_24h}`\n"
        f"🔒 کانال فعال: `{active_channels}` | بلاک‌ها: `{total_blocks}`\n"
        f"🛡 آنتی‌فلود: `{flood_limit}` درخواست / `{flood_sec}` ثانیه"
    )

    keyboard = [[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="admin_back")]]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ---------------------------------------------------------
# CREATE NEW POST (multi-file support)
# ---------------------------------------------------------
async def start_add_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('new_post_id', None)
    context.user_data.pop('new_post_files', None)
    context.user_data.pop('new_post_caption', None)

    await query.message.edit_text(
        "🆔 لطفاً شناسه اختصاصی پست را وارد کنید:\n(مثلاً: `injctor22`)\n\n"
        "فقط حروف انگلیسی و عدد مجاز است.",
        parse_mode="Markdown"
    )
    return WAITING_POST_ID

async def receive_post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post_id = update.message.text.strip().replace("/", "").lower()

    if not post_id.isalnum():
        await update.message.reply_text("❌ فقط حروف انگلیسی و عدد مجاز است. دوباره امتحان کنید:")
        return WAITING_POST_ID

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT post_id FROM posts WHERE post_id = ?", (post_id,))
    exists = cursor.fetchone()
    conn.close()

    if exists:
        await update.message.reply_text("❌ این شناسه قبلاً ثبت شده است. لطفاً یک شناسه دیگر ارسال کنید:")
        return WAITING_POST_ID

    context.user_data['new_post_id'] = post_id
    context.user_data['new_post_files'] = []
    context.user_data['new_post_caption'] = ""

    await update.message.reply_text(
        f"✅ شناسه `{post_id}` تایید شد.\n\n"
        f"حالا فایل‌ها را یکی‌یکی بفرستید (عکس، ویدیو، سند...)\n"
        f"می‌توانید چند فایل بفرستید.\n\n"
        f"وقتی تمام شد دستور /done را بفرستید.\n"
        f"برای لغو: /cancel",
        parse_mode="Markdown"
    )
    return WAITING_POST_CONTENT

async def receive_post_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg.text and msg.text.strip().lower() in ('/done', 'done', 'اتمام'):
        files = context.user_data.get('new_post_files', [])
        if not files:
            await update.message.reply_text("❌ هنوز فایلی اضافه نشده. حداقل یک فایل بفرستید یا /cancel کنید.")
            return WAITING_POST_CONTENT

        post_id = context.user_data['new_post_id']
        caption = context.user_data.get('new_post_caption', "")

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO posts (post_id, caption, file_type, file_id) VALUES (?, ?, ?, ?)",
            (post_id, caption, files[0][0], files[0][1])
        )
        for i, (ft, fid) in enumerate(files):
            cursor.execute(
                "INSERT INTO post_files (post_id, file_type, file_id, sort_order) VALUES (?, ?, ?, ?)",
                (post_id, ft, fid, i)
            )
        conn.commit()
        conn.close()

        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={post_id}"

        await update.message.reply_text(
            f"🎉 **پست با موفقیت ساخته شد!**\n\n"
            f"🆔 شناسه: `{post_id}`\n"
            f"📎 تعداد فایل: `{len(files)}`\n"
            f"🔗 لینک:\n`{link}`",
            parse_mode="Markdown"
        )
        context.user_data.pop('new_post_id', None)
        context.user_data.pop('new_post_files', None)
        context.user_data.pop('new_post_caption', None)
        await admin_panel(update, context)
        return ConversationHandler.END

    file_type = None
    file_id = None
    caption = msg.caption or ""

    if msg.photo:
        file_type = 'photo'
        file_id = msg.photo[-1].file_id
    elif msg.document:
        file_type = 'document'
        file_id = msg.document.file_id
    elif msg.video:
        file_type = 'video'
        file_id = msg.video.file_id
    elif msg.audio:
        file_type = 'audio'
        file_id = msg.audio.file_id
    elif msg.voice:
        file_type = 'voice'
        file_id = msg.voice.file_id
    elif msg.animation:
        file_type = 'animation'
        file_id = msg.animation.file_id
    else:
        await update.message.reply_text("❌ فرمت پشتیبانی نمی‌شود. فایل بفرستید یا /done بزنید.")
        return WAITING_POST_CONTENT

    files = context.user_data.get('new_post_files', [])
    files.append((file_type, file_id))
    context.user_data['new_post_files'] = files
    if caption:
        context.user_data['new_post_caption'] = caption

    await update.message.reply_text(
        f"✅ فایل {len(files)} اضافه شد ({file_type}).\n"
        f"فایل بعدی را بفرستید یا /done بزنید."
    )
    return WAITING_POST_CONTENT

# ---------------------------------------------------------
# MANAGE POSTS
# ---------------------------------------------------------
async def manage_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT post_id, is_active FROM posts ORDER BY post_id")
    posts = cursor.fetchall()
    conn.close()

    if not posts:
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]
        await query.message.edit_text("❌ هیچ پستی ثبت نشده است.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = []
    for p_id, is_act in posts:
        status_icon = "✅" if is_act == 1 else "❌"
        keyboard.append([InlineKeyboardButton(f"{status_icon} | {p_id}", callback_data=f"view_post_{p_id}")])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="admin_back")])
    await query.message.edit_text(
        "📂 **لیست پست‌ها:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_post_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    post_id = query.data.replace("view_post_", "")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_active, clicks_count, downloads_count, last_used_at, created_at, download_limit "
        "FROM posts WHERE post_id = ?",
        (post_id,)
    )
    post = cursor.fetchone()
    if not post:
        conn.close()
        await query.message.edit_text("❌ پست یافت نشد.")
        return

    is_active, clicks, downloads, last_used, created, dl_limit = post
    if dl_limit is None:
        dl_limit = 0
    default_lim = get_setting("default_download_limit", "0")
    limit_label = "نامحدود" if dl_limit == 0 else str(dl_limit)
    if dl_limit == 0 and default_lim and default_lim != "0":
        limit_label = f"پیش‌فرض سراسری ({default_lim})"

    cursor.execute("SELECT COUNT(*) FROM post_files WHERE post_id = ?", (post_id,))
    file_count = cursor.fetchone()[0] or 0
    if file_count == 0:
        file_count = 1

    cursor.execute(
        "SELECT COUNT(*) FROM link_events WHERE post_id = ? AND action='click' AND created_at >= datetime('now', '-1 day')",
        (post_id,)
    )
    c24 = cursor.fetchone()[0] or 0
    cursor.execute(
        "SELECT COUNT(*) FROM link_events WHERE post_id = ? AND action='download' AND created_at >= datetime('now', '-1 day')",
        (post_id,)
    )
    d24 = cursor.fetchone()[0] or 0
    cursor.execute(
        "SELECT COUNT(*) FROM link_events WHERE post_id = ? AND action='click' AND created_at >= datetime('now', '-7 day')",
        (post_id,)
    )
    c7 = cursor.fetchone()[0] or 0
    conn.close()

    status_str = "فعال 🟢" if is_active == 1 else "غیرفعال 🔴"
    toggle_str = "غیرفعال‌سازی ❌" if is_active == 1 else "فعال‌سازی ✅"

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={post_id}"

    text = (
        f"📊 مدیریت پست: {post_id}\n\n"
        f"🔗 {link}\n"
        f"⚙️ وضعیت: {status_str}\n"
        f"📎 تعداد فایل: {file_count}\n"
        f"🔢 سقف دانلود هر کاربر: {limit_label}\n\n"
        f"👆 کلیک کل: {clicks} | ۲۴س: {c24} | ۷روز: {c7}\n"
        f"📥 دانلود کل: {downloads} | ۲۴س: {d24}\n"
        f"🕒 آخرین استفاده: {last_used or '—'}\n"
        f"📅 تاریخ ساخت: {created or '—'}"
    )

    keyboard = [
        [InlineKeyboardButton(toggle_str, callback_data=f"toggle_post_{post_id}")],
        [InlineKeyboardButton("🔢 تنظیم سقف دانلود", callback_data=f"set_dl_limit_{post_id}")],
        [InlineKeyboardButton("🗑 حذف پست", callback_data=f"delete_post_{post_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="admin_manage_posts")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_post_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.replace("toggle_post_", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE posts SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE post_id = ?",
        (post_id,)
    )
    conn.commit()
    conn.close()
    await view_post_details(update, context)

async def delete_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.replace("delete_post_", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM post_files WHERE post_id = ?", (post_id,))
    cursor.execute("DELETE FROM link_events WHERE post_id = ?", (post_id,))
    cursor.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ پست حذف شد.", show_alert=True)
    await manage_posts(update, context)


async def start_set_dl_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.replace("set_dl_limit_", "")
    context.user_data["edit_dl_limit_post"] = post_id
    await query.message.edit_text(
        f"🔢 سقف دانلود برای پست `{post_id}`\n\n"
        f"عدد بفرستید:\n"
        f"• `0` = نامحدود\n"
        f"• `1` = فقط یک بار برای هر کاربر\n"
        f"• `3` = حداکثر ۳ بار\n\n"
        f"برای تنظیم پیش‌فرض سراسری همه لینک‌ها:\n"
        f"`default 2`",
        parse_mode="Markdown",
    )
    return WAITING_DOWNLOAD_LIMIT


async def receive_dl_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    post_id = context.user_data.get("edit_dl_limit_post")

    try:
        if text.startswith("default"):
            num = int(text.replace("default", "").strip() or "0")
            if num < 0:
                raise ValueError
            set_setting("default_download_limit", str(num))
            await update.message.reply_text(
                f"✅ پیش‌فرض سراسری سقف دانلود: {'نامحدود' if num == 0 else num}"
            )
        else:
            num = int(text)
            if num < 0:
                raise ValueError
            if not post_id:
                await update.message.reply_text("❌ پست مشخص نیست. از پنل دوباره شروع کنید.")
                await admin_panel(update, context)
                return ConversationHandler.END
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE posts SET download_limit = ? WHERE post_id = ?", (num, post_id))
            conn.commit()
            conn.close()
            await update.message.reply_text(
                f"✅ سقف دانلود `{post_id}`: {'نامحدود' if num == 0 else num}",
                parse_mode="Markdown",
            )
    except ValueError:
        await update.message.reply_text("❌ عدد نامعتبر. مثال: `1` یا `default 2`", parse_mode="Markdown")
        return WAITING_DOWNLOAD_LIMIT

    context.user_data.pop("edit_dl_limit_post", None)
    await admin_panel(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------
# PROMO
# ---------------------------------------------------------
def parse_telegram_post_link(url: str):
    """برمی‌گرداند (chat_id, message_id) یا None"""
    url = url.strip()
    # https://t.me/c/1234567890/123  (خصوصی)
    m = re.search(r't\.me/c/(\d+)/(\d+)', url)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    # https://t.me/username/123
    m = re.search(r't\.me/([A-Za-z0-9_]+)/(\d+)', url)
    if m and m.group(1).lower() not in ('c', 'joinchat'):
        return m.group(1), int(m.group(2))
    return None


async def promo_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT file_type, file_id, caption, is_active, show_count, forward_chat_id, "
        "forward_message_id, pin_enabled FROM promo WHERE id = 1"
    )
    promo = cursor.fetchone()
    conn.close()

    if not promo:
        file_type = file_id = caption = None
        is_active = show_count = pin_enabled = 0
        fwd_chat = fwd_msg = None
    else:
        file_type, file_id, caption, is_active, show_count, fwd_chat, fwd_msg, pin_enabled = promo

    status_str = "فعال 🟢" if is_active == 1 else "غیرفعال 🔴"
    toggle_str = "غیرفعال‌سازی ❌" if is_active == 1 else "فعال‌سازی ✅"
    pin_str = "پین: روشن 📌" if pin_enabled else "پین: خاموش"
    pin_btn = "خاموش کردن پین" if pin_enabled else "فعال کردن پین"

    if fwd_chat and fwd_msg:
        content_info = f"فوروارد از پست ({fwd_chat}/{fwd_msg})"
    elif file_id:
        content_info = f"دارد ({file_type or 'فایل'})"
    elif caption:
        content_info = "فقط متن"
    else:
        content_info = "ندارد"

    text = (
        f"💎 تبلیغ زیروند\n\n"
        f"⚙️ وضعیت: {status_str}\n"
        f"📝 محتوا: {content_info}\n"
        f"📄 کپشن: {caption or '—'}\n"
        f"📌 پین خودکار: {'بله' if pin_enabled else 'خیر'}\n"
        f"👁 تعداد نمایش به کاربران: {show_count or 0}"
    )
    keyboard = [
        [InlineKeyboardButton(toggle_str, callback_data="toggle_promo")],
        [InlineKeyboardButton(pin_btn, callback_data="toggle_promo_pin")],
        [InlineKeyboardButton("✏️ آپلود فایل/متن تبلیغ", callback_data="set_promo")],
        [InlineKeyboardButton("🔗 تنظیم از لینک پست (فوروارد)", callback_data="set_promo_link")],
        [InlineKeyboardButton("📍 آنپین همه پیام‌های تبلیغ", callback_data="unpin_all_promo")],
        [InlineKeyboardButton("🔄 ریست شمارنده نمایش", callback_data="reset_promo_count")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def toggle_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE promo SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = 1")
    conn.commit()
    conn.close()
    await promo_settings(update, context)


async def toggle_promo_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE promo SET pin_enabled = CASE WHEN pin_enabled = 1 THEN 0 ELSE 1 END WHERE id = 1")
    conn.commit()
    conn.close()
    await promo_settings(update, context)


async def reset_promo_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("شمارنده ریست شد ✅", show_alert=True)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE promo SET show_count = 0 WHERE id = 1")
    conn.commit()
    conn.close()
    await promo_settings(update, context)


async def unpin_all_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, message_id FROM promo_pins")
    rows = cursor.fetchall()
    success, fail = 0, 0
    for uid, mid in rows:
        try:
            await context.bot.unpin_chat_message(chat_id=uid, message_id=mid)
            success += 1
        except Exception:
            fail += 1
    cursor.execute("DELETE FROM promo_pins")
    conn.commit()
    conn.close()
    await query.message.reply_text(f"📍 آنپین انجام شد.\n✅ موفق: {success}\n❌ ناموفق: {fail}")
    await promo_settings(update, context)


async def start_set_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "📸/📝 عکس، ویدیو، فایل یا متن تبلیغ را بفرستید:\n"
        "(برای فوروارد از کانال از دکمه «لینک پست» استفاده کنید)"
    )
    return WAITING_PROMO_CONTENT


async def receive_promo_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_type = None
    file_id = None
    caption = msg.caption or msg.text or ""

    if msg.photo:
        file_type, file_id = 'photo', msg.photo[-1].file_id
    elif msg.document:
        file_type, file_id = 'document', msg.document.file_id
    elif msg.video:
        file_type, file_id = 'video', msg.video.file_id
    elif msg.animation:
        file_type, file_id = 'animation', msg.animation.file_id
    elif msg.forward_from_chat or msg.forward_origin:
        # پیام فوروارد شده مستقیم
        try:
            from_chat = msg.forward_from_chat.id if msg.forward_from_chat else None
            from_msg = msg.forward_from_message_id
            if from_chat and from_msg:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE promo SET file_type=NULL, file_id=NULL, caption=NULL, "
                    "forward_chat_id=?, forward_message_id=?, is_active=1 WHERE id=1",
                    (str(from_chat), from_msg),
                )
                conn.commit()
                conn.close()
                await update.message.reply_text("✅ تبلیغ از پیام فوروارد ذخیره و فعال شد.")
                await admin_panel(update, context)
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"forward promo save: {e}")
    elif msg.text:
        file_type, file_id = 'text', None
        caption = msg.text

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE promo SET file_type=?, file_id=?, caption=?, forward_chat_id=NULL, "
        "forward_message_id=NULL, is_active=1 WHERE id=1",
        (file_type, file_id, caption),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ تبلیغ ذخیره و فعال شد.")
    await admin_panel(update, context)
    return ConversationHandler.END


async def start_set_promo_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "🔗 لینک پست کانال را بفرستید:\n\n"
        "مثال:\n"
        "https://t.me/channelname/123\n"
        "https://t.me/c/1234567890/55\n\n"
        "⚠️ ربات باید در آن کانال عضو/ادمین باشد تا بتواند فوروارد کند."
    )
    return WAITING_PROMO_LINK


async def receive_promo_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    parsed = parse_telegram_post_link(text)
    if not parsed:
        await update.message.reply_text("❌ لینک نامعتبر است. دوباره بفرستید یا /cancel")
        return WAITING_PROMO_LINK

    chat_ref, msg_id = parsed
    bot = context.bot
    try:
        # resolve username to id if needed
        if isinstance(chat_ref, str):
            chat = await bot.get_chat(f"@{chat_ref}" if not chat_ref.startswith("@") else chat_ref)
            chat_id = chat.id
        else:
            chat_id = chat_ref

        # test forward to admin
        test = await bot.forward_message(
            chat_id=update.effective_user.id,
            from_chat_id=chat_id,
            message_id=msg_id,
        )
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE promo SET file_type=NULL, file_id=NULL, caption=NULL, "
            "forward_chat_id=?, forward_message_id=?, is_active=1 WHERE id=1",
            (str(chat_id), msg_id),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ تبلیغ از لینک ذخیره و فعال شد.\n"
            f"پیش‌نمایش بالا فوروارد شد.\n"
            f"chat: {chat_id} | msg: {msg_id}"
        )
    except Exception as e:
        logger.error(f"promo link error: {e}")
        await update.message.reply_text(
            f"❌ نتوانستم پست را فوروارد کنم.\n{e}\n\n"
            "ربات باید در کانال عضو باشد و پست عمومی/قابل‌دسترسی باشد."
        )
        return WAITING_PROMO_LINK

    await admin_panel(update, context)
    return ConversationHandler.END

# ---------------------------------------------------------
# CHANNELS
# ---------------------------------------------------------
async def manage_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, title, is_active, block_count FROM channels")
    channels = cursor.fetchall()
    conn.close()

    text = "🔒 **جوین اجباری**\n\n"
    keyboard = []
    for ch_id, title, is_act, blocks in channels:
        status = "✅" if is_act == 1 else "❌"
        keyboard.append([
            InlineKeyboardButton(f"{status} {title or ch_id} (بلاک:{blocks or 0})", callback_data=f"ch_info_{ch_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ افزودن کانال", callback_data="add_channel")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ch_id = query.data.replace("ch_info_", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title, invite_link, is_active, block_count FROM channels WHERE channel_id = ?", (ch_id,))
    ch = cursor.fetchone()
    conn.close()
    if not ch:
        await query.answer("پیدا نشد", show_alert=True)
        return
    title, invite_link, is_active, blocks = ch
    status = "فعال 🟢" if is_active == 1 else "غیرفعال 🔴"
    toggle_text = "غیرفعال‌سازی ❌" if is_active == 1 else "فعال‌سازی ✅"
    # بدون Markdown تا underscore در لینک/عنوان ارور نده
    text = (
        f"🔒 کانال\n\n"
        f"عنوان: {title or '—'}\n"
        f"آیدی: {ch_id}\n"
        f"لینک: {invite_link or '—'}\n"
        f"وضعیت: {status}\n"
        f"🚫 تعداد بلاک (عدم عضویت): {blocks or 0}"
    )
    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data=f"toggle_ch_{ch_id}")],
        [InlineKeyboardButton("🗑 حذف", callback_data=f"del_channel_{ch_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_channels")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ch_id = query.data.replace("toggle_ch_", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE channels SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE channel_id = ?",
        (ch_id,)
    )
    conn.commit()
    conn.close()
    await channel_info(update, context)

async def start_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "📌 **افزودن کانال:**\n\n"
        "• `@mychannel`\n"
        "• `-100123456789`\n"
        "• `https://t.me/mychannel`\n"
        "• `-100123456789|https://t.me/+xxxxx` (لینک خصوصی)\n\n"
        "⚠️ ربات باید ادمین کانال باشد.",
        parse_mode="Markdown"
    )
    return WAITING_CHANNEL_ADD

async def receive_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    bot = context.bot
    channel_id = None
    invite_link = None
    title = None

    try:
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) != 2:
                raise ValueError("فرمت اشتباه")
            part_a, part_b = parts[0], parts[1]
            if part_a.lstrip("-").isdigit():
                channel_id, invite_link = part_a, part_b
            elif part_b.lstrip("-").isdigit():
                channel_id, invite_link = part_b, part_a
            else:
                channel_id, invite_link = part_a, part_b
            if not invite_link.startswith("http"):
                invite_link = f"https://{invite_link.lstrip('/')}"
            try:
                chat = await bot.get_chat(int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id)
                title = chat.title or chat.username or channel_id
                channel_id = str(chat.id)
            except Exception:
                title = str(channel_id)

        elif "t.me/+" in text or "t.me/joinchat/" in text or "telegram.me/+" in text:
            invite_link = text if text.startswith("http") else f"https://{text.lstrip('/')}"
            try:
                chat = await bot.get_chat(invite_link)
                channel_id = str(chat.id)
                title = chat.title or chat.username or channel_id
            except Exception:
                await update.message.reply_text(
                    "⚠️ لینک خصوصی است. به این شکل بفرستید:\n"
                    "`-100123456789|https://t.me/+xxxxx`",
                    parse_mode="Markdown"
                )
                return WAITING_CHANNEL_ADD
        else:
            raw = text
            if "t.me/" in raw:
                raw = raw.split("t.me/")[-1].split("?")[0].strip("/")
            elif "telegram.me/" in raw:
                raw = raw.split("telegram.me/")[-1].split("?")[0].strip("/")
            if raw.startswith("@"):
                raw = raw[1:]
            try:
                if raw.lstrip("-").isdigit():
                    chat = await bot.get_chat(int(raw))
                else:
                    chat = await bot.get_chat(f"@{raw}")
                channel_id = str(chat.id)
                title = chat.title or chat.username or channel_id
                invite_link = f"https://t.me/{chat.username}" if chat.username else f"https://t.me/c/{str(chat.id).replace('-100', '')}"
            except Exception as e:
                logger.error(f"get_chat failed for '{raw}': {e}")
                await update.message.reply_text(
                    f"❌ کانال پیدا نشد.\n"
                    f"خطا: {e}\n\n"
                    "اگر کانال خصوصی است از فرمت:\n"
                    "آیدی|لینک\n"
                    "استفاده کنید. ربات باید ادمین باشد."
                )
                return WAITING_CHANNEL_ADD

        if not channel_id or not invite_link:
            raise ValueError("اطلاعات ناقص")

        channel_id = str(channel_id)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT block_count FROM channels WHERE channel_id = ?", (channel_id,))
        row = cursor.fetchone()
        block_count = row[0] if row else 0
        cursor.execute(
            "INSERT OR REPLACE INTO channels (channel_id, invite_link, title, is_active, block_count) VALUES (?, ?, ?, 1, ?)",
            (channel_id, invite_link, title or channel_id, block_count)
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ کانال اضافه شد!\n"
            f"📢 {title or channel_id}\n"
            f"🆔 {channel_id}\n"
            f"🔗 {invite_link}"
        )
    except Exception as e:
        logger.error(f"Add channel error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ خطا در افزودن کانال:\n{e}\n\n"
            "مطمئن شوید ربات ادمین کانال است و لینک/یوزرنیم درست است."
        )

    await admin_panel(update, context)
    return ConversationHandler.END

async def delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ch_id = query.data.replace("del_channel_", "")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (ch_id,))
    conn.commit()
    conn.close()
    await query.answer("✅ حذف شد.", show_alert=True)
    await manage_channels(update, context)

# ---------------------------------------------------------
# WELCOME MESSAGE
# ---------------------------------------------------------
async def admin_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current = get_setting("welcome_text")
    await query.message.edit_text(
        f"👋 **پیام خوش‌آمد فعلی:**\n\n{current}\n\n"
        f"متن جدید را بفرستید (از Markdown پشتیبانی می‌شود):",
        parse_mode="Markdown"
    )
    return WAITING_WELCOME_TEXT

async def receive_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    if not text.strip():
        await update.message.reply_text("❌ متن خالی است.")
        return WAITING_WELCOME_TEXT
    set_setting("welcome_text", text.strip())
    await update.message.reply_text("✅ پیام خوش‌آمد ذخیره شد.")
    await admin_panel(update, context)
    return ConversationHandler.END

# ---------------------------------------------------------
# BAN USER
# ---------------------------------------------------------
async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    banned = db_get_banned_count()
    await query.message.edit_text(
        f"🚫 **مدیریت بن**\n\nتعداد بن‌شده: `{banned}`\n\n"
        f"آیدی عددی کاربر را بفرستید:\n"
        f"• برای بن: فقط آیدی\n"
        f"• برای آنبن: `unban آیدی`\n\nمثال: `123456789` یا `unban 123456789`",
        parse_mode="Markdown"
    )
    return WAITING_BAN_ID

async def receive_ban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    try:
        if text.startswith("unban"):
            uid = int(text.replace("unban", "").strip())
            db_set_ban(uid, 0)
            await update.message.reply_text(f"✅ کاربر `{uid}` آنبن شد.", parse_mode="Markdown")
        else:
            uid = int(text)
            if is_admin(uid):
                await update.message.reply_text("❌ نمی‌توان ادمین را بن کرد.")
            else:
                db_set_ban(uid, 1)
                await update.message.reply_text(f"🚫 کاربر `{uid}` بن شد.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ آیدی نامعتبر است.")
        return WAITING_BAN_ID
    await admin_panel(update, context)
    return ConversationHandler.END

# ---------------------------------------------------------
# ANTI-FLOOD
# ---------------------------------------------------------
async def admin_flood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    limit = get_setting("flood_limit", "10")
    seconds = get_setting("flood_seconds", "60")
    await query.message.edit_text(
        f"🛡 **آنتی‌فلود**\n\n"
        f"محدودیت فعلی: `{limit}` درخواست در `{seconds}` ثانیه\n\n"
        f"برای تغییر به این شکل بفرستید:\n"
        f"`تعداد|ثانیه`\n\nمثال: `15|60` یعنی ۱۵ درخواست در ۶۰ ثانیه",
        parse_mode="Markdown"
    )
    return WAITING_FLOOD_LIMIT

async def receive_flood_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        if "|" in text:
            a, b = text.split("|", 1)
            limit, seconds = int(a.strip()), int(b.strip())
        else:
            limit = int(text)
            seconds = 60
        if limit < 1 or seconds < 1:
            raise ValueError
        set_setting("flood_limit", str(limit))
        set_setting("flood_seconds", str(seconds))
        await update.message.reply_text(f"✅ آنتی‌فلود تنظیم شد: `{limit}` / `{seconds}`ثانیه", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ فرمت اشتباه. مثال: `10|60`")
        return WAITING_FLOOD_LIMIT
    await admin_panel(update, context)
    return ConversationHandler.END

# ---------------------------------------------------------
# BACKUP
# ---------------------------------------------------------
async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=f,
                filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
                caption=f"💾 بک‌آپ دیتابیس\n📦 v{BOT_VERSION}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        await query.message.reply_text("✅ فایل بک‌آپ ارسال شد.")
    except Exception as e:
        await query.message.reply_text(f"❌ خطا در ارسال بک‌آپ: {e}")

# ---------------------------------------------------------
# LOG CHANNEL
# ---------------------------------------------------------
async def admin_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    current = get_setting("log_channel_id", "") or "تنظیم نشده"
    report_hour = get_setting("daily_report_hour", "21")
    await query.message.edit_text(
        f"📡 کانال لاگ\n\n"
        f"آیدی فعلی: {current}\n"
        f"ساعت گزارش روزانه: {report_hour}:00 (وقت سرور)\n\n"
        f"آیدی عددی کانال را بفرستید (مثال: -100123456789)\n"
        f"ربات باید ادمین کانال باشد.\n\n"
        f"برای تغییر ساعت گزارش روزانه:\n"
        f"hour 21\n\n"
        f"برای غیرفعال: off"
    )
    return WAITING_LOG_CHANNEL

async def receive_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("off", "0", "none", "غیرفعال"):
        set_setting("log_channel_id", "")
        await update.message.reply_text("✅ کانال لاگ غیرفعال شد.")
    elif text.lower().startswith("hour"):
        try:
            h = int(text.lower().replace("hour", "").strip())
            if not (0 <= h <= 23):
                raise ValueError
            set_setting("daily_report_hour", str(h))
            await update.message.reply_text(f"✅ ساعت گزارش روزانه: {h}:00")
        except ValueError:
            await update.message.reply_text("❌ ساعت نامعتبر (۰ تا ۲۳). مثال: hour 21")
            return WAITING_LOG_CHANNEL
    else:
        try:
            cid = int(text)
            set_setting("log_channel_id", str(cid))
            await update.message.reply_text(f"✅ کانال لاگ تنظیم شد: {cid}")
        except ValueError:
            await update.message.reply_text("❌ آیدی نامعتبر.")
            return WAITING_LOG_CHANNEL
    await admin_panel(update, context)
    return ConversationHandler.END

async def hourly_log_job(context: ContextTypes.DEFAULT_TYPE):
    log_ch = get_setting("log_channel_id", "")
    if not log_ch:
        return
    try:
        total_users = db_get_users_count()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='click' AND created_at >= datetime('now', '-1 hour')")
        c1h = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='download' AND created_at >= datetime('now', '-1 hour')")
        d1h = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM posts")
        posts = cursor.fetchone()[0] or 0
        conn.close()
        text = (
            f"📡 لاگ ساعتی ربات v{BOT_VERSION}\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"👤 کاربران: {total_users}\n"
            f"📁 پست‌ها: {posts}\n"
            f"👆 کلیک ۱ ساعت اخیر: {c1h}\n"
            f"📥 دانلود ۱ ساعت اخیر: {d1h}"
        )
        await context.bot.send_message(chat_id=int(log_ch), text=text)
    except Exception as e:
        logger.error(f"Hourly log error: {e}")


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """گزارش روزانه خودکار به کانال لاگ"""
    log_ch = get_setting("log_channel_id", "")
    if not log_ch:
        return
    try:
        total_users = db_get_users_count()
        banned = db_get_banned_count()
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users WHERE joined_at >= datetime('now', '-1 day')")
        new_users = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='click' AND created_at >= datetime('now', '-1 day')")
        clicks_24 = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM link_events WHERE action='download' AND created_at >= datetime('now', '-1 day')")
        downloads_24 = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM posts")
        total_posts = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM posts WHERE is_active = 1")
        active_posts = cursor.fetchone()[0] or 0

        cursor.execute(
            "SELECT post_id, downloads_count FROM posts ORDER BY downloads_count DESC LIMIT 5"
        )
        top_posts = cursor.fetchall()

        cursor.execute("SELECT COALESCE(SUM(block_count),0) FROM channels")
        blocks = cursor.fetchone()[0] or 0

        cursor.execute("SELECT show_count FROM promo WHERE id = 1")
        promo_row = cursor.fetchone()
        promo_shows = promo_row[0] if promo_row else 0

        conn.close()

        top_text = "\n".join(
            [f"  • {p[0]}: {p[1]} دانلود" for p in top_posts]
        ) if top_posts else "  —"

        text = (
            f"📅 گزارش روزانه ربات v{BOT_VERSION}\n"
            f"🗓 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"👤 کل کاربران: {total_users}\n"
            f"🆕 کاربر جدید ۲۴س: {new_users}\n"
            f"🚫 بن‌شده: {banned}\n\n"
            f"👆 کلیک ۲۴س: {clicks_24}\n"
            f"📥 دانلود ۲۴س: {downloads_24}\n\n"
            f"📁 پست‌ها: {total_posts} (فعال: {active_posts})\n"
            f"🏆 برترین لینک‌ها:\n{top_text}\n\n"
            f"🔒 بلاک جوین اجباری: {blocks}\n"
            f"💎 نمایش تبلیغ: {promo_shows}"
        )
        await context.bot.send_message(chat_id=int(log_ch), text=text)
        logger.info("Daily report sent")
    except Exception as e:
        logger.error(f"Daily report error: {e}")


async def check_daily_report_trigger(context: ContextTypes.DEFAULT_TYPE):
    """هر ساعت چک می‌کند آیا ساعت گزارش روزانه رسیده"""
    try:
        hour = int(get_setting("daily_report_hour", "21") or "21")
    except Exception:
        hour = 21
    now = datetime.now()
    if now.hour == hour and now.minute < 5:
        last = get_setting("last_daily_report_date", "")
        today = now.strftime("%Y-%m-%d")
        if last != today:
            set_setting("last_daily_report_date", today)
            await daily_report_job(context)

# ---------------------------------------------------------
# BROADCAST
# ---------------------------------------------------------
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("📢 پیام همگانی را بفرستید:")
    return WAITING_BROADCAST_MSG

async def receive_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['broadcast_msg_id'] = update.message.message_id
    context.user_data['broadcast_chat_id'] = update.message.chat_id
    keyboard = [
        [InlineKeyboardButton("✅ تایید و ارسال", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel_broadcast")]
    ]
    await update.message.reply_text(
        "🔍 پیش‌نمایش ثبت شد. ارسال شود؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg_id = context.user_data.get('broadcast_msg_id')
    chat_id = context.user_data.get('broadcast_chat_id')
    if not msg_id or not chat_id:
        await query.message.edit_text("❌ پیام یافت نشد.")
        return
    users = db_get_all_users()
    await query.message.edit_text(f"⏳ ارسال به `{len(users)}` کاربر...", parse_mode="Markdown")
    success, failed = 0, 0
    for u_id in users:
        try:
            await context.bot.copy_message(chat_id=u_id, from_chat_id=chat_id, message_id=msg_id)
            success += 1
        except Exception:
            failed += 1
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📊 نتیجه:\n✅ موفق: `{success}`\n❌ ناموفق: `{failed}`",
        parse_mode="Markdown"
    )

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("❌ لغو شد.", show_alert=True)
    await admin_panel(update, context)

# ---------------------------------------------------------
# CANCEL
# ---------------------------------------------------------
async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("❌ عملیات لغو شد.")
    await admin_panel(update, context)
    return ConversationHandler.END

# ---------------------------------------------------------
# HEALTH SERVER (برای Pxxl / health check)
# ---------------------------------------------------------
def start_health_server():
    port = int(os.getenv("PORT", os.getenv("EXPOSE_PORT", "3000")))

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return  # quiet

        def do_GET(self):
            body = b'{"status":"ok","service":"telegram-bot","version":"' + BOT_VERSION.encode() + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"Health server listening on 0.0.0.0:{port}")
    except Exception as e:
        logger.error(f"Health server failed: {e}")


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    start_health_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))

    add_post_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_post, pattern="^admin_add_post$")],
        states={
            WAITING_POST_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_post_id)],
            WAITING_POST_CONTENT: [
                MessageHandler(filters.COMMAND, receive_post_content),
                MessageHandler(filters.ALL & ~filters.COMMAND, receive_post_content),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(add_post_handler)

    set_promo_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_set_promo, pattern="^set_promo$")],
        states={WAITING_PROMO_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_promo_content)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(set_promo_handler)

    set_promo_link_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_set_promo_link, pattern="^set_promo_link$")],
        states={WAITING_PROMO_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_promo_link)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(set_promo_link_handler)

    add_channel_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_channel, pattern="^add_channel$")],
        states={WAITING_CHANNEL_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_info)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(add_channel_handler)

    broadcast_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_broadcast, pattern="^admin_broadcast$")],
        states={WAITING_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_broadcast_msg)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(broadcast_handler)

    welcome_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_welcome, pattern="^admin_welcome$")],
        states={WAITING_WELCOME_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_welcome_text)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(welcome_handler)

    ban_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_ban, pattern="^admin_ban$")],
        states={WAITING_BAN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ban_id)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(ban_handler)

    flood_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_flood, pattern="^admin_flood$")],
        states={WAITING_FLOOD_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flood_limit)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(flood_handler)

    log_ch_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_log_channel, pattern="^admin_log_channel$")],
        states={WAITING_LOG_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_log_channel)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(log_ch_handler)

    dl_limit_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_set_dl_limit, pattern="^set_dl_limit_")],
        states={WAITING_DOWNLOAD_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_dl_limit)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True
    )
    app.add_handler(dl_limit_handler)

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_back$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(manage_posts, pattern="^admin_manage_posts$"))
    app.add_handler(CallbackQueryHandler(view_post_details, pattern="^view_post_"))
    app.add_handler(CallbackQueryHandler(toggle_post_status, pattern="^toggle_post_"))
    app.add_handler(CallbackQueryHandler(delete_post, pattern="^delete_post_"))
    app.add_handler(CallbackQueryHandler(promo_settings, pattern="^admin_promo_settings$"))
    app.add_handler(CallbackQueryHandler(toggle_promo, pattern="^toggle_promo$"))
    app.add_handler(CallbackQueryHandler(toggle_promo_pin, pattern="^toggle_promo_pin$"))
    app.add_handler(CallbackQueryHandler(reset_promo_count, pattern="^reset_promo_count$"))
    app.add_handler(CallbackQueryHandler(unpin_all_promo, pattern="^unpin_all_promo$"))
    app.add_handler(CallbackQueryHandler(manage_channels, pattern="^admin_channels$"))
    app.add_handler(CallbackQueryHandler(channel_info, pattern="^ch_info_"))
    app.add_handler(CallbackQueryHandler(toggle_channel, pattern="^toggle_ch_"))
    app.add_handler(CallbackQueryHandler(delete_channel, pattern="^del_channel_"))
    app.add_handler(CallbackQueryHandler(confirm_broadcast, pattern="^confirm_broadcast$"))
    app.add_handler(CallbackQueryHandler(cancel_broadcast, pattern="^cancel_broadcast$"))
    app.add_handler(CallbackQueryHandler(handle_verify_join, pattern="^verify_join_"))
    app.add_handler(CallbackQueryHandler(admin_backup, pattern="^admin_backup$"))

    # Jobs
    if app.job_queue:
        app.job_queue.run_repeating(hourly_log_job, interval=3600, first=60)
        # هر ۵ دقیقه چک برای گزارش روزانه در ساعت تنظیم‌شده
        app.job_queue.run_repeating(check_daily_report_trigger, interval=300, first=120)
    else:
        logger.warning("JobQueue not available. Install: pip install 'python-telegram-bot[job-queue]'")

    print(f"🤖 Bot v{BOT_VERSION} is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
