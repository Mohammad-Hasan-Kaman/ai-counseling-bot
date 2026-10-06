# -*- coding: utf-8 -*-
import os
import io
import re
import json
import logging
import sqlite3
from pathlib import Path
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio
from concurrent.futures import ThreadPoolExecutor

from crawler import crawl_available_slots
from internal_ai_engine import SpiralMatchEngine, record_learning_event, record_feedback_direct, get_learning_stats
from excel_to_json import convert_excel_to_json
from ghq_analyzer import GHQ_QUESTIONS, calculate_ghq_scores, get_ghq_message
from user_db import init_user_db, validate_phone_and_get_count, save_user_consultation, replace_consultants, get_consultants_stats
from admin_tools import export_users_excel, get_broadcast_targets, send_broadcast
from config import (
    BOT_TOKEN,
    PROFILES_JSON, APPOINTMENTS_DB, MAPPING_JSON, USER_RECORDS_DB,
    CRAWLER_INTERVAL_HOURS, ADMIN_USER_IDS, ADMIN_PANEL_PASSWORD,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Conversation states
(NAME, PHONE, GENDER, AGE, TOPIC, PREV_THERAPY,
 PREV_DETAIL, EXPECTATION, PREFERRED_GENDER, BRANCH,
 ASK_GHQ, GHQ_QUESTION) = range(12)

user_data_cache: dict = {}

YES_NO_KB = ReplyKeyboardMarkup([["بله", "خیر"], ["شروع مجدد"]], resize_keyboard=True)
GENDER_KB = ReplyKeyboardMarkup([["آقا", "خانم"], ["شروع مجدد"]], resize_keyboard=True)
PREF_KB = ReplyKeyboardMarkup([["آقا", "خانم", "فرقی ندارد"], ["شروع مجدد"]], resize_keyboard=True)
BRANCH_KB = ReplyKeyboardMarkup([["ظفر", "خیابان ایران", "اهمیتی ندارد"], ["شروع مجدد"]], resize_keyboard=True)
REMOVE_KB = ReplyKeyboardRemove()
RESTART_KB = ReplyKeyboardMarkup([["شروع مجدد"]], resize_keyboard=True)

engine = SpiralMatchEngine()
init_user_db()


def _profile_url(name: str) -> str:
    try:
        with open(MAPPING_JSON, "r", encoding="utf-8") as f:
            m = json.load(f)
            if name in m:
                return m[name]
            for k, v in m.items():
                if name in k or k in name:
                    return v
    except Exception:
        pass
    return f"https://nikravan.org/team/{name.replace(' ', '-')}.html"


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_USER_IDS


# Admin sessions authenticated with password: {uid: login timestamp}
import time as _time
ADMIN_SESSION_TTL = 4 * 3600   # valid for 4 hours
_admin_sessions: dict = {}


def _admin_authenticated(uid: int) -> bool:
    """Admin status + logged in with a valid password within the last 4 hours"""
    ts = _admin_sessions.get(uid)
    if ts is None:
        return False
    if _time.time() - ts > ADMIN_SESSION_TTL:
        _admin_sessions.pop(uid, None)
        return False
    return True


CRAWL_STATUS_LABELS = {
    "free": "🟢 نوبت آزاد",
    "waiting": "🟡 لیست انتظار",
    "no_available": "🔴 بدون نوبت فعلی",
    "no_free_slots": "🔴 بدون نوبت آزاد",
    "phone_only": "📞 صرفاً تماس تلفنی",
    "no_table": "⚠️ جدول نوبت یافت نشد",
}


def _crawl_and_report() -> str:
    try:
        crawl_available_slots()
        conn = sqlite3.connect(str(APPOINTMENTS_DB))
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM appointments GROUP BY status").fetchall())
        conn.close()

        free = rows.get("free", 0)
        msg = f"✅ کراول نوبت‌ها به پایان رسید.\n\n**وضعیت مشاوران در سایت:**\n"
        for s, c in rows.items():
            label = CRAWL_STATUS_LABELS.get(s, s)
            msg += f"{label}: {c} مشاور\n"

        if free > 0:
            msg += f"\n🎯 {free} نوبت آزاد پیدا شد و در سیستم ثبت شد."
        else:
            msg += "\nℹ️ در این لحظه هیچ نوبت آزادی در سایت موجود نیست."
        return msg
    except Exception as e:
        return f"❌ خطای کراولر: {e}"


def _parse_ghq_choice(text: str, opts: list) -> int | None:
    clean_text = text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    m = re.search(r"^(\d+)", clean_text)
    if m and int(m.group(1)) in range(len(opts)):
        return int(m.group(1))
    for i, opt in enumerate(opts):
        if opt in clean_text or clean_text in opt:
            return i
    m_any = re.search(r"(\d+)", clean_text)
    if m_any and int(m_any.group(1)) in range(len(opts)):
        return int(m_any.group(1))
    return None


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    user_data_cache.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("فرآیند لغو شد. برای شروع مجدد دکمه زیر را لمس کنید:", reply_markup=RESTART_KB)
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    user_data_cache[uid] = {}
    context.user_data.clear()
    # Intake-form active flag — so the admin's reply during the form doesn't go to the broadcast
    context.user_data["_in_main_conv"] = True

    welcome_text = (
        "🌸 **سلام! به سامانه هوشمند پذیرش مرکز «خانواده نیک‌روان» خوش آمدید.**\n\n"
        "ما با بررسی دقیق شرایط و نیاز شما، مناسب‌ترین مشاوران مرکز را معرفی می‌کنیم.\n\n"
        "لطفاً **نام و نام خانوادگی** خود را وارد فرمایید:"
    )
    await update.message.reply_text(welcome_text, reply_markup=REMOVE_KB, parse_mode=ParseMode.MARKDOWN)
    return NAME


async def name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    user_data_cache[str(update.effective_user.id)]["full_name"] = text
    await update.message.reply_text("لطفاً شماره تماس خود را وارد کنید (مانند 09123456789):", reply_markup=REMOVE_KB)
    return PHONE


async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    phone = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not (phone.isdigit() and len(phone) == 11 and phone.startswith("09")):
        await update.message.reply_text("⚠️ شماره تماس نامعتبر است. لطفاً یک شماره ۱۱ رقمی معتبر با 09 وارد کنید:", reply_markup=REMOVE_KB)
        return PHONE
        
    full_name = user_data_cache[str(update.effective_user.id)].get("full_name", "")
    
    # Check that the name doesn't conflict with the registered phone number
    is_valid, req_count, _ = validate_phone_and_get_count(phone, full_name, update.effective_user.id)
    if not is_valid:
        error_msg = (
            "⚠️ **خطای احراز هویت شماره تماس:**\n\n"
            "این شماره تماس قبلاً با نام و مشخصات دیگری در سیستم ثبت شده است.\n\n"
            "جهت حفظ محرمانگی و جلوگیری از تداخل سوابق پرونده، لطفاً شماره تماس اختصاصی خود را وارد نمایید یا نام خود را مطابق ثبت اولیه وارد فرمایید."
        )
        await update.message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=REMOVE_KB)
        return PHONE
        
    user_data_cache[str(update.effective_user.id)]["phone"] = phone
    user_data_cache[str(update.effective_user.id)]["request_count"] = req_count
    
    # Show a welcome-back message for returning clients
    if req_count > 1:
        await update.message.reply_text(
            f"🌹 خوش‌آمدید {full_name} عزیز! این **بار {req_count}‌ام** است که در مرکز خانواده نیک‌روان در خدمت شما هستیم.",
            parse_mode=ParseMode.MARKDOWN
        )

    await update.message.reply_text("جنسیت شما:", reply_markup=GENDER_KB)
    return GENDER


async def gender_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if text not in ("آقا", "خانم"):
        await update.message.reply_text("لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=GENDER_KB)
        return GENDER
        
    user_data_cache[str(update.effective_user.id)]["gender"] = text
    await update.message.reply_text("سن شما (به عدد):", reply_markup=REMOVE_KB)
    return AGE


async def age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if not text.isdigit():
        await update.message.reply_text("لطفاً سن خود را تنها به صورت عدد وارد فرمایید:")
        return AGE
        
    user_data_cache[str(update.effective_user.id)]["age"] = int(text)
    await update.message.reply_text("لطفاً موضوع اصلی یا مشکلی که برای آن به مشاوره نیاز دارید را بنویسید:", reply_markup=REMOVE_KB)
    return TOPIC


async def topic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    user_data_cache[str(update.effective_user.id)]["topic"] = text
    await update.message.reply_text("آیا سابقه مراجعه قبلی به روان‌شناس یا روان‌پزشک داشته‌اید؟", reply_markup=YES_NO_KB)
    return PREV_THERAPY


async def prev_therapy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if text not in ("بله", "خیر"):
        await update.message.reply_text("لطفاً یکی از گزینه‌های «بله» یا «خیر» را انتخاب کنید:", reply_markup=YES_NO_KB)
        return PREV_THERAPY
        
    user_data_cache[str(update.effective_user.id)]["has_prev_therapy"] = (text == "بله")
    if text == "بله":
        await update.message.reply_text("در صورت تمایل توضیح مختصری درباره درمان قبلی بنویسید (در غیر این صورت یک پیام کوتاه ارسال کنید):", reply_markup=REMOVE_KB)
        return PREV_DETAIL
        
    await update.message.reply_text("انتظار یا رویکرد مدنظر شما از جلسات مشاوره چیست؟", reply_markup=REMOVE_KB)
    return EXPECTATION


async def prev_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    user_data_cache[str(update.effective_user.id)]["prev_detail"] = text
    await update.message.reply_text("انتظار یا رویکرد مدنظر شما از جلسات مشاوره چیست؟", reply_markup=REMOVE_KB)
    return EXPECTATION


async def expectation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    user_data_cache[str(update.effective_user.id)]["expectation"] = text
    await update.message.reply_text("ترجیح می‌دهید جنسیت مشاور شما چه باشد؟", reply_markup=PREF_KB)
    return PREFERRED_GENDER


async def preferred_gender_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if text not in ("آقا", "خانم", "فرقی ندارد"):
        await update.message.reply_text("لطفاً یکی از گزینه‌ها را انتخاب کنید:", reply_markup=PREF_KB)
        return PREFERRED_GENDER

    user_data_cache[str(update.effective_user.id)]["preferred_gender"] = text
    await update.message.reply_text(
        "کدام شعبه مرکز مشاوره خانواده نیک‌روان برای شما مناسب‌تر است؟",
        reply_markup=BRANCH_KB
    )
    return BRANCH


async def branch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if text not in ("ظفر", "خیابان ایران", "اهمیتی ندارد"):
        await update.message.reply_text("لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=BRANCH_KB)
        return BRANCH

    user_data_cache[str(update.effective_user.id)]["branch"] = text

    ghq_intro = (
        "📋 **آزمون غربالگری سلامت عمومی (GHQ-28)**\n\n"
        "تکمیل این آزمون اختیاری است اما به سیستم کمک می‌کند شرایط شما را با دقت بالینی بسیار بالاتری تحلیل کند.\n\n"
        "آیا مایل هستید این تست کوتاه (۲۸ سؤال) را پاسخ دهید؟"
    )
    await update.message.reply_text(ghq_intro, reply_markup=YES_NO_KB, parse_mode=ParseMode.MARKDOWN)
    return ASK_GHQ


async def ask_ghq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
    if text not in ("بله", "خیر"):
        await update.message.reply_text("لطفاً یکی از گزینه‌های «بله» یا «خیر» را انتخاب کنید:", reply_markup=YES_NO_KB)
        return ASK_GHQ
        
    if text == "بله":
        context.user_data["ghq_answers"] = []
        context.user_data["ghq_index"] = 0
        await _send_ghq_question(update, context)
        return GHQ_QUESTION
        
    await _recommend(update, context)
    return ConversationHandler.END


async def _send_ghq_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data["ghq_index"]
    q, opts = GHQ_QUESTIONS[idx]
    
    kb = [[f"{i} - {o}"] for i, o in enumerate(opts)]
    kb.append(["شروع مجدد"])
    
    msg_text = (
        f"📝 **سؤال {idx+1} از ۲۸:**\n\n"
        f"{q}\n\n"
        f"👇 *لطفاً یکی از گزینه‌های زیر را انتخاب فرمایید:*"
    )
    await update.message.reply_text(
        msg_text,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )


async def ghq_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()
    if raw_text in ["شروع مجدد", "شروع", "/start"]:
        return await start(update, context)
        
    idx = context.user_data.get("ghq_index", 0)
    if idx >= len(GHQ_QUESTIONS):
        await _recommend(update, context)
        return ConversationHandler.END
        
    _, opts = GHQ_QUESTIONS[idx]
    choice = _parse_ghq_choice(raw_text, opts)
    
    if choice is None:
        await update.message.reply_text("⚠️ لطفاً تنها یکی از گزینه‌های کیبورد زیر را انتخاب کنید:")
        await _send_ghq_question(update, context)
        return GHQ_QUESTION
        
    context.user_data["ghq_answers"].append(choice)
    context.user_data["ghq_index"] += 1
    
    if context.user_data["ghq_index"] >= len(GHQ_QUESTIONS):
        scores = calculate_ghq_scores(context.user_data["ghq_answers"])
        user_data_cache[str(update.effective_user.id)]["ghq_scores"] = scores
        
        await update.message.reply_text(get_ghq_message(scores), reply_markup=REMOVE_KB, parse_mode=ParseMode.MARKDOWN)
        await _recommend(update, context)
        return ConversationHandler.END
        
    await _send_ghq_question(update, context)
    return GHQ_QUESTION


async def _recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = user_data_cache.get(uid, {})
    if not data:
        await update.message.reply_text("اطلاعاتی یافت نشد. لطفاً از دکمه «شروع مجدد» استفاده فرمایید.", reply_markup=RESTART_KB)
        return ConversationHandler.END

    user_info = {
        "full_name": data.get("full_name", ""),
        "phone": data.get("phone", ""),
        "gender": data.get("gender", ""),
        "age": data.get("age", 0),
        "topic": data.get("topic", ""),
        "has_prev_therapy": data.get("has_prev_therapy", False),
        "prev_detail": data.get("prev_detail", ""),
        "expectation": data.get("expectation", ""),
        "preferred_gender": data.get("preferred_gender", "فرقی ندارد"),
        "branch": data.get("branch", "اهمیتی ندارد"),
        "location": data.get("branch", "اهمیتی ندارد"),
        "ghq_scores": data.get("ghq_scores", None),
    }

    await update.message.reply_text("🔄 در حال پردازش در موتور هوشمند مرکز خانواده نیک‌روان...", reply_markup=REMOVE_KB)

    recs = engine.match(user_info)

    if not recs:
        await update.message.reply_text("متأسفانه در حال حاضر مشاوری با این مشخصات یافت نشد.", reply_markup=RESTART_KB)
    else:
        req_number = save_user_consultation(update.effective_user.id, user_info, recs)
        
        ghq_lvl = "severe" if (user_info["ghq_scores"] and user_info["ghq_scores"].get("total", 0) >= 43) else "normal"
        for r in recs:
            record_learning_event(r["name"], user_info["topic"], ghq_level=ghq_lvl, success=True)

        count_badge = f" (ثبت درخواست نوبت {req_number} شما)" if req_number > 1 else ""
        msg = f"🎯 **مشاوران برگزیده مرکز خانواده نیک‌روان برای شما{count_badge}:**\n\n"
        for i, r in enumerate(recs, 1):
            msg += f"🏅 **گزینه {i}: {r['name']}**\n"
            msg += f"📋 دلیل پیشنهاد: {r['reason']}\n"
            msg += f"🔗 [مشاهده نوبت‌ها و رزرو در سایت خانواده نیک‌روان]({_profile_url(r['name'])})\n\n"
            
        msg += "✨ برای ارزیابی مجدد یا ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید."
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=RESTART_KB)

    user_data_cache.pop(uid, None)
    context.user_data.clear()
    return ConversationHandler.END


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Panel entry: /admin or /admin PASSWORD"""
    uid = update.effective_user.id
    if not _is_admin(uid):
        return await update.message.reply_text("دسترسی غیرمجاز.")

    supplied = (context.args or [""])[0] if context.args else ""
    if not supplied:
        # Bare /admin: show the menu if there's an active session, otherwise ask for the password
        if _admin_authenticated(uid):
            return await _send_admin_menu(update)
        return await update.message.reply_text(
            "🔐 **ورود به پنل مدیریت**\n\n"
            "لطفاً رمز عبور را وارد کنید:\n"
            "`/admin YOUR_PASSWORD`",
            parse_mode=ParseMode.MARKDOWN,
        )

    if supplied == ADMIN_PANEL_PASSWORD:
        _admin_sessions[uid] = _time.time()
        await update.message.reply_text("✅ احراز هویت موفق! خوش آمدید.")
        return await _send_admin_menu(update)

    await update.message.reply_text("❌ رمز عبور نادرست است.")


async def _send_admin_menu(update: Update):
    await update.message.reply_text(
        f"👑 **پنل مدیریت مرکز مشاوره خانواده نیک‌روان**\nمشاوران فعال: {len(engine.profiles)}\n\n"
        "📣 **ارسال پیام همگانی (برادکست):**\n"
        "/broadcast - شروع ارسال پیام همگانی (یا روی پیام دلخواه ریپلای کنید)\n"
        "/cancel_broadcast - لغو عملیات برادکست\n\n"
        "📥 **دیتا:**\n"
        "/export - دریافت فایل اکسل دیتای کاربران\n"
        "/upload - آپلود فایل اکسل جدید مشاوران (تبدیل + ذخیره در دیتابیس)\n\n"
        "⚙️ **سیستم:**\n"
        "/crawl - به‌روزرسانی دستی نوبت‌ها از سایت\n"
        "/stats - آمار نوبت‌ها و مراجعین\n"
        "/feedback - ثبت بازخورد برای سیستم خودآموز (موفق/ناموفق)\n"
        "/learning - وضعیت یادگیری فعلی موتور\n"
        "/logout - خروج از پنل",
        parse_mode=ParseMode.MARKDOWN
    )


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_admin(update.effective_user.id):
        _admin_sessions.pop(update.effective_user.id, None)
        await update.message.reply_text("از پنل مدیریت خارج شدید.")


async def _require_admin_session(update: Update) -> bool:
    """Check admin password authentication; sends an appropriate message when no session exists"""
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("دسترسی غیرمجاز.")
        return False
    if not _admin_authenticated(uid):
        await update.message.reply_text(
            "🔐 ابتدا وارد پنل شوید:\n`/admin YOUR_PASSWORD`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    return True


_executor = ThreadPoolExecutor(max_workers=2)

def _safe_background_crawl():
    try:
        crawl_available_slots()
        log.info("✅ Background crawl finished successfully.")
    except Exception as e:
        log.error("❌ Background crawl error: %s", e)


async def _run_crawl_and_notify(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_executor, _crawl_and_report)
    except Exception as e:
        result = f"خطا: {e}"
    try:
        await context.bot.send_message(chat_id=user_id, text=result)
    except Exception:
        pass


async def crawl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin_session(update):
        return
    await update.message.reply_text("⏳ فرآیند کراول در پس‌زمینه آغاز شد...")
    asyncio.create_task(_run_crawl_and_notify(update.effective_user.id, context))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin_session(update):
        return

    STATUS_LABELS = {
        "free": "🟢 نوبت آزاد",
        "waiting": "🟡 لیست انتظار",
        "no_available": "🔴 بدون نوبت فعلی",
        "no_free_slots": "🔴 بدون نوبت آزاد",
        "phone_only": "📞 صرفاً تماس تلفنی",
        "no_table": "⚠️ جدول نوبت یافت نشد",
    }

    try:
        conn = sqlite3.connect(str(APPOINTMENTS_DB))
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM appointments GROUP BY status").fetchall())
        top = conn.execute("SELECT counselor_name, COUNT(*) FROM appointments WHERE status='free' GROUP BY counselor_name ORDER BY 2 DESC LIMIT 5").fetchall()
        top_all = conn.execute(
            "SELECT DISTINCT counselor_name FROM appointments WHERE status='free'"
        ).fetchall()
        free_counselors = conn.execute(
            "SELECT COUNT(DISTINCT counselor_name) FROM appointments WHERE status='free'"
        ).fetchone()[0]
        total_free_slots = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE status='free'"
        ).fetchone()[0]
        site_total_counselors = conn.execute(
            "SELECT COUNT(DISTINCT counselor_name) FROM appointments"
        ).fetchone()[0]
        # How many active engine (Excel) counselors have free slots — fuzzy name matching
        from internal_ai_engine import names_match
        active_with_free = sum(
            1 for p in engine.profiles
            if any(names_match(p["clean_name"], r[0]) for r in top_all)
        )
        conn.close()
    except Exception:
        rows = {}
        top = []
        top_all = []
        free_counselors = 0
        total_free_slots = 0
        site_total_counselors = 0
        active_with_free = 0

    try:
        u_conn = sqlite3.connect(str(USER_RECORDS_DB))
        total_users = u_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_requests = u_conn.execute("SELECT COUNT(*) FROM user_requests").fetchone()[0]
        u_conn.close()
    except Exception:
        total_users, total_requests = 0, 0

    msg = (
        f"📊 **آمار سیستم خانواده نیک‌روان:**\n"
        f"👥 مراجعین ثبت‌شده: {total_users} نفر\n"
        f"📑 کل ارزیابی‌های انجام‌شده: {total_requests} بار\n"
        f"👩‍⚕️ تعداد مشاوران فعال در موتور: {len(engine.profiles)} نفر\n\n"
        f"**وضعیت نوبت‌ها:**\n"
    )
    for s, c in rows.items():
        label = STATUS_LABELS.get(s, s)
        msg += f"{label}: {c} مشاور\n"

    # Free-slot stats from the latest crawl — whole site vs. active engine counselors
    if free_counselors > 0:
        msg += (
            f"\n🎯 **نتیجه آخرین کراول:** از {site_total_counselors} مشاور ثبت‌شده در سایت، "
            f"**{free_counselors} نفر** دارای نوبت آزاد بودند "
            f"(مجموعاً {total_free_slots} نوبت خالی)\n"
            f"📌 از {len(engine.profiles)} مشاور فعال ربات (اکسل)، "
            f"{active_with_free} نفر الان نوبت آزاد دارند\n"
        )
        msg += "\n🔥 جزئیات نوبت‌های آزاد:\n"
        for n, c in top:
            msg += f"• {n}: {c} نوبت\n"
        if len(top) == 5 and free_counselors > 5:
            msg += f"... و {free_counselors - 5} مشاور دیگر\n"
    else:
        msg += "\nℹ️ در آخرین کراول هیچ نوبت آزادی پیدا نشد.\nبا /crawl می‌توانید به‌روزرسانی دستی بگیرید."
    if not any(s == "free" for s in rows):
        msg += "\nℹ️ در آخرین کراول هیچ نوبت آزادی پیدا نشد.\nبا /crawl می‌توانید به‌روزرسانی دستی بگیرید."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


FEEDBACK_CONCEPTS = {
    "1": "اضطراب", "2": "افسردگی", "3": "وسواس", "4": "زوج_ازدواج",
    "5": "کودک", "6": "نوجوان_جوان", "7": "والد_فرزند", "8": "ارتباط_تعارض",
    "9": "توسعه_فردی", "10": "حقوقی", "11": "پزشکی_روانپزشکی",
}
FEEDBACK_MENU = (
    "🧠 **ثبت بازخورد برای سیستم خودآموز**\n\n"
    "فرمت: `/feedback شماره‌مفهوم نام‌مشاور بله|خیر`\n"
    "• بله = مراجعه موفق بود (تقویت مشاور در این موضوع)\n"
    "• خیر = مراجع نرفت/راضی نبود (کاهش وزن مشاور در این موضوع)\n\n"
    "**شماره مفهوم‌ها:**\n"
    "۱ اضطراب | ۲ افسردگی | ۳ وسواس | ۴ زوج/ازدواج | ۵ کودک | ۶ نوجوان\n"
    "۷ والد-فرزند | ۸ ارتباط/تعارض | ۹ توسعه فردی | ۱۰ حقوقی | ۱۱ روانپزشکی\n\n"
    "**نمونه:** `/feedback 1 زهرا مجاهدی خیر`\n\n"
    "📊 برای دیدن وضعیت فعلی یادگیری: `/learning`"
)


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record an admin's positive/negative feedback for a pair (counselor × concept)"""
    if not await _require_admin_session(update):
        return

    args = (context.args or [])
    if not args:
        return await update.message.reply_text(FEEDBACK_MENU, parse_mode=ParseMode.MARKDOWN)

    if len(args) < 3:
        return await update.message.reply_text(
            "⚠️ فرمت کامل: `/feedback شماره‌مفهوم نام‌مشاور بله|خیر`\n"
            "مثال: `/feedback 2 سارا رضایی بله`",
            parse_mode=ParseMode.MARKDOWN,
        )

    num = args[0].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    concept = FEEDBACK_CONCEPTS.get(num)
    if not concept:
        return await update.message.reply_text(
            f"❌ شماره مفهوم نامعتبر است. بین ۱ تا ۱۱:\n{FEEDBACK_MENU.split('**شماره')[1].split('**نمونه')[0]}",
            parse_mode=ParseMode.MARKDOWN,
        )

    verdict_raw = args[-1].strip()
    if verdict_raw in ("بله", "بله.", "✅"):
        success, verdict_label = True, "موفق ✅"
    elif verdict_raw in ("خیر", "خیر.", "❌"):
        success, verdict_label = False, "ناموفق ❌"
    else:
        return await update.message.reply_text(
            '⚠️ کلمه آخر باید «بله» (موفق) یا «خیر» (ناموفق) باشد.',
            parse_mode=ParseMode.MARKDOWN,
        )

    counselor_name = " ".join(args[1:-1]).strip()

    # Match the name against active profiles
    from internal_ai_engine import names_match
    matched = next((p["clean_name"] for p in engine.profiles if names_match(p["clean_name"], counselor_name)), None)
    if not matched:
        return await update.message.reply_text(
            f"❌ مشاور «{counselor_name}» در لیست فعال یافت نشد. نام را چک کنید.",
            parse_mode=ParseMode.MARKDOWN,
        )

    ok = record_feedback_direct(matched, concept, success)
    if not ok:
        return await update.message.reply_text("❌ خطا در ثبت بازخورد.")

    # Show the new weight
    from internal_ai_engine import get_learning_weight
    w = get_learning_weight(matched, concept)
    concept_fa = {"زوج_ازدواج": "زوج/ازدواج", "نوجوان_جوان": "نوجوان", "والد_فرزند": "والد-فرزند",
                  "ارتباط_تعارض": "ارتباط/تعارض", "پزشکی_روانپزشکی": "روانپزشکی"}.get(concept, concept)
    await update.message.reply_text(
        f"🧠 **بازخورد ثبت شد**\n\n"
        f"👩‍⚕️ مشاور: {matched}\n"
        f"📋 موضوع: {concept_fa}\n"
        f"{'⬆️' if success else '⬇️'} نتیجه: {verdict_label}\n"
        f"⚖️ وزن فعلی این مشاور در این موضوع: **×{w:.2f}** "
        f"(خنثی=۱.۰۰، بازه مجاز ۰.۶۰ تا ۱.۶۰)",
        parse_mode=ParseMode.MARKDOWN,
    )


async def learning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the self-learning system status"""
    if not await _require_admin_session(update):
        return
    rows = get_learning_stats()
    if not rows:
        return await update.message.reply_text(
            "📊 هنوز داده یادگیری ثبت نشده.\nبا `/feedback ...` اولین بازخورد را ثبت کنید."
        )
    msg = "🧠 **وضعیت سیستم خودآموز (۲۰ رکورد آخر):**\n\n"
    for n, c, p, ng, w in rows:
        arrow = "⬆️" if w > 1.0 else ("⬇️" if w < 1.0 else "➖")
        msg += f"{arrow} {n} — {c}: ✅{p} ❌{ng} → ×{w}\n"
    await update.message.reply_text(msg)


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the consultant Excel upload flow"""
    if not await _require_admin_session(update):
        return
    context.user_data["awaiting_consultants_file"] = True
    await update.message.reply_text(
        "📤 **آپلود فایل مشاوران**\n\n"
        "لطفاً فایل اکسل مشاوران را ارسال کنید.\n\n"
        "پس از دریافت، فایل به‌صورت خودکار:\n"
        "۱. به JSON تبدیل می‌شود\n"
        "۲. در دیتابیس ذخیره و داده قبلی کامل جایگزین می‌شود\n"
        "۳. موتور هوشمند بلافاصله reload می‌شود",
        parse_mode=ParseMode.MARKDOWN,
    )


def _process_consultants_excel_sync(xlsx_bytes: bytes, uploaded_by: int) -> dict:
    """Synchronous processing: save file → convert to JSON → replace DB → return result"""
    import json as _json
    base = PROFILES_JSON.parent

    # 1. Permanently save the Excel file with a timestamp
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    saved_xlsx = base / f"consultants_{stamp}.xlsx"
    saved_xlsx.write_bytes(xlsx_bytes)

    # 2. Convert to JSON (the project's original file)
    count_json = convert_excel_to_json(str(saved_xlsx), str(PROFILES_JSON))

    # 3. Read the JSON and fully replace the consultants table in the database (with comparison)
    profiles = _json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    compare = replace_consultants(profiles, uploaded_by)

    return {
        "json_count": count_json,
        "db_count": compare["inserted"],
        "compare": compare,
        "saved_path": str(saved_xlsx),
        "file_size": len(xlsx_bytes),
    }


async def _handle_consultants_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download and fully process the consultants' Excel file"""
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith((".xlsx", ".xlsm")):
        await update.message.reply_text("⚠️ لطفاً فقط فایل اکسل (.xlsx) ارسال فرمایید.")
        return

    uid = update.effective_user.id
    status = await update.message.reply_text("⏳ در حال دانلود و پردازش فایل...")
    file_obj = await doc.get_file()
    fp = file_obj.file_path or ""
    dl_url = fp.replace("https://api.telegram.org", "https://tapi.bale.ai") if fp.startswith("http") else f"https://tapi.bale.ai/file/bot{BOT_TOKEN}/{fp}"

    import httpx
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        r = await c.get(dl_url)
        if r.status_code != 200 or len(r.content) < 100:
            await status.edit_text("❌ خطا در دانلود فایل. دوباره تلاش کنید.")
            return
        xlsx_bytes = r.content

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _executor, _process_consultants_excel_sync, xlsx_bytes, uid
        )

        # Hot-reload the engine — no bot restart needed
        old_active = len(engine.profiles)
        engine.reload()
        new_active = len(engine.profiles)

        # Data-integrity report from the database (after the replacement)
        stats = await loop.run_in_executor(_executor, get_consultants_stats)

        comp = result.get("compare", {})
        fname = os.path.basename(result["saved_path"])

        # ── Full report for the uploader ──
        rep = (
            "✅ **آپلود موفق — دیتای مشاوران کامل جایگزین شد**\n\n"
            f"📁 فایل: `{fname}` ({result['file_size'] // 1024} KB)\n\n"
            "**۱. پردازش فایل:**\n"
            f"• ردیف‌های استخراج‌شده از اکسل: {result['json_count']} مشاور\n"
            f"• رکوردهای درج‌شده در دیتابیس: {result['db_count']} رکورد\n\n"
            "**۲. مقایسه با دیتای قبلی:**\n"
            f"• تعداد قبلی: {comp.get('old_count', '-')} مشاور → تعداد جدید: {comp.get('new_count', '-')}\n"
        )
        added = comp.get("added", [])
        removed = comp.get("removed", [])
        if added:
            rep += f"• ➕ مشاوران جدید ({len(added)}): {'، '.join(added[:6])}{'...' if len(added) > 6 else ''}\n"
        else:
            rep += "• ➕ مشاور جدید: ندارد\n"
        if removed:
            rep += f"• ➖ حذف‌شده نسبت به قبل ({len(removed)}): {'، '.join(removed[:6])}{'...' if len(removed) > 6 else ''}\n"
        else:
            rep += "• ➖ مشاور حذف‌شده: ندارد\n"

        rep += (
            "\n**۳. صحت داده در دیتابیس (فیلدهای پرشده):**\n"
            f"• کل رکوردها: {stats['total']}\n"
            f"• ضریب توانمندی: {stats['with_ability']} | محل کار: {stats['with_location']}\n"
            f"• تحصیلات/سوابق: {stats['with_education']} | حوزه عمومی: {stats['with_general_area']}\n"
            f"• دارای تخصص جزئی: {stats['with_specializations']} مشاور (مجموع {stats['spec_total']} تخصص)\n"
            f"• محدوده سنی: {stats['with_age_range']} | پروانه: {stats['with_license']} | ملاحظات: {stats['with_notes']}\n"
        )
        if stats.get("by_location"):
            loc_str = "، ".join(f"{k}: {v}" for k, v in stats["by_location"].items())
            rep += f"• توزیع شعبه: {loc_str}\n"

        rep += (
            f"\n**۴. موتور هوشمند:**\n"
            f"• پروفایل فعال: {new_active} نفر (قبلاً {old_active})\n"
            f"• تغییرات بلافاصله اعمال شد — بدون نیاز به ری‌استارت ✅"
        )
        await status.edit_text(rep, parse_mode=ParseMode.MARKDOWN)

        # ── Brief summary for the other admins and the developer ──
        brief = (
            "📢 **گزارش آپلود دیتای مشاوران**\n"
            f"توسط کاربر `{uid}`\n"
            f"فایل: `{fname}`\n"
            f"👥 {comp.get('old_count', '?')} ← {comp.get('new_count', '?')} مشاور | "
            f"جدید: {len(added)} | حذف: {len(removed)}\n"
            f"🔄 موتور هوشمند به‌روزرسانی شد ({new_active} پروفایل فعال)"
        )
        for admin_id in ADMIN_USER_IDS:
            if admin_id != uid:
                try:
                    await context.bot.send_message(admin_id, brief, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass

    except Exception as e:
        log.error("Consultants upload error: %s", e)
        await status.edit_text(f"❌ خطا در پردازش اکسل:\n{e}")
    finally:
        context.user_data.pop("awaiting_consultants_file", None)


async def doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """File received: if the admin is waiting for an Excel upload → process consultants; otherwise guide them"""
    uid = update.effective_user.id
    if not _is_admin(uid):
        return
    if not _admin_authenticated(uid):
        await update.message.reply_text(
            "🔐 برای ارسال فایل مشاوران، ابتدا وارد پنل شوید:\n`/admin YOUR_PASSWORD`\n"
            "سپس دستور `/upload` را اجرا کنید.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not context.user_data.get("awaiting_consultants_file"):
        await update.message.reply_text(
            "ℹ️ برای ارسال فایل اکسل مشاوران ابتدا دستور `/upload` را اجرا کنید.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _handle_consultants_upload(update, context)


# ── Admin panel: broadcast (standalone ConversationHandler) ────────

(BRWAIT_CONTENT, BRCONFIRM) = range(2)   # broadcast flow states


def _admin_check(update: Update) -> bool:
    """Allow entering the broadcast flow only for admins in a private chat"""
    u = update.effective_user
    return bool(u and _is_admin(u.id) and update.effective_chat.type == "private")


async def _session_gate(update: Update, context) -> bool:
    """Broadcast entry requires an active admin session"""
    uid = update.effective_user.id
    if not _admin_authenticated(uid):
        await update.message.reply_text(
            "🔐 ابتدا وارد پنل شوید:\n`/admin YOUR_PASSWORD`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False
    return True


async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Enter the broadcast flow:
    - If replying to a message, that message is the candidate → go straight to the confirmation step
    - Otherwise, wait for the content in the next message
    """
    if not _admin_check(update):
        return ConversationHandler.END
    if not await _session_gate(update, context):
        return ConversationHandler.END

    replied = update.message.reply_to_message
    if replied:
        context.user_data["br_chat_id"] = update.effective_chat.id
        context.user_data["br_msg_id"] = replied.message_id
        return await _ask_broadcast_confirm(update, context)

    context.user_data["br_chat_id"] = None
    await update.message.reply_text(
        "📣 **ارسال پیام همگانی**\n\n"
        "لطفاً پیام موردنظر خود را ارسال کنید.\n"
        "هر نوع محتوایی قابل ارسال است: متن، عکس، ویدیو، ویس، فایل و...\n\n"
        "❌ برای انصراف: /cancel_broadcast",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BRWAIT_CONTENT


async def broadcast_receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive broadcast content (text or media)"""
    if not _admin_check(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text in ("❌ انصراف", "/cancel_broadcast"):
        context.user_data.pop("br_chat_id", None)
        context.user_data.pop("br_msg_id", None)
        await update.message.reply_text("عملیات برادکست لغو شد.", reply_markup=RESTART_KB)
        return ConversationHandler.END

    context.user_data["br_chat_id"] = update.effective_chat.id
    context.user_data["br_msg_id"] = update.message.message_id
    return await _ask_broadcast_confirm(update, context)


async def _ask_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    targets = get_broadcast_targets()
    confirm_kb = ReplyKeyboardMarkup(
        [["✅ تایید ارسال", "❌ انصراف"]], resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        f"👥 تعداد گیرندگان: **{len(targets)}** نفر\n\n"
        f"آیا از ارسال همگانی این پیام مطمئن هستید؟",
        reply_markup=confirm_kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return BRCONFIRM


async def broadcast_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final confirmation or cancellation"""
    if not _admin_check(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    if text in ("❌ انصراف", "/cancel_broadcast"):
        context.user_data.pop("br_chat_id", None)
        context.user_data.pop("br_msg_id", None)
        await update.message.reply_text("عملیات برادکست لغو شد.", reply_markup=RESTART_KB)
        return ConversationHandler.END

    if text != "✅ تایید ارسال":
        await update.message.reply_text(
            'لطفاً با دکمه «✅ تایید ارسال» تأیید کنید یا «❌ انصراف» را بزنید.'
        )
        return BRCONFIRM

    chat_id = context.user_data.pop("br_chat_id", None)
    msg_id = context.user_data.pop("br_msg_id", None)

    if not chat_id or not msg_id:
        await update.message.reply_text("پیامی برای ارسال یافت نشد. ابتدا /broadcast را اجرا کنید.", reply_markup=RESTART_KB)
        return ConversationHandler.END

    targets = get_broadcast_targets()
    if not targets:
        await update.message.reply_text("هیچ گیرنده‌ای یافت نشد.", reply_markup=RESTART_KB)
        return ConversationHandler.END

    progress_msg = await update.message.reply_text(f"📤 شروع ارسال همگانی به {len(targets)} کاربر...")

    result = await send_broadcast(context.bot, targets, chat_id, msg_id, progress_msg=progress_msg)

    final_text = (
        f"🏁 **برادکست تمام شد:**\n\n"
        f"✅ موفق: {result['sent']} نفر\n"
        f"❌ ناموفق: {result['failed']} نفر\n"
        f"👥 کل گیرندگان: {result['total']} نفر\n\n"
        f"(ناموفق‌ها معمولاً کاربرانی هستند که بات را بلاک کرده‌اند)"
    )
    try:
        await progress_msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
    except Exception:
        await context.bot.send_message(update.effective_user.id, final_text, parse_mode=ParseMode.MARKDOWN)

    return ConversationHandler.END


async def cancel_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the broadcast operation from any step"""
    context.user_data.pop("br_chat_id", None)
    context.user_data.pop("br_msg_id", None)
    if _is_admin(update.effective_user.id):
        await update.message.reply_text("عملیات برادکست لغو شد.", reply_markup=RESTART_KB)


def build_broadcast_conversation() -> ConversationHandler:
    """Build the standalone broadcast conversation — must be registered before the main conversation"""
    admin_only = filters.User(user_id=list(ADMIN_USER_IDS)) & filters.ChatType.PRIVATE

    return ConversationHandler(
        entry_points=[
            CommandHandler("broadcast", broadcast_entry, filters=filters.ChatType.PRIVATE),
            MessageHandler(admin_only & filters.REPLY & ~filters.COMMAND, broadcast_entry_reply),
        ],
        states={
            BRWAIT_CONTENT: [
                MessageHandler(admin_only & ~filters.COMMAND, broadcast_receive_content),
                CommandHandler("cancel_broadcast", cancel_broadcast_cmd),
            ],
            BRCONFIRM: [
                MessageHandler(admin_only & ~filters.COMMAND, broadcast_confirm_handler),
                CommandHandler("cancel_broadcast", cancel_broadcast_cmd),
            ],
        },
        fallbacks=[
            CommandHandler("cancel_broadcast", cancel_broadcast_cmd),
            CommandHandler("start", lambda u, c: ConversationHandler.END),
        ],
        allow_reentry=True,
        name="broadcast_conv",
        persistent=False,
    )


async def broadcast_entry_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """An admin's reply to any message = broadcast candidate (outside the intake form)"""
    if not _admin_check(update):
        return ConversationHandler.END
    # If the admin is mid-intake-form, their reply belongs to the form, not the broadcast
    if context.user_data.get("_in_main_conv"):
        return ConversationHandler.END
    if not await _session_gate(update, context):
        return ConversationHandler.END
    context.user_data["br_chat_id"] = update.effective_chat.id
    context.user_data["br_msg_id"] = update.message.reply_to_message.message_id
    return await _ask_broadcast_confirm(update, context)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Excel file of user data"""
    if not await _require_admin_session(update):
        return
    await update.message.reply_text("📊 در حال تهیه فایل اکسل دیتای کاربران...")
    try:
        loop = asyncio.get_running_loop()
        excel_bytes = await loop.run_in_executor(_executor, export_users_excel)
        buf = io.BytesIO(excel_bytes)
        buf.name = "counseling_users.xlsx"
        from datetime import datetime as _dt
        caption = (
            f"📋 **خروجی دیتای کاربران مرکز خانواده نیک‌روان**\n"
            f"🗓 تاریخ تولید: {_dt.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"شیت ۱: کاربران | شیت ۲: سوابق درخواست‌ها"
        )
        await update.message.reply_document(document=buf, caption=caption, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error("Export error: %s", e)
        await update.message.reply_text(f"❌ خطا در تولید خروجی:\n{e}")


# (The old broadcast flow was removed — the new version is defined above as a standalone ConversationHandler)


# ── Scheduler & Startup Hook ────────────────────────────

async def _do_background_crawl():
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, _safe_background_crawl)
    except Exception as e:
        log.error("Crawl execution error: %s", e)


async def _scheduled_crawl():
    log.info("⏰ Starting scheduled 2-hour crawl...")
    await _do_background_crawl()


async def post_init(application: Application):
    log.info("🚀 Triggering initial background crawl on startup...")
    asyncio.create_task(_do_background_crawl())


async def _global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Global error handler: transient network errors (momentary proxy/internet drops)
    are logged briefly — PTB retries on its own, so a full traceback isn't needed.
    """
    err = context.error
    if isinstance(err, NetworkError) and ("disconnected" in str(err).lower() or "timed out" in str(err).lower()):
        log.warning("🌐 قطعی موقت شبکه در ارتباط با سرور بله (به‌صورت خودکار تلاش مجدد می‌شود).")
        return
    log.error("خطای پردازش آپدیت: %s", err, exc_info=err)
    if isinstance(update, Update) and update.effective_user:
        try:
            await context.bot.send_message(
                update.effective_user.id,
                "⚠️ یک خطای غیرمنتظره رخ داد. لطفاً عملیات را دوباره تلاش کنید.",
            )
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).base_url("https://tapi.bale.ai/bot").post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(r"^(شروع مجدد|/start|شروع)$"), start),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, gender_handler)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_handler)],
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, topic_handler)],
            PREV_THERAPY: [MessageHandler(filters.TEXT & ~filters.COMMAND, prev_therapy_handler)],
            PREV_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, prev_detail_handler)],
            EXPECTATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, expectation_handler)],
            PREFERRED_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, preferred_gender_handler)],
            BRANCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, branch_handler)],
            ASK_GHQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_ghq_handler)],
            GHQ_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ghq_question_handler)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(r"^(شروع مجدد|/start|شروع)$"), start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start),
        ],
    )

    # The broadcast conversation must be registered before the main conversation so that
    # the global intake-flow fallback doesn't steal admin messages
    app.add_handler(build_broadcast_conversation())

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Regex(r"^(شروع مجدد|/start|شروع)$"), start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("crawl", crawl_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("myid", lambda u, c: u.message.reply_text(f"شناسه شما: `{u.effective_user.id}`", parse_mode=ParseMode.MARKDOWN)))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("learning", learning_cmd))

    sched = AsyncIOScheduler()
    sched.add_job(_scheduled_crawl, "interval", hours=CRAWLER_INTERVAL_HOURS)
    sched.start()

    app.add_error_handler(_global_error_handler)

    log.info("🤖 AI counseling bot started with 2h crawler interval.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()