"""
Telegram Ban-Word Bot
- Auto-deletes group messages that contain banned words / spam commands (e.g. /masuk, /join)
- Ban list per group stored in Supabase
- Admin commands: /addban, /delban, /listban, /status
"""
import asyncio
import logging
import os
import re
import time

from supabase import create_client
from telegram import Update
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("banbot")

# ---------- config ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service_role / secret key (server only!)
DEFAULT_BANS = {w.strip().lower() for w in os.getenv("DEFAULT_BANS", "/masuk,/join").split(",") if w.strip()}
EXEMPT_ADMINS = os.getenv("EXEMPT_ADMINS", "true").lower() == "true"
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE = "banned_words"
LOG_TABLE = "delete_log"
OWN_COMMANDS = ("/addban", "/delban", "/listban", "/status", "/start", "/help")
START_TIME = time.time()

_ban_cache: dict[int, tuple[set[str], float]] = {}
_admin_cache: dict[int, tuple[set[int], float]] = {}


# ---------- supabase helpers ----------
def _db_fetch(chat_id: int) -> set[str]:
    res = sb.table(TABLE).select("word").eq("chat_id", chat_id).execute()
    return {r["word"] for r in (res.data or [])}


def _db_add(chat_id: int, words: list[str], user_id: int) -> None:
    rows = [{"chat_id": chat_id, "word": w, "added_by": user_id} for w in words]
    sb.table(TABLE).upsert(rows, on_conflict="chat_id,word").execute()


def _db_del(chat_id: int, words: list[str]) -> None:
    sb.table(TABLE).delete().eq("chat_id", chat_id).in_("word", words).execute()


def _db_log(chat_id: int, user_id, word: str) -> None:
    sb.table(LOG_TABLE).insert({"chat_id": chat_id, "user_id": user_id, "word": word}).execute()


def _db_stats(chat_id: int) -> tuple[int, int]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    total = sb.table(LOG_TABLE).select("id", count="exact").eq("chat_id", chat_id).limit(1).execute().count or 0
    day = (
        sb.table(LOG_TABLE).select("id", count="exact")
        .eq("chat_id", chat_id).gte("created_at", since).limit(1).execute().count or 0
    )
    return total, day


def _fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


async def get_bans(chat_id: int) -> set[str]:
    hit = _ban_cache.get(chat_id)
    if hit and time.time() - hit[1] < CACHE_TTL:
        return hit[0]
    try:
        words = await asyncio.to_thread(_db_fetch, chat_id)
    except Exception as e:  # keep working on DB hiccups
        log.error("Supabase fetch failed: %s", e)
        words = hit[0] if hit else set()
    words = words | DEFAULT_BANS
    _ban_cache[chat_id] = (words, time.time())
    return words


# ---------- matching ----------
def _pattern(word: str) -> re.Pattern:
    w = re.escape(word)
    if word.startswith("/"):
        # command anywhere as its own token, also /join@SomeBot
        return re.compile(rf"(?<!\S){w}(?:@\w+)?(?=\s|$|[.,!?])", re.I)
    if re.fullmatch(r"\w+", word):
        return re.compile(rf"(?<!\w){w}(?!\w)", re.I)  # whole word
    return re.compile(w, re.I)  # links / phrases: substring


_pat_cache: dict[str, re.Pattern] = {}


def find_banned(text: str, bans: set[str]) -> str | None:
    for word in bans:
        p = _pat_cache.get(word) or _pat_cache.setdefault(word, _pattern(word))
        if p.search(text):
            return word
    return None


# ---------- admin helpers ----------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    msg = update.effective_message
    # anonymous admin posting as the group, or channel linked posts
    if msg.sender_chat and msg.sender_chat.id == chat.id:
        return True
    user = update.effective_user
    if not user:
        return False
    hit = _admin_cache.get(chat.id)
    if not hit or time.time() - hit[1] > 300:
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            ids = {a.user.id for a in admins}
        except TelegramError:
            ids = set()
        _admin_cache[chat.id] = (ids, time.time())
        hit = _admin_cache[chat.id]
    return user.id in hit[0]


def _parse_words(args: list[str]) -> list[str]:
    raw = " ".join(args)
    parts = re.split(r"[,\s]+", raw)
    return sorted({p.strip().lower() for p in parts if p.strip()})


# ---------- handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Ban-word bot 🧹\n"
        "Add me to a group as admin with 'Delete messages' permission.\n\n"
        "Admin commands (in the group):\n"
        "/addban /masuk /join promo  – add words\n"
        "/delban promo  – remove words\n"
        "/listban  – show the list\n"
        "/status  – bot health & stats"
    )


async def cmd_addban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.effective_message.reply_text("Use this inside your group.")
    if not await is_admin(update, context):
        return
    words = _parse_words(context.args)
    if not words:
        return await update.effective_message.reply_text("Usage: /addban /masuk /join spamword")
    uid = update.effective_user.id if update.effective_user else 0
    await asyncio.to_thread(_db_add, update.effective_chat.id, words, uid)
    _ban_cache.pop(update.effective_chat.id, None)
    await update.effective_message.reply_text("✅ Banned: " + ", ".join(words))


async def cmd_delban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.effective_message.reply_text("Use this inside your group.")
    if not await is_admin(update, context):
        return
    words = _parse_words(context.args)
    if not words:
        return await update.effective_message.reply_text("Usage: /delban spamword")
    await asyncio.to_thread(_db_del, update.effective_chat.id, words)
    _ban_cache.pop(update.effective_chat.id, None)
    note = ""
    if any(w in DEFAULT_BANS for w in words):
        note = "\n(Some are in DEFAULT_BANS on Railway – remove them there.)"
    await update.effective_message.reply_text("🗑 Removed: " + ", ".join(words) + note)


async def cmd_listban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.effective_message.reply_text("Use this inside your group.")
    if not await is_admin(update, context):
        return
    _ban_cache.pop(update.effective_chat.id, None)
    bans = sorted(await get_bans(update.effective_chat.id))
    await update.effective_message.reply_text("🚫 Banned words:\n" + ("\n".join(bans) or "(none)"))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    uptime = _fmt_uptime(time.time() - START_TIME)
    if chat.type == "private":
        return await update.effective_message.reply_text(
            f"🟢 Bot online\n⏱ Uptime: {uptime}\nUse /status inside a group for group stats."
        )
    if not await is_admin(update, context):
        return

    # database + stats
    try:
        total, day = await asyncio.to_thread(_db_stats, chat.id)
        db_line = "✅ connected"
    except Exception as e:
        log.error("Status DB check failed: %s", e)
        total = day = None
        db_line = "❌ error (check SUPABASE_URL / KEY / schema.sql)"

    # bot permission in this group
    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        can_delete = me.status == "creator" or (me.status == "administrator" and getattr(me, "can_delete_messages", False))
    except TelegramError:
        can_delete = False
    perm_line = "✅ can delete" if can_delete else "❌ make me admin with 'Delete messages'"

    _ban_cache.pop(chat.id, None)
    bans = await get_bans(chat.id)
    deleted = f"{day} in 24h · {total} total" if total is not None else "n/a"

    await update.effective_message.reply_text(
        "📊 Bot status\n"
        f"🟢 Online · ⏱ {uptime}\n"
        f"🗄 Database: {db_line}\n"
        f"🛡 Permission: {perm_line}\n"
        f"🚫 Banned words: {len(bans)}\n"
        f"🧹 Deleted: {deleted}\n"
        f"👮 Admins exempt: {'yes' if EXEMPT_ADMINS else 'no'}"
    )


async def filter_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    text = msg.text or msg.caption or ""
    if not text:
        return
    if text.lower().startswith(OWN_COMMANDS):
        return
    if EXEMPT_ADMINS and await is_admin(update, context):
        return
    hit = find_banned(text, await get_bans(msg.chat_id))
    if not hit:
        return
    try:
        await msg.delete()
        who = update.effective_user.id if update.effective_user else None
        log.info("Deleted msg in %s from %s (matched %r)", msg.chat_id, who, hit)
        try:
            await asyncio.to_thread(_db_log, msg.chat_id, who, hit)
        except Exception as e:
            log.warning("Could not log deletion: %s", e)
    except TelegramError as e:
        log.warning("Could not delete (is the bot admin with delete rights?): %s", e)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.warning("Another bot instance is polling (normal for a few seconds during redeploy).")
        return
    log.error("Unhandled error: %s", err, exc_info=err)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start), group=0)
    app.add_handler(CommandHandler("addban", cmd_addban), group=0)
    app.add_handler(CommandHandler("delban", cmd_delban), group=0)
    app.add_handler(CommandHandler("listban", cmd_listban), group=0)
    app.add_handler(CommandHandler("status", cmd_status), group=0)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION), filter_messages),
        group=1,
    )
    app.add_error_handler(on_error)
    log.info("Bot running. Default bans: %s", ", ".join(sorted(DEFAULT_BANS)))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
