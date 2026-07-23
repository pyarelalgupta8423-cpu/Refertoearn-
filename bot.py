import sys, os, asyncio, random, logging, re
from urllib.parse import urlparse
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ChatMemberHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from pymongo import ReturnDocument
from argo_listener import start_argo_listener_thread
from config import BOT_TOKEN, ADMIN_IDS, SECOND_BOT_USERNAME
from database import *
from reward_service import *
from datetime import datetime, timedelta
from bson import ObjectId
import secrets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

web_app = Flask(__name__)
@web_app.route("/")
def home(): return "Bot Running ✅", 200
def run_web_server(): web_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), use_reloader=False)

# ============ HELPERS ============
def safe_object_id(value):
    try: return ObjectId(value)
    except: return None

def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("https", "http", "tg") and bool(parsed.netloc)
    except: return False

ALLOWED_CALLBACKS = {
    "main_menu", "withdraw_menu", "main_get_id", "main_refer", "main_available_ids",
    "main_tasks", "main_earn", "main_stats", "main_balance", "start_verify",
    "check_join", "ext_tasks_complete",
}
URL_PLACEHOLDERS = {"user_id", "username"}

def validate_button_action(action):
    if action.startswith("url:"):
        url = action[4:].strip()
        if not is_valid_url(url): return False
        for ph in re.findall(r'\{(\w+)\}', url):
            if ph not in URL_PLACEHOLDERS: return False
        return True
    if action.startswith("callback:"):
        data = action[9:]
        return data in ALLOWED_CALLBACKS and 1 <= len(data.encode("utf-8")) <= 64
    return False

def run_transaction(callback):
    try:
        with client.start_session() as session:
            return session.with_transaction(callback)
    except Exception as e:
        logger.error(f"Transaction failed: {e}")
        return None

def escape_for_mode(text, parse_mode):
    if parse_mode == ParseMode.HTML: return text
    if parse_mode == ParseMode.MARKDOWN: return escape_markdown(str(text), version=1)
    if parse_mode == ParseMode.MARKDOWN_V2: return escape_markdown(str(text), version=2)
    return text

async def safe_send(bot, chat_id, text, parse_mode=None, reply_markup=None):
    try:
        return await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Send failed: {e}")
        try: return await bot.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e2: logger.error(f"Fallback send failed: {e2}"); return None

async def safe_edit(query, text, parse_mode=None, reply_markup=None):
    try:
        return await query.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Edit failed: {e}")
        try: return await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception as e2: logger.error(f"Fallback edit failed: {e2}"); return None

async def safe_answer(query, text=None, show_alert=False):
    try:
        if text: await query.answer(text, show_alert=show_alert)
        else: await query.answer()
    except Exception as e: logger.error(f"Answer failed: {e}")

def get_screen(sid):
    s = get_collection("ui_screens").find_one({"screen_id": sid})
    return s if s and s.get("text") else DEFAULT_SCREENS.get(sid, {"text": "", "parse_mode": "Markdown"})

def get_parse_mode(screen):
    mode = screen.get("parse_mode", "Markdown")
    if mode == "HTML": return ParseMode.HTML
    if mode == "MarkdownV2": return ParseMode.MARKDOWN_V2
    if mode == "None": return None
    return ParseMode.MARKDOWN

def get_buttons(sid, active_only=True):
    q = {"screen_id": sid}
    if active_only: q["active"] = True
    return list(get_collection("ui_buttons").find(q).sort("order", 1))

def get_all_buttons(sid):
    return get_buttons(sid, active_only=False)

# DEPRECATED: use get_all_services() from database.py
def get_services():
    return get_all_services(only_accounts=True)

def render_text(template, context):
    if not template: return ""
    for k, v in context.items(): template = template.replace(f"{{{k}}}", str(v))
    return template

def build_keyboard(buttons, text_context, url_context, parse_mode):
    kb = []; row = []; current_size = None
    for btn in buttons:
        size = max(1, min(int(btn.get("row_size", 2)), 8))
        if current_size is None: current_size = size
        if size != current_size and row: kb.append(row); row = []; current_size = size
        action = btn.get("action", "")
        if action.startswith("url:"):
            text = render_text(btn.get("text", ""), text_context)
            url = render_text(action.replace("url:", ""), url_context)
            row.append(InlineKeyboardButton(text, url=url))
        elif action.startswith("callback:"):
            text = render_text(btn.get("text", ""), text_context)
            row.append(InlineKeyboardButton(text, callback_data=action.replace("callback:", "")))
        if len(row) >= current_size: kb.append(row); row = []; current_size = None
    if row: kb.append(row)
    return InlineKeyboardMarkup(kb) if kb else None

SCREEN_IDS = [
    "welcome", "welcome_back", "force_join", "external_tasks", "verification_done", "main_menu",
    "withdraw_success", "withdraw_insufficient", "withdraw_menu", "withdraw_input",
    "referral", "profile", "stats", "earn", "tasks_menu", "task_detail", "no_tasks", "task_add_group",
    "insufficient_referrals", "verify_required",
    "referral_reward_l1", "referral_reward_l2", "group_reward", "group_reward_pending", "group_no_reward",
    "withdraw_approved_user", "withdraw_rejected_user", "withdraw_payout_card",
    "keyboard_get_id", "keyboard_refer", "keyboard_dashboard", "keyboard_tasks", "keyboard_balance", "keyboard_profile",
]

DEFAULT_SCREENS = {
    "welcome": {"screen_id": "welcome", "text": "👋 ᴡᴇʟᴄᴏᴍᴇ {first_name}!\n\n⚠️ ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟs:", "parse_mode": "Markdown"},
    "welcome_back": {"screen_id": "welcome_back", "text": "👋 ᴡᴇʟᴄᴏᴍᴇ ʙᴀᴄᴋ {first_name}!\n\n✅ ᴠᴇʀɪꜰɪᴇᴅ\n\n👇 ᴄʜᴏᴏsᴇ:", "parse_mode": "Markdown"},
    "force_join": {"screen_id": "force_join", "text": "⚠️ ᴊᴏɪɴ ᴀʟʟ ᴄʜᴀɴɴᴇʟs:", "parse_mode": "Markdown"},
    "external_tasks": {"screen_id": "external_tasks", "text": "📋 ᴄᴏᴍᴘʟᴇᴛᴇ ᴛᴀsᴋs:", "parse_mode": "Markdown"},
    "verification_done": {"screen_id": "verification_done", "text": "🎉 ᴅᴏɴᴇ!\n✅ ᴄʜᴀɴɴᴇʟs\n✅ ᴛᴀsᴋs\n✅ ʀᴇꜰᴇʀʀᴀʟ", "parse_mode": "Markdown"},
    "main_menu": {"screen_id": "main_menu", "text": "📱 *ᴍᴀɪɴ ᴍᴇɴᴜ*", "parse_mode": "Markdown"},
    "withdraw_success": {"screen_id": "withdraw_success", "text": "✅ sᴜʙᴍɪᴛᴛᴇᴅ!\n🔢 `{token}`\n💰 -{deducted} | 💎 {remaining}", "parse_mode": "Markdown"},
    "withdraw_insufficient": {"screen_id": "withdraw_insufficient", "text": "❌ ɴᴏᴛ ᴇɴᴏᴜɢʜ!\n💰 {points} | 💎 ɴᴇᴇᴅ {required}", "parse_mode": "Markdown"},
    "withdraw_menu": {"screen_id": "withdraw_menu", "text": "💳 sᴇʟᴇᴄᴛ sᴇʀᴠɪᴄᴇ:", "parse_mode": "Markdown"},
    "withdraw_input": {"screen_id": "withdraw_input", "text": "📝 {input_label}\n\nsᴇɴᴅ ᴅᴇᴛᴀɪʟs:", "parse_mode": "Markdown"},
    "referral": {"screen_id": "referral", "text": "🔗 `{link}`\n\n💰 ʟ1: {level1} | ʟ2: {level2}", "parse_mode": "Markdown"},
    "profile": {"screen_id": "profile", "text": "👤 {full_name}\n💰 {points} ᴘᴛs\n👥 ʀᴇꜰs: {referrals}\n✅ ᴛᴀsᴋs: {tasks_done}", "parse_mode": "Markdown"},
    "stats": {"screen_id": "stats", "text": "📊 ᴛᴏᴛᴀʟ: {completed} | ⏳{pending} | 💰{total_requests}", "parse_mode": "Markdown"},
    "earn": {"screen_id": "earn", "text": "💎 ᴇᴀʀɴ\n\n🔗 ʟ1: {level1} | ʟ2: {level2}\n📱 ɢʀᴏᴜᴘs: 10-200\n📋 ᴛᴀsᴋs: 5-100\n💳 ᴍɪɴ: {min_withdraw}", "parse_mode": "Markdown"},
    "tasks_menu": {"screen_id": "tasks_menu", "text": "📋 ᴛᴀsᴋs:", "parse_mode": "Markdown"},
    "task_detail": {"screen_id": "task_detail", "text": "📋 *{task_name}*\n💰 {task_points} ᴘᴛs\n⏳ {attempts}/2", "parse_mode": "Markdown"},
    "task_add_group": {"screen_id": "task_add_group", "text": "📋 ᴀᴅᴅ @{bot_username} ᴀs ᴀᴅᴍɪɴ!", "parse_mode": "Markdown"},
    "no_tasks": {"screen_id": "no_tasks", "text": "📋 ɴᴏ ᴛᴀsᴋs!", "parse_mode": "Markdown"},
    "insufficient_referrals": {"screen_id": "insufficient_referrals", "text": "🔒 ɴᴇᴇᴅ {required} ʀᴇꜰs! ʏᴏᴜ: {referrals}", "parse_mode": "Markdown"},
    "verify_required": {"screen_id": "verify_required", "text": "❌ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ʀᴇǫᴜɪʀᴇᴅ!\nᴜsᴇ /start", "parse_mode": "Markdown"},
    "referral_reward_l1": {"screen_id": "referral_reward_l1", "text": "🎉 ɴᴇᴡ ʀᴇꜰᴇʀʀᴀʟ!\n💰 +{points} ᴘᴛs", "parse_mode": "Markdown"},
    "referral_reward_l2": {"screen_id": "referral_reward_l2", "text": "🌟 ʟ2 ʙᴏɴᴜs!\n💰 +{points} ᴘᴛs", "parse_mode": "Markdown"},
    "group_reward": {"screen_id": "group_reward", "text": "✅ ʀᴇᴡᴀʀᴅ!\n📱 {group_name}\n👥 {member_count}\n💰 {reward_points}", "parse_mode": "Markdown"},
    "group_reward_pending": {"screen_id": "group_reward_pending", "text": "⚠️ ᴘᴇɴᴅɪɴɢ!\n📱 {group_name}\n💰 {reward_points}\n\nᴠᴇʀɪꜰʏ ᴡɪᴛʜ /start", "parse_mode": "Markdown"},
    "group_no_reward": {"screen_id": "group_no_reward", "text": "❌ <100 ᴍᴇᴍʙᴇʀs!", "parse_mode": "Markdown"},
    "withdraw_approved_user": {"screen_id": "withdraw_approved_user", "text": "✅ ᴀᴘᴘʀᴏᴠᴇᴅ!\n🔢 #{token}\n💰 {points}", "parse_mode": "Markdown"},
    "withdraw_rejected_user": {"screen_id": "withdraw_rejected_user", "text": "❌ ʀᴇᴊᴇᴄᴛᴇᴅ\n💰 {points} ʀᴇꜰᴜɴᴅᴇᴅ", "parse_mode": "Markdown"},
    "withdraw_payout_card": {"screen_id": "withdraw_payout_card", "text": "💳 *ɴᴇᴡ ᴡɪᴛʜᴅʀᴀᴡᴀʟ*\n🔢 #{token}\n👤 {full_name}\n🆔 `{user_id}`\n📱 @{username}\n💎 sᴇʀᴠɪᴄᴇ: {service_name}\n💰 {points} ᴘᴛs\n👥 ᴅɪʀᴇᴄᴛ ʀᴇꜰs: {referrals}\n📝 ᴜsᴇʀ ɪɴᴘᴜᴛ: {user_input}", "parse_mode": "Markdown"},
    "keyboard_get_id": {"screen_id": "keyboard_get_id", "text": "✅ sᴜʙᴍɪᴛᴛᴇᴅ!\n🔢 `{token}`\n💰 -{deducted} | 💎 {remaining}", "parse_mode": "Markdown"},
    "keyboard_refer": {"screen_id": "keyboard_refer", "text": "🔗 `{link}`", "parse_mode": "Markdown"},
    "keyboard_dashboard": {"screen_id": "keyboard_dashboard", "text": "📊 ᴛ: {completed} | ⏳{pending} | 💰{total_requests}", "parse_mode": "Markdown"},
    "keyboard_tasks": {"screen_id": "keyboard_tasks", "text": "📋 ᴛᴀsᴋs:", "parse_mode": "Markdown"},
    "keyboard_balance": {"screen_id": "keyboard_balance", "text": "💰 *{points}* ᴘᴛs", "parse_mode": "Markdown"},
    "keyboard_profile": {"screen_id": "keyboard_profile", "text": "👤 {full_name}\n💰 {points} ᴘᴛs\n👥 ʀᴇꜰs: {referrals}\n✅ ᴛᴀsᴋs: {tasks_done}", "parse_mode": "Markdown"},
}

DEFAULT_BUTTONS = [
    {"screen_id": "main_menu", "text": "💳 ᴡɪᴛʜᴅʀᴀᴡ", "action": "callback:withdraw_menu", "order": 1, "active": True},
    {"screen_id": "main_menu", "text": "🔗 ʀᴇꜰᴇʀʀᴀʟ", "action": "callback:main_refer", "order": 2, "active": True},
    {"screen_id": "main_menu", "text": "📊 sᴛᴀᴛᴜs", "action": "callback:main_available_ids", "order": 3, "active": True},
    {"screen_id": "main_menu", "text": "📋 ᴛᴀsᴋs", "action": "callback:main_tasks", "order": 4, "active": True},
    {"screen_id": "main_menu", "text": "💎 ᴇᴀʀɴ", "action": "callback:main_earn", "order": 5, "active": True},
    {"screen_id": "main_menu", "text": "👤 ᴘʀᴏꜰɪʟᴇ", "action": "callback:main_stats", "order": 6, "active": True},
]

def seed_cms_data():
    for sid in SCREEN_IDS:
        if not get_collection("ui_screens").find_one({"screen_id": sid}):
            get_collection("ui_screens").insert_one(DEFAULT_SCREENS.get(sid, {"screen_id": sid, "text": sid, "parse_mode": "Markdown"}))
    for btn in DEFAULT_BUTTONS:
        get_collection("ui_buttons").update_one({"screen_id": btn["screen_id"], "action": btn["action"]}, {"$setOnInsert": btn}, upsert=True)

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("💳 ᴡɪᴛʜᴅʀᴀᴡ"), KeyboardButton("🔗 ʀᴇꜰᴇʀ & ᴇᴀʀɴ")],
    [KeyboardButton("📊 ᴅᴀsʜʙᴏᴀʀᴅ"), KeyboardButton("📋 ᴛᴀsᴋs")],
    [KeyboardButton("💰 ʙᴀʟᴀɴᴄᴇ"), KeyboardButton("👤 ᴘʀᴏꜰɪʟᴇ")]
], resize_keyboard=True)
VERIFY_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("🔄 sᴛᴀʀᴛ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ")]], resize_keyboard=True)

def build_text_context(user, user_data, parse_mode):
    return {
        "first_name": escape_for_mode(user.first_name or "User", parse_mode),
        "full_name": escape_for_mode(user.full_name or "User", parse_mode),
        "username": escape_for_mode(user.username or "N/A", parse_mode),
        "user_id": str(user.id),
        "points": str(user_data.get("points", 0)),
        "referrals": str(len(user_data.get("referrals", []))),
        "level2_referrals": str(len(user_data.get("level2_referrals", []))),
        "tasks_done": str(len(user_data.get("completed_tasks", []))),
        "balance": str(user_data.get("points", 0)),
        "join_date": user_data.get("join_date", datetime.now()).strftime("%d %b %Y"),
        "required_withdraw": str(get_points_config().get("min_withdraw", 50)),
        "required_referrals": "0",
    }

def build_url_context(user, user_data):
    return {"user_id": str(user.id), "username": user.username or "N/A"}

def sync_verification_version(uid):
    current_version = get_verification_version()
    user = get_user(uid)
    if not user: return None
    if user.get("verification_version", 0) == current_version: return user
    get_collection("users").update_one({"user_id": uid}, {"$set": {"external_tasks_completed": False, "verification_version": current_version}, "$unset": {"verification.external_required": "", "verification.external_attempts": ""}})
    return get_user(uid)

def ensure_user_in_db(uid, username="", full_name=""):
    get_collection("users").update_one({"user_id": uid}, {"$set": {"username": username, "full_name": full_name}, "$setOnInsert": {"points": 0, "referrals": [], "level2_referrals": [], "completed_tasks": [], "force_join_completed": False, "external_tasks_completed": False, "verification_version": 0, "join_date": datetime.now()}}, upsert=True)

def get_or_create_user(uid, username="", full_name=""):
    ensure_user_in_db(uid, username, full_name)
    return get_user(uid)

def reject_withdrawal_atomic(serial_no, admin_id):
    def callback(session):
        req = get_collection("withdraw_requests").find_one({"serial_no": serial_no, "status": "pending"}, session=session)
        if not req: return None
        result = get_collection("withdraw_requests").update_one({"_id": req["_id"], "status": "pending"}, {"$set": {"status": "rejected", "processed_date": datetime.now(), "processed_by": admin_id, "refund_completed": True}, "$unset": {"payout_notification_failed": "", "payout_notification_error": ""}}, session=session)
        if result.modified_count != 1: return None
        refund = get_collection("users").update_one({"user_id": req["user_id"]}, {"$inc": {"points": req["points"]}}, session=session)
        if refund.modified_count != 1: raise RuntimeError("Refund failed")
        return req
    return run_transaction(callback)

# ============ USER HANDLERS ============
async def check_force_join(uid, context):
    not_joined, check_failed = [], False
    for ch in get_collection("channels").find({"active": True}):
        try:
            m = await context.bot.get_chat_member(ch["channel_id"], uid)
            if m.status in ("left", "kicked"): not_joined.append(ch)
        except Exception as e:
            check_failed = True; logger.error(f"Force join check failed for {ch.get('channel_id')}: {e}")
    return not_joined, check_failed

async def ensure_force_join_verified(uid, context):
    not_joined, check_failed = await check_force_join(uid, context)
    if check_failed: return False
    if not_joined:
        get_collection("users").update_one({"user_id": uid}, {"$set": {"force_join_completed": False}})
        return False
    get_collection("users").update_one({"user_id": uid}, {"$set": {"force_join_completed": True}})
    return True

async def ensure_user_verified(uid, context):
    u = sync_verification_version(uid)
    if not u or not u.get("external_tasks_completed"): return False
    return await ensure_force_join_verified(uid, context)

async def process_pending_group_rewards(uid, context):
    for r in process_pending_group_rewards_atomic(uid):
        sc = get_screen("group_reward"); pm = get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"], {"group_name": escape_for_mode(r.get("title","?"), pm), "member_count": str(r.get("member_count",0)), "reward_points": str(r.get("points",0))}), pm)

async def handle_referral_points(uid, rid, context):
    if not await ensure_user_verified(uid, context) or uid == rid: return False
    r = credit_referral_atomic(uid, rid)
    if not r: return False
    sc = get_screen("referral_reward_l1"); pm = get_parse_mode(sc)
    await safe_send(context.bot, r["referrer_id"], render_text(sc["text"], {"points": str(r['level1_points'])}), pm)
    if r.get("level2_id"):
        sc2 = get_screen("referral_reward_l2"); pm2 = get_parse_mode(sc2)
        await safe_send(context.bot, r["level2_id"], render_text(sc2["text"], {"points": str(r['level2_points'])}), pm2)
    return True

async def handle_force_join_complete(update, context):
    q = update.callback_query; uid = q.from_user.id
    if not await ensure_force_join_verified(uid, context): await safe_answer(q, "❌ ᴊᴏɪɴ!"); return
    links = list(get_collection("external_links").find({"active": True}))
    if links:
        sc = get_screen("external_tasks"); pm = get_parse_mode(sc)
        kb = [[InlineKeyboardButton(f"🔗 {l['name']}", url=l['url'])] for l in links if is_valid_url(l.get("url",""))]
        kb.append([InlineKeyboardButton("🔍 ᴠᴇʀɪꜰʏ ᴀʟʟ", callback_data="ext_tasks_complete")])
        await safe_edit(q, sc["text"], pm, InlineKeyboardMarkup(kb))
    else: await complete_verification(update, context, uid)

async def handle_external_tasks_complete(update, context):
    q=update.callback_query; uid=q.from_user.id
    if not await ensure_force_join_verified(uid, context): await safe_answer(q,"❌ ᴊᴏɪɴ!"); return
    await complete_verification(update, context, uid)

async def complete_verification(update, context, uid):
    q = update.callback_query
    if not await ensure_force_join_verified(uid, context): await safe_answer(q, "❌ sᴛᴀʏ!"); return
    get_collection("users").update_one({"user_id": uid}, {"$set": {"external_tasks_completed": True, "verification_version": get_verification_version()}, "$unset": {"verification.external_required": "", "verification.external_attempts": ""}})
    await process_pending_group_rewards(uid, context)
    u = get_user(uid)
    if u.get("pending_referrer"): await handle_referral_points(uid, u["pending_referrer"], context)
    fresh_u = get_user(uid); sc = get_screen("verification_done"); pm = get_parse_mode(sc)
    await safe_edit(q, sc["text"], pm, build_keyboard(get_buttons("main_menu"), build_text_context(q.from_user, fresh_u, pm), build_url_context(q.from_user, fresh_u), pm))

def build_premium_force_join(user, channels):
    first_name = escape_markdown(user.first_name or "User")
    text = (
        f"👋 *Hello➳ {first_name}*\n\n"
        "➳ *YOU NEED TO JOIN THE FOLLOWING*\n"
        "*CHANNELS BEFORE CONTINUING☆*\n\n"
        "✨ Join every channel below, then tap *Continue➳✨*."
    )
    kb = []
    for c in channels:
        title = c.get("channel_name") or "Join Channel"
        if len(title) > 30: title = title[:27] + "..."
        kb.append([InlineKeyboardButton(f"➳ {title} ✨", url=c["invite_link"])])
    kb.append([InlineKeyboardButton("Continue➳✨", callback_data="check_join")])
    return text, InlineKeyboardMarkup(kb)

# ============ CALLBACK ROUTER ============
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q.data; uid = q.from_user.id
    u = get_or_create_user(uid, q.from_user.username or "", q.from_user.full_name or "")
    if d == "main_menu":
        if await ensure_user_verified(uid, context):
            sc = get_screen("main_menu"); pm = get_parse_mode(sc)
            await safe_edit(q, sc["text"], pm, build_keyboard(get_buttons("main_menu"), build_text_context(q.from_user, u, pm), build_url_context(q.from_user, u), pm))
        else:
            sc = get_screen("verify_required"); pm = get_parse_mode(sc)
            await safe_edit(q, sc["text"], pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔄 sᴛᴀʀᴛ", callback_data="start_verify")]]))
    elif d in ["withdraw_menu", "main_get_id"]: await withdraw_menu_handler(update, context)
    elif d == "main_refer": await refer_menu_handler(update, context)
    elif d == "main_available_ids": await available_ids_handler(update, context)
    elif d == "main_tasks": await tasks_menu_handler(update, context)
    elif d == "main_earn": await earn_points_handler(update, context)
    elif d == "main_stats": await show_stats(update, context)
    elif d == "main_balance": await safe_answer(q, f"💰 {u['points']} ᴘᴛs")
    elif d == "check_join":
        not_joined, check_failed = await check_force_join(uid, context)
        if check_failed: await safe_answer(q, "⚠️ Verification temporarily unavailable!")
        elif not_joined:
            await safe_answer(q, "❌ ᴊᴏɪɴ ᴀʟʟ ᴄʜᴀɴɴᴇʟs!")
            text, markup = build_premium_force_join(q.from_user, not_joined)
            await safe_edit(q, text, ParseMode.MARKDOWN, markup)
        else: await safe_answer(q, "✅ ᴠᴇʀɪꜰɪᴇᴅ!"); await handle_force_join_complete(update, context)
    elif d == "start_verify":
        u = sync_verification_version(uid); not_joined, check_failed = await check_force_join(uid, context)
        if check_failed: await safe_answer(q, "⚠️ Verification temporarily unavailable!")
        elif not_joined:
            text, markup = build_premium_force_join(q.from_user, not_joined)
            await safe_edit(q, text, ParseMode.MARKDOWN, markup)
        else: await handle_force_join_complete(update, context)
    elif d == "ext_tasks_complete": await handle_external_tasks_complete(update, context)
    elif d.startswith("task_do_"): await handle_specific_task(update, context, d.replace("task_do_", ""))
    elif d.startswith("task_open_"):
        tid=d.replace("task_open_",""); t=get_task_by_id(tid)
        if not t or not is_valid_url(t.get("url","")): await safe_answer(q,"❌"); return
        get_collection("users").update_one({"user_id":uid},{"$set":{f"task_opened_at.{tid}":datetime.utcnow()}})
        await safe_answer(q,"🔗 Link ready!"); await q.message.reply_text(f"🔗 {t.get('name','Task')}\n{t['url']}\n\nComplete task, then verify.")
    elif d.startswith("task_verify_"): await verify_task_completion(update, context, d.replace("task_verify_", ""))
    elif d.startswith("taskshot_approve_") or d.startswith("taskshot_reject_"): await review_task_screenshot(update, context)
    elif d.startswith("withdraw_service_"): await handle_withdraw_service(update, context, d.replace("withdraw_service_", ""))
    elif d.startswith("withdraw_input_"): await handle_withdraw_input_prompt(update, context, d.replace("withdraw_input_", ""))
    elif d.startswith("payout_approve_"): await payout_approve(update, context, d.replace("payout_approve_", ""))
    elif d.startswith("payout_reject_"): await payout_reject(update, context, d.replace("payout_reject_", ""))
    elif d.startswith("admin_"): await handle_admin_callbacks(update, context)
    else: await safe_answer(q, "❓")

# ============ WITHDRAW HANDLERS (using unified services) ============
async def withdraw_menu_handler(update, context):
    q = update.callback_query; uid = q.from_user.id
    for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
    if not await ensure_user_verified(uid, context): await safe_answer(q, "❌"); return
    services = get_all_services(only_accounts=True)  # unified services
    if not services:
        sc = get_screen("no_tasks"); await safe_edit(q, sc["text"], get_parse_mode(sc), InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]])); return
    u = get_user(uid); kb = []
    for s in services:
        txt = f"{s.get('emoji','💳')} {s.get('name','Svc')} ({s.get('points',50)} ᴘᴛs)"
        if s.get("required_referrals", 0) > 0 and len(u.get("referrals", [])) < s["required_referrals"]: txt += " 🔒"
        kb.append([InlineKeyboardButton(txt, callback_data=f"withdraw_service_{s['_id']}")])
    kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
    sc = get_screen("withdraw_menu"); await safe_edit(q, sc["text"], get_parse_mode(sc), InlineKeyboardMarkup(kb))

async def handle_withdraw_service(update, context, sid):
    q = update.callback_query; uid = q.from_user.id
    oid = safe_object_id(sid)
    if not oid: await safe_answer(q, "❌ Invalid!"); return
    s = get_service(sid)  # unified
    if not s or not s.get("is_active"): await safe_answer(q, "❌ Service unavailable!"); return
    u = get_user(uid); req_pts = s.get("points", get_points_config()["min_withdraw"]); req_refs = s.get("required_referrals", 0)
    if req_pts <= 0: await safe_answer(q, "❌ Service error!"); return
    if u['points'] < req_pts:
        sc = get_screen("withdraw_insufficient"); ctx = build_text_context(q.from_user, u, get_parse_mode(sc)); ctx["required"] = str(req_pts)
        await safe_edit(q, render_text(sc["text"], ctx), get_parse_mode(sc)); return
    if req_refs > 0 and len(u.get("referrals", [])) < req_refs:
        sc = get_screen("insufficient_referrals"); ctx = build_text_context(q.from_user, u, get_parse_mode(sc)); ctx["required"] = str(req_refs)
        await safe_edit(q, render_text(sc["text"], ctx), get_parse_mode(sc)); return
    if s.get("input_label"):
        context.user_data["withdraw_service"] = sid; context.user_data["awaiting_withdraw_input"] = True
        sc = get_screen("withdraw_input"); await safe_edit(q, render_text(sc["text"], {"input_label": s["input_label"]}), get_parse_mode(sc), InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="withdraw_menu")]]))
    else:
        await process_withdraw(update, context, uid, s, "")

async def handle_withdraw_input_prompt(update, context, sid):
    q = update.callback_query; uid = q.from_user.id
    if not await ensure_user_verified(uid, context): await safe_answer(q, "❌ Verify!"); return
    oid = safe_object_id(sid)
    if not oid: await safe_answer(q, "❌ Invalid!"); return
    s = get_service(sid)
    if not s or not s.get("is_active"): await safe_answer(q, "❌ Service unavailable!"); return
    context.user_data["withdraw_service"] = sid; context.user_data["awaiting_withdraw_input"] = True
    sc = get_screen("withdraw_input"); await safe_edit(q, render_text(sc["text"], {"input_label": s.get("input_label","")}), get_parse_mode(sc), InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="withdraw_menu")]]))

async def process_withdraw(update, context, uid, service, user_input):
    if not await ensure_user_verified(uid, context):
        for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
        if hasattr(update, 'callback_query') and update.callback_query: await safe_answer(update.callback_query, "❌ Verification required!")
        elif hasattr(update, 'message') and update.message: await safe_send(context.bot, uid, "❌ Verification required!\nUse /start", reply_markup=VERIFY_KEYBOARD)
        return
    service_id = service.get("_id"); fresh_service = get_service(str(service_id))
    if not fresh_service or not fresh_service.get("is_active"):
        for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
        if hasattr(update, 'callback_query') and update.callback_query: await safe_answer(update.callback_query, "❌ Service unavailable!")
        elif hasattr(update, 'message') and update.message: await safe_send(context.bot, uid, "❌ Service unavailable!", reply_markup=MAIN_KEYBOARD)
        return
    service = fresh_service; fresh_user = get_user(uid); req_pts = service.get("points", get_points_config()["min_withdraw"])
    if req_pts <= 0:
        if hasattr(update, 'callback_query') and update.callback_query: await safe_answer(update.callback_query, "❌ Service error!")
        return
    if len(fresh_user.get("referrals", [])) < service.get("required_referrals", 0):
        for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
        if hasattr(update, 'message') and update.message: await safe_send(context.bot, uid, "❌ Referral requirement not met.", reply_markup=MAIN_KEYBOARD)
        elif hasattr(update, 'callback_query') and update.callback_query: await safe_answer(update.callback_query, "❌ Referral requirement not met.")
        return
    user = update.effective_user if hasattr(update, 'effective_user') and update.effective_user else None
    username = user.username if user else "N/A"; full_name = user.full_name if user else "Unknown"
    r = create_withdrawal_atomic(uid, req_pts, username, full_name, service_id=str(service.get("_id","")), service_name=service.get("name",""), service_emoji=service.get("emoji","💳"), required_referrals=service.get("required_referrals",0), user_input=user_input or "N/A")
    if not r:
        if hasattr(update, 'callback_query') and update.callback_query: await safe_answer(update.callback_query, "❌")
        return
    payout_channel = get_payout_channel()
    if payout_channel:
        u = get_user(uid); sc = get_screen("withdraw_payout_card"); pm = get_parse_mode(sc)
        ctx = build_text_context(user, u, pm)
        ctx.update({"token": str(r['serial_no']), "points": str(req_pts), "user_input": escape_for_mode(user_input or "N/A", pm), "service_name": escape_for_mode(service.get("name","?"), pm), "service_emoji": service.get("emoji","💳")})
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ᴀᴘᴘʀᴏᴠᴇ", callback_data=f"payout_approve_{r['serial_no']}"), InlineKeyboardButton("❌ ʀᴇᴊᴇᴄᴛ", callback_data=f"payout_reject_{r['serial_no']}")]])
            await context.bot.send_message(payout_channel, render_text(sc["text"], ctx), reply_markup=kb, parse_mode=pm)
        except Exception as e:
            logger.error(f"Payout send failed: {e}")
            get_collection("withdraw_requests").update_one({"serial_no": r['serial_no']}, {"$set": {"payout_notification_failed": True, "payout_notification_error": str(e)}})
    sc2 = get_screen("withdraw_success"); pm2 = get_parse_mode(sc2)
    ctx2 = build_text_context(user, get_user(uid), pm2)
    ctx2.update({"token": str(r['serial_no']), "deducted": str(req_pts), "remaining": str(r['new_balance'])})
    if hasattr(update, 'callback_query') and update.callback_query:
        await safe_edit(update.callback_query, render_text(sc2["text"], ctx2), pm2, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))
    elif hasattr(update, 'message') and update.message:
        sc3 = get_screen("keyboard_get_id"); pm3 = get_parse_mode(sc3)
        await safe_send(context.bot, uid, render_text(sc3["text"], ctx2), pm3, MAIN_KEYBOARD)
    for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)

# ============ PAYOUT APPROVE (with claim generation) ============
async def payout_approve(update, context, serial_no):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS: await safe_answer(q, "❌ Unauthorized!"); return
    try: serial_no = int(serial_no)
    except: await safe_answer(q, "❌ Invalid!"); return
    r = get_collection("withdraw_requests").find_one_and_update({"serial_no": serial_no, "status": "pending"}, {"$set": {"status": "completed", "processed_date": datetime.now(), "processed_by": q.from_user.id}, "$unset": {"payout_notification_failed": "", "payout_notification_error": ""}}, return_document=ReturnDocument.BEFORE)
    if not r: await safe_answer(q, "⚠️ Already processed!"); return
    # Send approval message to user
    sc = get_screen("withdraw_approved_user"); pm = get_parse_mode(sc)
    await safe_send(context.bot, r["user_id"], render_text(sc["text"], {"token": str(r['serial_no']), "points": str(r['points'])}), pm)

    # --- NEW: Create claim link for second bot ---
    if SECOND_BOT_USERNAME and r.get("service_id"):
        try:
            token = create_claim(r["user_id"], r["service_id"])
            link = f"https://t.me/{SECOND_BOT_USERNAME}?start=claim_{token}"
            claim_msg = (
                f"🎉 Your withdrawal is approved!\n\n"
                f"Click below to claim your service:\n{link}\n\n"
                f"⏳ This link expires in 24 hours."
            )
            await safe_send(context.bot, r["user_id"], claim_msg, parse_mode=None)
        except Exception as e:
            logger.error(f"Failed to create claim: {e}")
    else:
        if not SECOND_BOT_USERNAME:
            logger.warning("SECOND_BOT_USERNAME not set, cannot send claim link.")
        # If service_id missing, still okay.

    await q.edit_message_text((q.message.text or "") + "\n\n✅ APPROVED", reply_markup=None)
    await safe_answer(q, "✅ Approved!")

async def payout_reject(update, context, serial_no):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS: await safe_answer(q, "❌ Unauthorized!"); return
    try: serial_no = int(serial_no)
    except: await safe_answer(q, "❌ Invalid!"); return
    r = reject_withdrawal_atomic(serial_no, q.from_user.id)
    if not r: await safe_answer(q, "⚠️ Already processed!"); return
    sc = get_screen("withdraw_rejected_user"); pm = get_parse_mode(sc)
    await safe_send(context.bot, r["user_id"], render_text(sc["text"], {"token": str(r['serial_no']), "points": str(r['points'])}), pm)
    await q.edit_message_text((q.message.text or "") + "\n\n❌ REJECTED", reply_markup=None)
    await safe_answer(q, "❌ Rejected!")

def get_payout_channel():
    s = get_collection("settings").find_one({"type": "payout_channel"})
    return s["channel_id"] if s else None

# ============ MENU HANDLERS (unchanged) ============
async def refer_menu_handler(update, context):
    q = update.callback_query; link = f"https://t.me/{context.bot.username}?start=ref_{q.from_user.id}"
    sc = get_screen("referral"); pm = get_parse_mode(sc)
    await safe_edit(q, render_text(sc["text"], {"link": link, "level1": str(get_points_config()['refer_level_1']), "level2": str(get_points_config()['refer_level_2'])}), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))

async def show_stats(update, context):
    q = update.callback_query; sc = get_screen("profile"); pm = get_parse_mode(sc)
    await safe_edit(q, render_text(sc["text"], build_text_context(q.from_user, get_or_create_user(q.from_user.id), pm)), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))

async def available_ids_handler(update, context):
    q = update.callback_query
    t=get_collection("withdraw_requests").count_documents({}); p=get_collection("withdraw_requests").count_documents({"status":"pending"}); c=get_collection("withdraw_requests").count_documents({"status":"completed"})
    sc = get_screen("stats"); pm = get_parse_mode(sc)
    await safe_edit(q, render_text(sc["text"], {"completed":str(c),"pending":str(p),"total_requests":str(t)}), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))

async def earn_points_handler(update, context):
    q = update.callback_query; p = get_points_config(); sc = get_screen("earn"); pm = get_parse_mode(sc)
    await safe_edit(q, render_text(sc["text"], {"level1":str(p['refer_level_1']),"level2":str(p['refer_level_2']),"min_withdraw":str(p['min_withdraw'])}), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]))

async def tasks_menu_handler(update, context):
    q = update.callback_query
    if not await ensure_user_verified(q.from_user.id, context): await safe_answer(q, "❌"); return
    completed = set(get_user(q.from_user.id).get("completed_tasks", []))
    tasks = [t for t in get_collection("tasks").find({"active": True}) if t["_id"] not in completed]
    if not tasks:
        sc = get_screen("no_tasks"); await safe_edit(q, sc["text"], get_parse_mode(sc), InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]])); return
    kb = [[InlineKeyboardButton(f"📌 {t['name']} (+{t['points']})", callback_data=f"task_do_{t['_id']}")] for t in tasks]
    kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
    sc = get_screen("tasks_menu"); await safe_edit(q, sc["text"], get_parse_mode(sc), InlineKeyboardMarkup(kb))

async def handle_specific_task(update, context, tid):
    q = update.callback_query; oid = safe_object_id(tid)
    if not oid: await safe_answer(q, "❌"); return
    t = get_task_by_id(tid)
    if not t: await safe_answer(q, "❌"); return
    if not await ensure_user_verified(q.from_user.id, context): await safe_answer(q, "❌"); return
    u = get_user(q.from_user.id)
    if tid in [str(x) for x in u.get("completed_tasks",[])]: await safe_answer(q, "✅"); return
    if t["type"] == "add_to_group":
        sc = get_screen("task_add_group"); pm = get_parse_mode(sc)
        await safe_edit(q, render_text(sc["text"], {"bot_username": context.bot.username}), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_tasks")]]))
    else:
        if not is_valid_url(t.get("url","")): await safe_answer(q, "❌ Task configuration error!"); return
        a = u.get("task_attempts",{}).get(tid,0); sc = get_screen("task_detail"); pm = get_parse_mode(sc)
        vtype=t.get("type","none")
        label="🤖 ᴠᴇʀɪꜰʏ ᴀʀɢᴏ" if vtype=="argo" else ("📸 sᴇɴᴅ sᴄʀᴇᴇɴsʜᴏᴛ" if vtype=="screenshot" else "🎯 ᴄʟᴀɪᴍ")
        await safe_edit(q, render_text(sc["text"], {"task_name": escape_for_mode(t['name'], pm), "task_points": str(t['points']), "attempts": str(a)}), pm, InlineKeyboardMarkup([[InlineKeyboardButton("🔗 ᴏᴘᴇɴ", callback_data=f"task_open_{tid}")], [InlineKeyboardButton(label, callback_data=f"task_verify_{tid}")], [InlineKeyboardButton("🔙", callback_data="main_tasks")]]))

async def credit_task_reward(uid, task, tid):
    oid=safe_object_id(tid)
    if not oid: return False
    r=get_collection("users").update_one({"user_id":uid,"completed_tasks":{"$ne":oid}},{"$inc":{"points":task["points"]},"$addToSet":{"completed_tasks":oid}})
    return r.modified_count==1

async def verify_task_completion(update, context, tid):
    q=update.callback_query; uid=q.from_user.id; t=get_task_by_id(tid)
    if not t or t.get("points",0)<=0: await safe_answer(q,"❌ Task error!"); return
    if not await ensure_user_verified(uid,context): await safe_answer(q,"❌"); return
    oid=safe_object_id(tid)
    if oid in get_user(uid).get("completed_tasks",[]): await safe_answer(q,"✅ Already completed!"); return
    vtype=t.get("type","none")
    if vtype=="argo":
        opened=get_user(uid).get("task_opened_at",{}).get(tid)
        if not opened: await safe_answer(q,"🤖 Open Argo task first!"); return
        now=datetime.utcnow(); reports=list(get_collection("external_reports").find({"used":False,"message_date":{"$gte":opened,"$lte":opened+timedelta(minutes=10)}}).sort("message_date",1).limit(20))
        matches=[]
        for r in reports:
            prefix=str(r.get("masked_user_id","")).split("*",1)[0]
            if prefix and str(uid).startswith(prefix): matches.append(r)
        if len(matches)!=1: await safe_answer(q,"🔍 Argo report not found yet!"); return
        claimed=get_collection("external_reports").find_one_and_update({"_id":matches[0]["_id"],"used":False},{"$set":{"used":True,"claimed_by":uid,"claimed_at":now,"task_id":tid}},return_document=ReturnDocument.AFTER)
        if not claimed: await safe_answer(q,"⏳ Try again!"); return
        if not await credit_task_reward(uid,t,tid): await safe_answer(q,"✅ Already completed!"); return
        await safe_answer(q,f"✅ +{t['points']}!"); await tasks_menu_handler(update,context); return
    if vtype=="screenshot":
        if get_collection("task_verification_requests").find_one({"user_id":uid,"task_id":tid,"status":"pending"}): await safe_answer(q,"⏳ Screenshot pending!"); return
        context.user_data["awaiting_task_screenshot"]=tid
        await safe_edit(q,f"📸 Send screenshot for: {t.get('name','Task')}\n\nAdmin will review it.",parse_mode=None); return
    if vtype=="add_to_group": await safe_answer(q,"ℹ️ Verified automatically after group add."); return
    if not await credit_task_reward(uid,t,tid): await safe_answer(q,"✅ Already completed!"); return
    await safe_answer(q,f"✅ +{t['points']}!"); await tasks_menu_handler(update,context)

async def handle_task_screenshot(update, context):
    uid=update.effective_user.id; tid=context.user_data.get("awaiting_task_screenshot")
    if not tid or not update.message or not update.message.photo: return
    t=get_task_by_id(tid)
    if not t or t.get("type")!="screenshot": context.user_data.pop("awaiting_task_screenshot",None); return
    if get_collection("task_verification_requests").find_one({"user_id":uid,"task_id":tid,"status":"pending"}): await update.message.reply_text("⏳ Already pending."); context.user_data.pop("awaiting_task_screenshot",None); return
    x=get_collection("task_verification_requests").insert_one({"user_id":uid,"task_id":tid,"task_name":t["name"],"points":t["points"],"file_id":update.message.photo[-1].file_id,"status":"pending","created_at":datetime.utcnow()})
    rid=str(x.inserted_id); kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ APPROVE",callback_data=f"taskshot_approve_{rid}"),InlineKeyboardButton("❌ REJECT",callback_data=f"taskshot_reject_{rid}")]])
    for aid in ADMIN_IDS:
        try: await context.bot.send_photo(aid,update.message.photo[-1].file_id,caption=f"📸 Task Verification\n\n👤 User: {uid}\n📋 Task: {t['name']}\n💰 Points: {t['points']}",reply_markup=kb)
        except Exception as e: logger.error("Screenshot send failed: %s",e)
    context.user_data.pop("awaiting_task_screenshot",None); await update.message.reply_text("✅ Submitted for review.")

async def review_task_screenshot(update, context):
    q=update.callback_query
    if q.from_user.id not in ADMIN_IDS: await safe_answer(q,"❌"); return
    approve=q.data.startswith("taskshot_approve_"); rid=q.data.replace("taskshot_approve_","").replace("taskshot_reject_",""); roid=safe_object_id(rid)
    request=get_collection("task_verification_requests").find_one_and_update({"_id":roid,"status":"pending"},{"$set":{"status":"approved" if approve else "rejected","reviewed_at":datetime.utcnow(),"reviewed_by":q.from_user.id}},return_document=ReturnDocument.BEFORE) if roid else None
    if not request: await safe_answer(q,"⚠️ Already processed!"); return
    label="❌ REJECTED"
    if approve:
        task=get_task_by_id(request["task_id"])
        label=f"✅ APPROVED • +{task['points']} pts" if task and await credit_task_reward(request["user_id"],task,request["task_id"]) else "✅ APPROVED • Already credited"
    await q.edit_message_caption(caption=(q.message.caption or "")+f"\n\n{label}",reply_markup=None)
    try: await context.bot.send_message(request["user_id"],f"{label}\n📋 {request.get('task_name','Task')}")
    except Exception: pass
    await safe_answer(q,label)

# ============ KEYBOARD MESSAGE HANDLER (uses unified services) ============
async def handle_keyboard_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text; uid = update.effective_user.id
    if context.user_data.get("awaiting_withdraw_input") and context.user_data.get("withdraw_service"):
        sid = context.user_data.get("withdraw_service"); oid = safe_object_id(sid)
        if not oid:
            for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
            await safe_send(context.bot, uid, "❌ Invalid session!", reply_markup=MAIN_KEYBOARD); return
        s = get_service(sid)
        if not s or not s.get("is_active"):
            for k in ("awaiting_withdraw_input", "withdraw_service", "withdraw_input"): context.user_data.pop(k, None)
            await safe_send(context.bot, uid, "❌ Service unavailable!", reply_markup=MAIN_KEYBOARD); return
        context.user_data["withdraw_input"] = text; context.user_data["awaiting_withdraw_input"] = False
        await process_withdraw(update, context, uid, s, text); return
    if text in ["💳 ᴡɪᴛʜᴅʀᴀᴡ", "🆔 ɢᴇᴛ ɪᴅ"]:
        if not await ensure_user_verified(uid, context): await safe_send(context.bot, uid, "❌", reply_markup=VERIFY_KEYBOARD); return
        services = get_all_services(only_accounts=True)
        if not services: await safe_send(context.bot, uid, get_screen("no_tasks")["text"], reply_markup=MAIN_KEYBOARD); return
        u = get_user(uid); kb = []
        for s in services:
            txt = f"{s.get('emoji','💳')} {s.get('name','Svc')} ({s.get('points',50)} ᴘᴛs)"
            if s.get("required_referrals",0)>0 and len(u.get("referrals",[]))<s["required_referrals"]: txt += " 🔒"
            kb.append([InlineKeyboardButton(txt, callback_data=f"withdraw_service_{s['_id']}")])
        sc = get_screen("withdraw_menu"); await safe_send(context.bot, uid, sc["text"], get_parse_mode(sc), InlineKeyboardMarkup(kb))
    elif text in ["🔗 ʀᴇꜰᴇʀ & ᴇᴀʀɴ"]:
        sc = get_screen("keyboard_refer"); pm = get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"], {"link": f"https://t.me/{context.bot.username}?start=ref_{uid}"}), pm, MAIN_KEYBOARD)
    elif text in ["📊 ᴅᴀsʜʙᴏᴀʀᴅ"]:
        t=get_collection("withdraw_requests").count_documents({}); p=get_collection("withdraw_requests").count_documents({"status":"pending"}); c=get_collection("withdraw_requests").count_documents({"status":"completed"})
        sc = get_screen("keyboard_dashboard"); pm = get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"], {"completed":str(c),"pending":str(p),"total_requests":str(t)}), pm, MAIN_KEYBOARD)
    elif text in ["📋 ᴛᴀsᴋs"]:
        if not await ensure_user_verified(uid, context): await safe_send(context.bot, uid, "❌", reply_markup=VERIFY_KEYBOARD); return
        tasks=list(get_collection("tasks").find({"active":True}))
        if tasks:
            sc = get_screen("keyboard_tasks"); pm = get_parse_mode(sc)
            await safe_send(context.bot, uid, sc["text"], pm, InlineKeyboardMarkup([[InlineKeyboardButton(f"📌 {t['name']} (+{t['points']})", callback_data=f"task_do_{t['_id']}")] for t in tasks]))
        else: await safe_send(context.bot, uid, get_screen("no_tasks")["text"], reply_markup=MAIN_KEYBOARD)
    elif text in ["💰 ʙᴀʟᴀɴᴄᴇ"]:
        u = get_or_create_user(uid, update.effective_user.username or "", update.effective_user.full_name or "")
        sc = get_screen("keyboard_balance"); pm = get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"], {"points":str(u['points'])}), pm, MAIN_KEYBOARD)
    elif text in ["👤 ᴘʀᴏꜰɪʟᴇ"]:
        u = get_or_create_user(uid, update.effective_user.username or "", update.effective_user.full_name or "")
        sc = get_screen("keyboard_profile"); pm = get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"], build_text_context(update.effective_user, u, pm)), pm, MAIN_KEYBOARD)
    elif text in ["🔄 sᴛᴀʀᴛ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ"]: await safe_send(context.bot, uid, "ᴜsᴇ /start!", reply_markup=VERIFY_KEYBOARD)

# ============ ADMIN MESSAGE HANDLER (unchanged, but uses unified services) ============
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS: return
    text = update.message.text.strip(); awaiting = context.user_data.get("awaiting")
    if not awaiting: await handle_keyboard_message(update, context); return
    
    if awaiting == "channel_username":
        try:
            username=text.replace("@","").strip(); chat=await context.bot.get_chat(f"@{username}")
            me=await context.bot.get_me(); bot_member=await context.bot.get_chat_member(chat.id, me.id)
            if bot_member.status != "administrator": await update.message.reply_text("❌ Bot must be admin in channel first!"); return
            try: link=(await context.bot.create_chat_invite_link(chat.id)).invite_link
            except: link=f"https://t.me/{username}"
            get_collection("channels").insert_one({"channel_id":chat.id,"channel_name":f"@{username}","invite_link":link,"active":True,"added_date":datetime.now()})
            increment_verification_version(); context.user_data.clear()
            await update.message.reply_text(f"✅ {chat.title}", reply_markup=create_admin_keyboard())
        except Exception as e: logger.error(f"Channel add: {e}"); await update.message.reply_text("❌")
    elif awaiting == "link_name": context.user_data["link_name"]=text; context.user_data["awaiting"]="link_url"; await update.message.reply_text("🔗 sᴇɴᴅ ᴜʀʟ (https://...):")
    elif awaiting == "link_url":
        if not is_valid_url(text): await update.message.reply_text("❌ Invalid URL! Use https://..."); return
        context.user_data["link_url"]=text; context.user_data["awaiting"]="link_verification_type"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🤖 ARGO AUTO",callback_data="admin_linkverify_argo")],[InlineKeyboardButton("📸 SCREENSHOT",callback_data="admin_linkverify_screenshot")],[InlineKeyboardButton("🚫 NO VERIFICATION",callback_data="admin_linkverify_none")]])
        await update.message.reply_text("🔍 Select verification type:",reply_markup=kb)
    elif awaiting == "payout_channel":
        try:
            chat=await context.bot.get_chat(f"@{text.replace('@','')}")
            get_collection("settings").update_one({"type":"payout_channel"},{"$set":{"channel_id":chat.id,"channel_name":f"@{text.replace('@','')}"}},upsert=True)
            context.user_data.clear(); await update.message.reply_text("✅", reply_markup=create_admin_keyboard())
        except: await update.message.reply_text("❌")
    elif awaiting == "service_name": context.user_data["service_name"]=text; context.user_data["awaiting"]="service_emoji"; await update.message.reply_text("🎨 ᴇᴍᴏᴊɪ:")
    elif awaiting == "service_emoji": context.user_data["service_emoji"]=text; context.user_data["awaiting"]="service_points"; await update.message.reply_text("💰 ᴘᴏɪɴᴛs:")
    elif awaiting == "service_points":
        try:
            pts=int(text)
            if pts <= 0: await update.message.reply_text("❌ Points must be > 0!"); return
            context.user_data["service_points"]=pts; context.user_data["awaiting"]="service_referrals"; await update.message.reply_text("👥 ʀᴇǫ ʀᴇꜰs:")
        except: await update.message.reply_text("❌ ɴᴜᴍʙᴇʀ!")
    elif awaiting == "service_referrals":
        try:
            refs=int(text)
            if refs < 0: await update.message.reply_text("❌ Referrals cannot be negative!"); return
            context.user_data["service_referrals"]=refs; context.user_data["awaiting"]="service_input_label"; await update.message.reply_text("📝 ɪɴᴘᴜᴛ ʟᴀʙᴇʟ (ᴏʀ 'none'):")
        except: await update.message.reply_text("❌ ɴᴜᴍʙᴇʀ!")
    elif awaiting == "service_input_label":
        label=text if text.lower()!="none" else ""
        # Use create_service from database
        service_data = {
            "name": context.user_data.get("service_name","Svc"),
            "emoji": context.user_data.get("service_emoji","💳"),
            "points": context.user_data.get("service_points",50),
            "required_referrals": context.user_data.get("service_referrals",0),
            "input_label": label,
            "platform": "telegram",
            "type": "account"
        }
        create_service(
            name=service_data["name"],
            price=service_data["points"],   # price in INR – same as points for simplicity
            points=service_data["points"],
            platform=service_data["platform"],
            type="account",
            description=service_data.get("input_label", "")
        )
        context.user_data.clear(); await update.message.reply_text("✅ sᴇʀᴠɪᴄᴇ ᴀᴅᴅᴇᴅ!", reply_markup=create_admin_keyboard())
    elif awaiting == "edit_screen_text":
        sid=context.user_data.get("editing_screen")
        get_collection("ui_screens").update_one({"screen_id":sid},{"$set":{"text":text}},upsert=True)
        context.user_data.clear(); await update.message.reply_text(f"✅ *{sid}* ᴜᴘᴅᴀᴛᴇᴅ!", reply_markup=create_admin_keyboard(), parse_mode=ParseMode.MARKDOWN)
    elif awaiting == "edit_service_name": context.user_data["service_name"]=text; context.user_data["awaiting"]="edit_service_emoji"; await update.message.reply_text("🎨 ɴᴇᴡ ᴇᴍᴏᴊɪ:")
    elif awaiting == "edit_service_emoji": context.user_data["service_emoji"]=text; context.user_data["awaiting"]="edit_service_points"; await update.message.reply_text("💰 ɴᴇᴡ ᴘᴏɪɴᴛs:")
    elif awaiting == "edit_service_points":
        try:
            pts=int(text)
            if pts <= 0: await update.message.reply_text("❌ Points must be > 0!"); return
            context.user_data["service_points"]=pts; context.user_data["awaiting"]="edit_service_referrals"; await update.message.reply_text("👥 ɴᴇᴡ ʀᴇǫ ʀᴇꜰs:")
        except: await update.message.reply_text("❌ ɴᴜᴍʙᴇʀ!")
    elif awaiting == "edit_service_referrals":
        try:
            refs=int(text)
            if refs < 0: await update.message.reply_text("❌ Referrals cannot be negative!"); return
            context.user_data["service_referrals"]=refs; context.user_data["awaiting"]="edit_service_input_label"; await update.message.reply_text("📝 ɴᴇᴡ ɪɴᴘᴜᴛ ʟᴀʙᴇʟ:")
        except: await update.message.reply_text("❌ ɴᴜᴍʙᴇʀ!")
    elif awaiting == "edit_service_input_label":
        label=text if text.lower()!="none" else ""; sid=context.user_data.get("editing_service")
        update_data = {
            "name": context.user_data.get("service_name"),
            "emoji": context.user_data.get("service_emoji"),
            "points": context.user_data.get("service_points"),
            "required_referrals": context.user_data.get("service_referrals"),
            "input_label": label
        }
        update_service(sid, update_data)
        context.user_data.clear(); await update.message.reply_text("✅ sᴇʀᴠɪᴄᴇ ᴜᴘᴅᴀᴛᴇᴅ!", reply_markup=create_admin_keyboard())
    elif awaiting == "add_button_text": context.user_data["btn_text"]=text; context.user_data["awaiting"]="add_button_action"; await update.message.reply_text("🔗 sᴇɴᴅ ᴀᴄᴛɪᴏɴ:\n`callback:action_name` ᴏʀ `url:https://...`")
    elif awaiting == "add_button_action":
        if not validate_button_action(text): await update.message.reply_text("❌ Invalid action!"); return
        sid=context.user_data.get("editing_screen"); order=get_collection("ui_buttons").count_documents({"screen_id":sid})+1
        get_collection("ui_buttons").insert_one({"screen_id":sid,"text":context.user_data.get("btn_text","Btn"),"action":text,"order":order,"active":True})
        context.user_data.clear(); await update.message.reply_text("✅ ʙᴜᴛᴛᴏɴ ᴀᴅᴅᴇᴅ!", reply_markup=create_admin_keyboard())

# ============ ADMIN UI HELPERS (using unified services) ============
async def show_admin_services(q):
    services = get_all_services(only_accounts=True)
    kb = []
    for s in services:
        sid = str(s["_id"])
        available = get_service_available_count(sid)
        kb.append([InlineKeyboardButton(f"{'✅' if s.get('is_active') else '❌'} {s.get('emoji','💳')} {s['name']} [{available}]", callback_data=f"admin_toggle_service_{sid}"),
                   InlineKeyboardButton("✏️", callback_data=f"admin_edit_service_{sid}"),
                   InlineKeyboardButton("🗑", callback_data=f"admin_delete_service_{sid}")])
    kb.append([InlineKeyboardButton("➕ ᴀᴅᴅ", callback_data="admin_add_service")]); kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await safe_edit(q, f"💳 sᴇʀᴠɪᴄᴇs: {len(services)}", ParseMode.MARKDOWN, InlineKeyboardMarkup(kb))

async def show_admin_channels(q):
    chs=list(get_collection("channels").find({"active":True}))
    kb=[[InlineKeyboardButton(f"❌ {c['channel_name']}", callback_data=f"admin_remove_ch_{c['_id']}")] for c in chs]
    kb.append([InlineKeyboardButton("➕", callback_data="admin_add_channel")]); kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await safe_edit(q, f"🔗 {len(chs)}", reply_markup=InlineKeyboardMarkup(kb))

async def show_admin_links(q):
    links=list(get_collection("external_links").find({"active":True}))
    kb=[[InlineKeyboardButton(f"❌ {l['name']}", callback_data=f"admin_remove_link_{l['_id']}")] for l in links]
    kb.append([InlineKeyboardButton("➕", callback_data="admin_add_link")]); kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await safe_edit(q, f"🔗 {len(links)}", reply_markup=InlineKeyboardMarkup(kb))

async def show_admin_tasks(q):
    tasks=list(get_collection("tasks").find({}))
    kb=[[InlineKeyboardButton(f"{'✅' if t.get('active',True) else '❌'} {t['name']} ({t['points']})", callback_data=f"admin_toggle_task_{t['_id']}"), InlineKeyboardButton("🗑", callback_data=f"admin_remove_task_{t['_id']}")] for t in tasks]
    kb.append([InlineKeyboardButton("➕", callback_data="admin_add_task")]); kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await safe_edit(q, f"📋 {len(tasks)}", reply_markup=InlineKeyboardMarkup(kb))

async def show_admin_withdrawals(q):
    pending=list(get_collection("withdraw_requests").find({"status":"pending"}).sort("request_date", 1).limit(5))
    if not pending: await safe_edit(q, "✅ ɴᴏɴᴇ!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    else:
        text="💳 *ᴘᴇɴᴅɪɴɢ:*\n\n"; kb=[]
        for req in pending:
            text+=f"#{req['serial_no']} | {req.get('full_name','?')} | {req['points']}ᴘ | {req.get('service_name','?')}\n"
            kb.append([InlineKeyboardButton(f"✅ #{req['serial_no']}", callback_data=f"admin_approve_{req['serial_no']}"), InlineKeyboardButton("❌", callback_data=f"admin_reject_{req['serial_no']}")])
        kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
        await safe_edit(q, text, ParseMode.MARKDOWN, InlineKeyboardMarkup(kb))

async def show_admin_buttons_list(q, sid):
    buttons=get_all_buttons(sid)
    kb=[[InlineKeyboardButton(f"{'✅' if b.get('active',True) else '❌'} {b.get('text','?')}", callback_data=f"admin_toggle_btn_{b['_id']}")] for b in buttons]
    kb.append([InlineKeyboardButton("➕ ɴᴇᴡ", callback_data=f"admin_addbtn_{sid}")]); kb.append([InlineKeyboardButton("🔙", callback_data=f"admin_edit_screen_{sid}")])
    await safe_edit(q, f"🔘 *{sid}*: {len(buttons)}", ParseMode.MARKDOWN, InlineKeyboardMarkup(kb))

# ============ ADMIN CALLBACKS (updated for unified services) ============
async def handle_admin_callbacks(update, context):
    q = update.callback_query; d = q.data; uid = q.from_user.id
    if uid not in ADMIN_IDS: await safe_answer(q, "❌"); return
    
    INPUT_FLOWS = {"admin_add_service", "admin_add_channel", "admin_add_link", "admin_addbtn_", "admin_edittext_", "admin_edit_service_"}
    if not any(d.startswith(flow) for flow in INPUT_FLOWS): context.user_data.clear()
    
    if d == "admin_stats":
        u=get_collection("users").count_documents({}); g=get_collection("groups").count_documents({})
        await safe_edit(q, f"📊 ᴜ:{u} | ɢ:{g} | ᴡ:{get_collection('withdraw_requests').count_documents({})}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    elif d == "admin_cms_screens":
        screens=list(get_collection("ui_screens").find({}))
        kb=[[InlineKeyboardButton(f"📝 {s['screen_id']}", callback_data=f"admin_edit_screen_{s['screen_id']}")] for s in screens]
        kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
        await safe_edit(q, f"🎨 *ᴄᴍs* ({len(screens)} screens)", ParseMode.MARKDOWN, InlineKeyboardMarkup(kb))
    elif d.startswith("admin_edit_screen_"):
        sid=d.replace("admin_edit_screen_",""); screen=get_screen(sid)
        kb=[[InlineKeyboardButton("✏️ ᴛᴇxᴛ", callback_data=f"admin_edittext_{sid}")],[InlineKeyboardButton("🔘 ʙᴜᴛᴛᴏɴs", callback_data=f"admin_editbuttons_{sid}")],[InlineKeyboardButton("🔄 ʀᴇsᴇᴛ", callback_data=f"admin_resetscreen_{sid}")],[InlineKeyboardButton("🔙", callback_data="admin_cms_screens")]]
        await safe_edit(q, f"📝 {sid}\n\n{screen.get('text', '')[:300]}", parse_mode=None, reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("admin_edittext_"):
        sid=d.replace("admin_edittext_",""); context.user_data["awaiting"]="edit_screen_text"; context.user_data["editing_screen"]=sid
        await safe_edit(q, f"✏️ sᴇɴᴅ ᴛᴇxᴛ ꜰᴏʀ *{sid}*", ParseMode.MARKDOWN, InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_cms_screens")]]))
    elif d.startswith("admin_resetscreen_"):
        sid=d.replace("admin_resetscreen_","")
        if sid in DEFAULT_SCREENS: get_collection("ui_screens").update_one({"screen_id":sid},{"$set":DEFAULT_SCREENS[sid]},upsert=True)
        await safe_answer(q, "✅")
    elif d == "admin_cms_services": await show_admin_services(q)
    elif d == "admin_add_service": context.user_data["awaiting"]="service_name"; await safe_edit(q, "➕ sᴇʀᴠɪᴄᴇ ɴᴀᴍᴇ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_cms_services")]]))
    elif d.startswith("admin_edit_service_"):
        sid=d.replace("admin_edit_service_",""); oid=safe_object_id(sid)
        if not oid: await safe_answer(q, "❌"); return
        s=get_service(sid)
        if s: context.user_data["editing_service"]=sid; context.user_data["awaiting"]="edit_service_name"; await safe_edit(q, f"✏️ ᴇᴅɪᴛ {s.get('name','Svc')}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_cms_services")]]))
    elif d.startswith("admin_toggle_service_"):
        sid=d.replace("admin_toggle_service_","")
        s=get_service(sid)
        if s: update_service(sid, {"is_active": not s.get("is_active", True)})
        await safe_answer(q, "✅"); await show_admin_services(q)
    elif d.startswith("admin_delete_service_"):
        sid=d.replace("admin_delete_service_","")
        delete_service(sid)
        await safe_answer(q, "✅"); await show_admin_services(q)
    elif d == "admin_cms_buttons":
        buttons=list(get_collection("ui_buttons").find({}).sort([("screen_id",1),("order",1)]))
        kb=[[InlineKeyboardButton(f"{b['screen_id']} | {b.get('text','?')}", callback_data=f"admin_toggle_btn_{b['_id']}")] for b in buttons[:10]]
        if len(buttons) > 10:
            kb.append([InlineKeyboardButton(f"📄 Page 1/{ (len(buttons)//10)+1 }", callback_data="admin_cms_buttons")])
        kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
        await safe_edit(q, f"🔘 {len(buttons)} buttons (showing first 10)", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("admin_editbuttons_"): await show_admin_buttons_list(q, d.replace("admin_editbuttons_",""))
    elif d.startswith("admin_addbtn_"):
        sid=d.replace("admin_addbtn_",""); context.user_data["awaiting"]="add_button_text"; context.user_data["editing_screen"]=sid
        await safe_edit(q, "➕ sᴇɴᴅ ᴛᴇxᴛ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data=f"admin_editbuttons_{sid}")]]))
    elif d.startswith("admin_toggle_btn_"):
        oid=safe_object_id(d.replace("admin_toggle_btn_",""))
        if not oid: await safe_answer(q, "❌"); return
        b=get_collection("ui_buttons").find_one({"_id":oid})
        if not b: await safe_answer(q, "❌ Not found!"); return
        get_collection("ui_buttons").update_one({"_id":oid},{"$set":{"active":not b.get("active",True)}})
        await safe_answer(q, "✅"); await show_admin_buttons_list(q, b["screen_id"])
    elif d == "admin_manage_channels": await show_admin_channels(q)
    elif d == "admin_add_channel": context.user_data["awaiting"]="channel_username"; await safe_edit(q, "📢 @username:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_manage_channels")]]))
    elif d.startswith("admin_remove_ch_"):
        oid=safe_object_id(d.replace("admin_remove_ch_",""))
        if not oid: await safe_answer(q, "❌"); return
        get_collection("channels").delete_one({"_id":oid})
        increment_verification_version(); await safe_answer(q, "✅"); await show_admin_channels(q)
    elif d.startswith("admin_linkverify_"):
        vtype=d.replace("admin_linkverify_",""); name=context.user_data.get("link_name","Link"); url=context.user_data.get("link_url")
        if vtype not in ("argo","screenshot","none") or not is_valid_url(url or ""): await safe_answer(q,"❌"); return
        get_collection("external_links").insert_one({"name":name,"url":url,"active":True,"verification_type":vtype,"added_date":datetime.now()})
        increment_verification_version(); context.user_data.clear(); await safe_edit(q,f"✅ Added: {vtype} verification",reply_markup=create_admin_keyboard())
    elif d == "admin_manage_links": await show_admin_links(q)
    elif d == "admin_add_link": context.user_data["awaiting"]="link_name"; await safe_edit(q, "➕ ɴᴀᴍᴇ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_manage_links")]]))
    elif d.startswith("admin_remove_link_"):
        oid=safe_object_id(d.replace("admin_remove_link_",""))
        if not oid: await safe_answer(q, "❌"); return
        result=get_collection("external_links").delete_one({"_id":oid})
        if result.deleted_count==1: increment_verification_version()
        await safe_answer(q, "✅" if result.deleted_count else "⚠️"); await show_admin_links(q)
    elif d == "admin_manage_tasks": await show_admin_tasks(q)
    elif d == "admin_add_task": await safe_edit(q, "`/addtask name|pts|type|url`", ParseMode.MARKDOWN, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_manage_tasks")]]))
    elif d.startswith("admin_remove_task_"):
        oid=safe_object_id(d.replace("admin_remove_task_",""))
        if not oid: await safe_answer(q, "❌"); return
        get_collection("tasks").delete_one({"_id":oid}); await safe_answer(q, "✅"); await show_admin_tasks(q)
    elif d.startswith("admin_toggle_task_"):
        oid=safe_object_id(d.replace("admin_toggle_task_",""))
        if not oid: await safe_answer(q, "❌"); return
        t=get_collection("tasks").find_one({"_id":oid})
        if t: get_collection("tasks").update_one({"_id":oid},{"$set":{"active":not t.get("active",True)}})
        await safe_answer(q, "✅"); await show_admin_tasks(q)
    elif d == "admin_withdrawals": await show_admin_withdrawals(q)
    elif d.startswith("admin_approve_"):
        sn=d.replace("admin_approve_","")
        try: sn=int(sn)
        except: await safe_answer(q, "❌"); return
        r=get_collection("withdraw_requests").find_one_and_update({"serial_no":sn,"status":"pending"},{"$set":{"status":"completed","processed_date":datetime.now(),"processed_by":uid},"$unset":{"payout_notification_failed":"","payout_notification_error":""}},return_document=ReturnDocument.BEFORE)
        if r:
            sc = get_screen("withdraw_approved_user"); pm = get_parse_mode(sc)
            await safe_send(context.bot, r["user_id"], render_text(sc["text"],{"token":str(r['serial_no']),"points":str(r['points'])}), pm)
        await safe_answer(q, "✅" if r else "⚠️"); await show_admin_withdrawals(q)
    elif d.startswith("admin_reject_"):
        sn=d.replace("admin_reject_","")
        try: sn=int(sn)
        except: await safe_answer(q, "❌"); return
        r=reject_withdrawal_atomic(sn, uid)
        if r:
            sc = get_screen("withdraw_rejected_user"); pm = get_parse_mode(sc)
            await safe_send(context.bot, r["user_id"], render_text(sc["text"],{"token":str(r['serial_no']),"points":str(r['points'])}), pm)
        await safe_answer(q, "❌" if r else "⚠️"); await show_admin_withdrawals(q)
    elif d == "admin_set_payout": context.user_data["awaiting"]="payout_channel"; await safe_edit(q, "📢 @channel:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="admin_panel")]]))
    elif d == "admin_points_config":
        p=get_points_config()
        await safe_edit(q, f"💎 ʟ1:{p['refer_level_1']} ʟ2:{p['refer_level_2']}\nɢʀᴘ:{p['group_add_small']}-{p['group_add_big']}\nᴍɪɴ:{p['min_withdraw']}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️", callback_data="admin_edit_points")],[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    elif d == "admin_edit_points": await safe_edit(q, "`/setpoints key val`", ParseMode.MARKDOWN, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_points_config")]]))
    elif d == "admin_groups_menu": await safe_edit(q, f"👥 {get_collection('groups').count_documents({})}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊", callback_data="admin_group_stats")],[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    elif d == "admin_group_stats":
        groups=list(get_collection("groups").find({}).limit(10))
        await safe_edit(q, "\n".join([f"📱 {g.get('title','?')} | {g.get('member_count',0)} | 💰{g.get('reward_points',0)}" for g in groups]) if groups else "ɴᴏɴᴇ", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_groups_menu")]]))
    elif d == "admin_broadcast_menu": await safe_edit(q, "📢 ʀᴇᴘʟʏ + `/broadcast`", ParseMode.MARKDOWN, InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))
    elif d == "admin_panel": await safe_edit(q, "🔐 *ᴀᴅᴍɪɴ*", ParseMode.MARKDOWN, create_admin_keyboard())
    else: await safe_answer(q, "?")

def create_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 ᴄᴍs sᴄʀᴇᴇɴs", callback_data="admin_cms_screens")],
        [InlineKeyboardButton("🔘 ᴄᴍs ʙᴜᴛᴛᴏɴs", callback_data="admin_cms_buttons")],
        [InlineKeyboardButton("💳 sᴇʀᴠɪᴄᴇs", callback_data="admin_cms_services")],
        [InlineKeyboardButton("📢 ʙʀᴏᴀᴅᴄᴀsᴛ", callback_data="admin_broadcast_menu")],
        [InlineKeyboardButton("📊 sᴛᴀᴛs", callback_data="admin_stats")],
        [InlineKeyboardButton("🔗 ᴄʜᴀɴɴᴇʟs", callback_data="admin_manage_channels")],
        [InlineKeyboardButton("🔗 ʟɪɴᴋs", callback_data="admin_manage_links")],
        [InlineKeyboardButton("📋 ᴛᴀsᴋs", callback_data="admin_manage_tasks")],
        [InlineKeyboardButton("💳 ᴡɪᴛʜᴅʀᴀᴡᴀʟs", callback_data="admin_withdrawals")],
        [InlineKeyboardButton("📢 ᴘᴀʏᴏᴜᴛ", callback_data="admin_set_payout")],
        [InlineKeyboardButton("💎 ᴘᴏɪɴᴛs", callback_data="admin_points_config")],
        [InlineKeyboardButton("👥 ɢʀᴏᴜᴘs", callback_data="admin_groups_menu")]
    ])

# ============ BOT COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; args=context.args; uid=user.id
    ensure_user_in_db(uid, user.username or "", user.full_name)
    if args and args[0].startswith("ref_") and not get_user(uid).get("pending_referrer"):
        try:
            rid=int(args[0].replace("ref_",""))
            if rid!=uid and not get_user(uid).get("referred_by") and not get_user(uid).get("referral_rewarded"):
                ref_user = get_user(rid)
                if ref_user: get_collection("users").update_one({"user_id":uid},{"$set":{"pending_referrer":rid}})
        except: pass
    u=sync_verification_version(uid)
    not_joined, check_failed = await check_force_join(uid,context)
    if check_failed:
        await safe_send(context.bot, uid, "⚠️ Verification temporarily unavailable!", reply_markup=VERIFY_KEYBOARD); return
    if not_joined:
        text, markup = build_premium_force_join(user, not_joined)
        await safe_send(context.bot, uid, text, ParseMode.MARKDOWN, markup)
        return
    if u.get("external_tasks_completed"):
        get_collection("users").update_one({"user_id":uid},{"$set":{"force_join_completed":True}})
        fresh_u=get_user(uid); sc=get_screen("welcome_back"); pm=get_parse_mode(sc)
        await safe_send(context.bot, uid, render_text(sc["text"],build_text_context(user,fresh_u,pm)), pm, MAIN_KEYBOARD)
    else:
        get_collection("users").update_one({"user_id":uid},{"$set":{"force_join_completed":True}})
        links=list(get_collection("external_links").find({"active":True}))
        if links:
            sc=get_screen("external_tasks"); pm=get_parse_mode(sc)
            kb=[[InlineKeyboardButton(f"🔗 {l['name']}", url=l['url'])] for l in links if is_valid_url(l.get("url",""))]
            kb.append([InlineKeyboardButton("🔍 ᴠᴇʀɪꜰʏ ᴀʟʟ", callback_data="ext_tasks_complete")])
            await safe_send(context.bot, uid, render_text(sc["text"],build_text_context(user,u,pm)), pm, InlineKeyboardMarkup(kb))
        else:
            get_collection("users").update_one({"user_id":uid},{"$set":{"external_tasks_completed":True,"verification_version":get_verification_version()},"$unset":{"verification.external_required":"","verification.external_attempts":""}})
            await process_pending_group_rewards(uid,context)
            if get_user(uid).get("pending_referrer"): await handle_referral_points(uid,get_user(uid)["pending_referrer"],context)
            fresh_u=get_user(uid); sc=get_screen("verification_done"); pm=get_parse_mode(sc)
            await safe_send(context.bot, uid, render_text(sc["text"],build_text_context(user,fresh_u,pm)), pm, MAIN_KEYBOARD)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await safe_send(context.bot, update.effective_user.id, "🔐 *ᴀᴅᴍɪɴ*", ParseMode.MARKDOWN, create_admin_keyboard())

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or not update.message.reply_to_message: return
    msg=update.message.reply_to_message; total=get_collection("users").count_documents({})
    s=await update.message.reply_text(f"📢 0/{total}"); ok=fail=0
    for i,u in enumerate(get_collection("users").find({}),1):
        try: await msg.copy(chat_id=u["user_id"]); ok+=1
        except: fail+=1
        if i%20==0: await s.edit_text(f"📢 ✅{ok} ❌{fail} {i}/{total}")
        await asyncio.sleep(0.05)
    await s.edit_text(f"✅ {ok}/{total}")

async def broadcast_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or not update.message.reply_to_message: return
    ok=fail=0
    for g in get_collection("groups").find({"reward_given":True}):
        try: await update.message.reply_to_message.copy(chat_id=g["chat_id"]); ok+=1
        except Exception as e: logger.error(f"Group broadcast failed {g.get('chat_id')}: {e}"); fail+=1
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"✅ {ok} sent, {fail} failed")

async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("Use Admin Panel → Channels → Add Channel for validated channel setup.")

async def add_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        raw = update.message.text.split(maxsplit=1)
        if len(raw) != 2: raise ValueError("Missing arguments")
        p = [x.strip() for x in raw[1].split("|", 1)]
        if len(p) != 2: raise ValueError("Invalid format")
        name, url = p
        if not name or not is_valid_url(url): await update.message.reply_text("❌ Invalid URL! Use https://..."); return
        get_collection("external_links").insert_one({"name": name, "url": url, "active": True, "added_date": datetime.now()})
        increment_verification_version(); await update.message.reply_text("✅")
    except Exception as e: logger.error("Add link failed: %s", e); await update.message.reply_text("❌ Format: /addlink Name | https://...")

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        raw = update.message.text.split(maxsplit=1)
        if len(raw) != 2: raise ValueError("Missing arguments")
        p = [x.strip() for x in raw[1].split("|")]
        if len(p) < 3: raise ValueError("Invalid format")
        name, pts_str, task_type = p[0], p[1], p[2]
        url = p[3].strip() if len(p) > 3 else ""
        pts = int(pts_str)
        if pts <= 0: await update.message.reply_text("❌ Task points must be > 0!"); return
        allowed_types = ("argo", "screenshot", "none", "add_to_group")
        task_type = task_type.lower()
        if task_type not in allowed_types: await update.message.reply_text("❌ Type: argo | screenshot | none | add_to_group"); return
        if task_type != "add_to_group" and not is_valid_url(url): await update.message.reply_text("❌ Invalid task URL!"); return
        get_collection("tasks").insert_one({"name": name, "points": pts, "type": task_type, "url": url, "active": True, "created_date": datetime.now(), "task_key": f"task_{datetime.now().timestamp()}"})
        await update.message.reply_text(f"✅ Task added\n📋 {name}\n💰 {pts} pts\n🔍 {task_type}")
    except Exception as e: logger.error("Add task failed: %s", e); await update.message.reply_text("❌ Format: /addtask Name | Points | type | url")

async def set_points_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        k,v=update.message.text.replace("/setpoints ","").split()
        pts=get_points_config()
        if k in pts:
            val = int(v)
            if val < 0: await update.message.reply_text("❌ Points cannot be negative!"); return
            pts[k]=val; update_points_config(pts); await update.message.reply_text(f"✅ {k}={v}")
        else: await update.message.reply_text("❌ Invalid key!")
    except: await update.message.reply_text("❌ Format: /setpoints key value")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    context.user_data.clear(); await update.message.reply_text("❌", reply_markup=create_admin_keyboard())

async def handle_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm=update.my_chat_member
    old_status, new_status = cm.old_chat_member.status, cm.new_chat_member.status
    if new_status != "administrator": return
    if old_status == "administrator": return
    chat, actor = cm.chat, cm.from_user
    if not actor: return
    ensure_user_in_db(actor.id, actor.username or "", actor.full_name or "")
    try: mc=await context.bot.get_chat_member_count(chat.id)
    except: mc=0
    cfg,pts=get_points_config(),0
    if 100<=mc<=1000: pts=cfg["group_add_small"]
    elif 1001<=mc<=2000: pts=cfg["group_add_medium"]
    elif 2001<=mc<=3000: pts=cfg["group_add_m2"]
    elif 3001<=mc<=5000: pts=cfg["group_add_m3"]
    elif 5001<=mc<=10000: pts=cfg["group_add_m4"]
    elif mc>10000: pts=cfg["group_add_big"]
    if pts<=0:
        sc=get_screen("group_no_reward"); pm=get_parse_mode(sc)
        await safe_send(context.bot, actor.id, render_text(sc["text"],{"member_count":str(mc)}), pm); return
    existing=get_collection("groups").find_one({"chat_id":chat.id})
    if existing:
        owner_id = existing.get("reward_claimed_by")
        if not owner_id: logger.error(f"Group {chat.id} has no reward owner"); return
        pts = existing.get("reward_points", pts)
        get_collection("groups").update_one({"chat_id":chat.id},{"$set":{"title":chat.title or "?","last_member_count":mc}})
        # Auto-complete add_to_group task for the owner
        add_to_group_tasks = list(get_collection("tasks").find({"type": "add_to_group", "active": True}))
        for task in add_to_group_tasks:
            tid = str(task["_id"])
            get_collection("users").update_one(
                {"user_id": owner_id, "completed_tasks": {"$ne": ObjectId(tid)}},
                {"$addToSet": {"completed_tasks": ObjectId(tid)}}
            )
    else:
        owner_id = actor.id
        get_collection("groups").insert_one({"chat_id":chat.id,"title":chat.title or "?","member_count":mc,"reward_claimed_by":owner_id,"reward_points":pts,"reward_given":False,"reward_status":"pending","added_at":datetime.now()})
        # Auto-complete add_to_group task for the first adder
        add_to_group_tasks = list(get_collection("tasks").find({"type": "add_to_group", "active": True}))
        for task in add_to_group_tasks:
            tid = str(task["_id"])
            get_collection("users").update_one(
                {"user_id": owner_id, "completed_tasks": {"$ne": ObjectId(tid)}},
                {"$addToSet": {"completed_tasks": ObjectId(tid)}}
            )
    if await ensure_user_verified(owner_id,context):
        if credit_group_reward_atomic(chat.id,owner_id,chat.title,mc,pts):
            sc=get_screen("group_reward"); pm=get_parse_mode(sc)
            await safe_send(context.bot, owner_id, render_text(sc["text"],{"group_name":escape_for_mode(chat.title or "?",pm),"member_count":str(mc),"reward_points":str(pts)}), pm)
    else:
        create_group_pending_reward(chat.id,owner_id,chat.title,mc,pts)
        sc=get_screen("group_reward_pending"); pm=get_parse_mode(sc)
        await safe_send(context.bot, owner_id, render_text(sc["text"],{"group_name":escape_for_mode(chat.title or "?",pm),"reward_points":str(pts)}), pm)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=(type(context.error), context.error, context.error.__traceback__) if context.error else None)

def main():
    init_db(); seed_cms_data()
    Thread(target=run_web_server, daemon=True).start()
    Thread(target=start_argo_listener_thread, daemon=True).start()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(16)
        .build()
    )
    for cmd,func in [("start",start),("admin",admin_command),("broadcast",broadcast_command),("broadcastgroups",broadcast_groups_command),("addchannel",add_channel_command),("addlink",add_link_command),("addtask",add_task_command),("setpoints",set_points_command),("cancel",cancel_command)]:
        app.add_handler(CommandHandler(cmd,func))
    app.add_handler(MessageHandler(filters.PHOTO, handle_task_screenshot))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS) & ~filters.COMMAND, handle_admin_messages))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard_message))
    app.add_handler(ChatMemberHandler(handle_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_handler)
    logger.info("🚀 Production CMS Bot Started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
