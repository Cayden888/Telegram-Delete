"""
Telegram Ban-Word Bot
- Auto-deletes group messages that contain banned words / spam commands (e.g. /masuk, /join)
- Ban list per group stored in Supabase (table: banned_words)
- Deletions logged in Supabase (table: delete_log) for /status
- Admin commands: /addban, /delban, /listban, /status
"""
import asyncio
import logging
import os
import re
import time

from supabase import create_client
from telegram import MessageEntity, Update
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("banbot")


# ---------- config ----------
def _env(name: str) -> str:
    val = os.getenv(name, "").strip().strip('"').strip("'")
    if not val:
        raise SystemExit(f"Missing environment variable {name} (set it in Railway → Variables)")
    return val


BOT_TOKEN = _env("BOT_TOKEN")
SUPABASE_URL = _env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _env("SUPABASE_KEY")  # service_role / secret key (server only!)
DEFAULT_BANS = {w.strip().lower() for w in os.getenv("DEFAULT_BANS", "/masuk,/join").split(",") if w.strip()}
EXEMPT_ADMINS = os.getenv("EXEMPT_ADMINS", "true").strip().lower() in ("1", "true", "yes")
try:
    CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))
except ValueError:
    CACHE_TTL = 60

TABLE = "banned_words"
LOG_TABLE = "delete_log"
OWN_COMMANDS = ("addban", "delban", "listban", "status", "start", "help")
START_TIME = time.time()

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

_ban_cache: dict[int, tuple[set[str], float]] = {}
_admin_cache: dict[int, tuple[set[int], float]] = {}
_pat_cache: dict[str, re.Pattern] = {}


# ---------- supabase (sync calls, always run via asyncio.to_thread) ----------
def _db_fetch(chat_id: int) -> set[str]:
    res = sb.table(TABLE).select("word").eq("chat_id", chat_id).execute()
    return {r["word"] for r in (res.data or [])}


def _db_add(chat_id: int, words: list[str], user_id: int | None) -> None:
    rows = [{"chat_id": chat_id, "word": w, "added_by": user_id} for w in words]
    sb.table(TABLE).upsert(rows, on_conflict="chat_id,word").execute()


def _db_del(chat_id: int, words: list[str]) -> None:
    sb.table(TABLE).delete().eq("chat_id", chat_id).in_("word", words).execute()


def _db_log(chat_id: int, user_id: int | None, word: str) -> None:
    sb.table(LOG_TABLE).insert({"chat_id": chat_id, "user_id": user_id, "word": word}).execute()


def _db_stats(chat_id: int) -> tuple[int, int]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    total = sb.table(LOG_TABLE).select("id", count="exact").eq("chat_id", chat_id).limit(1).execute()
    day = (
        sb.table(LOG_TABLE).select("id", count="exact")
        .eq("chat_id", chat_id).gte("created_at", since).limit(1).execute()
    )
    return total.count or 0, day.count or 0


def _short_err(e: Exception) -> str:
    return str(e)[:200]


async def get_bans(chat_id: int, fresh: bool = False) -> set[str]:
    hit = _ban_cache.get(chat_id)
    if hit and not fresh and time.time() - hit[1] < CACHE_TTL:
        return hit[0]
    try:
        words = await asyncio.to_thread(_db_fetch, chat_id)
    except Exception as e:  # DB down → keep last known list, retry after TTL
        log.error("Supabase fetch failed: %s", _short_err(e))
        words = (hit[0] - DEFAULT_BANS) if hit else set()
    words = words | DEFAULT_BANS
    _ban_cache[chat_id] = (words, time.time())
    return words


# ---------- matching ----------
def _pattern(word: str) -> re.Pattern:
    w = re.escape(word)
    if word.startswith("/"):
        # the command as its own token anywhere in the text, also /join@SomeBot
        return re.compile(rf"(?<!\S){w}(?:@\w+)?(?![\w/])", re.I)
    if re.fullmatch(r"\w+", word):
        return re.compile(rf"(?<!\w){w}(?!\w)", re.I)  # whole word
    return re.compile(w, re.I)  # links / phrases: substring


def find_banned(text: str, bans: set[str]) -> str | None:
    for word in sorted(bans):  # sorted = deterministic
        p = _pat_cache.get(word)
        if p is None:
            p = _pat_cache[word] = _pattern(word)
        if p.search(text):
            return word
    return None


def message_text(msg) -> str:
    """Text + caption + hidden hyperlink URLs (spammers hide t.me links behind words)."""
    parts = [msg.text or "", msg.caption or ""]
    for ent in list(msg.entities or []) + list(msg.caption_entities or []):
        if ent.type == MessageEntity.TEXT_LINK and ent.url:
            parts.append(ent.url)
    return "\n".join(p for p in parts if p)


def own_command(text: str) -> bool:
    m = re.match(r"/(\w+)(?:@\w+)?", text)
    return bool(m and m.group(1).lower() in OWN_COMMANDS)


# ---------- admin helpers ----------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return False
    # anonymous admin (posting as the group itself)
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
        except TelegramError as e:
            log.warning("Could not get admins for %s: %s", chat.id, e)
            ids = hit[0] if hit else set()
        _admin_cache[chat.id] = (ids, time.time())
        hit = _admin_cache[chat.id]
    return user.id in hit[0]


async def group_admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the command may run. Replies in private chats."""
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text("Use this command inside your group.")
        return False
    return await is_admin(update, context)


def _parse_words(args: list[str]) -> list[str]:
    parts = re.split(r"[,\s]+", " ".join(args or []))
    return sorted({p.strip().lower() for p in parts if p.strip()})[:100]


def _fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


# ---------- command handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Ban-word bot 🧹\n"
        "Add me to a group as admin with 'Delete messages' permission.\n\n"
        "Admin commands (in the group):\n"
        "/addban /masuk /join promo – add words\n"
        "/delban promo – remove words\n"
        "/listban – show the list\n"
        "/status – bot health & stats"
    )


async def cmd_addban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await group_admin_only(update, context):
        return
    words = _parse_words(context.args)
    if not words:
        return await update.effective_message.reply_text("Usage: /addban /masuk /join spamword")
    uid = update.effective_user.id if update.effective_user else None
    try:
        await asyncio.to_thread(_db_add, update.effective_chat.id, words, uid)
    except Exception as e:
        log.error("addban failed: %s", _short_err(e))
        return await update.effective_message.reply_text("❌ Could not save to database. Check Railway logs.")
    _ban_cache.pop(update.effective_chat.id, None)
    await update.effective_message.reply_text("✅ Banned: " + ", ".join(words))


async def cmd_delban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await group_admin_only(update, context):
        return
    words = _parse_words(context.args)
    if not words:
        return await update.effective_message.reply_text("Usage: /delban spamword")
    try:
        await asyncio.to_thread(_db_del, update.effective_chat.id, words)
    except Exception as e:
        log.error("delban failed: %s", _short_err(e))
        return await update.effective_message.reply_text("❌ Could not update database. Check Railway logs.")
    _ban_cache.pop(update.effective_chat.id, None)
    note = ""
    if any(w in DEFAULT_BANS for w in words):
        note = "\n(Some of these are in DEFAULT_BANS on Railway – remove them there.)"
    await update.effective_message.reply_text("🗑 Removed: " + ", ".join(words) + note)


async def cmd_listban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await group_admin_only(update, context):
        return
    bans = sorted(await get_bans(update.effective_chat.id, fresh=True))
    body = "\n".join(bans) or "(none)"
    if len(body) > 3900:  # Telegram limit is 4096 chars
        body = body[:3900] + "\n…"
    await update.effective_message.reply_text(f"🚫 Banned words ({len(bans)}):\n{body}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    uptime = _fmt_uptime(time.time() - START_TIME)
    if chat.type == "private":
        return await update.effective_message.reply_text(
            f"🟢 Bot online · ⏱ {uptime}\nUse /status inside a group for group stats."
        )
    if not await is_admin(update, context):
        return

    try:
        total, day = await asyncio.to_thread(_db_stats, chat.id)
        db_line = "✅ connected"
        deleted = f"{day} in 24h · {total} total"
    except Exception as e:
        log.error("Status DB check failed: %s", _short_err(e))
        db_line = "❌ error (see Railway logs)"
        deleted = "n/a"

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        can_delete = me.status == "creator" or (
            me.status == "administrator" and bool(getattr(me, "can_delete_messages", False))
        )
    except TelegramError:
        can_delete = False
    perm_line = "✅ can delete" if can_delete else "❌ make me admin with 'Delete messages'"

    bans = await get_bans(chat.id, fresh=True)
    await update.effective_message.reply_text(
        "📊 Bot status\n"
        f"🟢 Online · ⏱ {uptime}\n"
        f"🗄 Database: {db_line}\n"
        f"🛡 Permission: {perm_line}\n"
        f"🚫 Banned words: {len(bans)}\n"
        f"🧹 Deleted: {deleted}\n"
        f"👮 Admins exempt: {'yes' if EXEMPT_ADMINS else 'no'}"
    )


# ---------- the filter ----------
async def filter_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    if msg.from_user and msg.from_user.id == context.bot.id:
        return
    text = message_text(msg)
    if not text:
        return

    admin = await is_admin(update, context)
    if admin and (EXEMPT_ADMINS or own_command(text)):
        return

    hit = find_banned(text, await get_bans(msg.chat_id))
    if not hit:
        return

    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("Could not delete in %s (is the bot admin with delete rights?): %s", msg.chat_id, e)
        return

    who = msg.from_user.id if msg.from_user else None
    log.info("Deleted msg in %s from %s (matched %r)", msg.chat_id, who, hit)
    try:
        await asyncio.to_thread(_db_log, msg.chat_id, who, hit)
    except Exception as e:
        log.warning("Could not log deletion: %s", _short_err(e))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.warning("Another bot instance is using this token (normal for a few seconds during a redeploy).")
        return
    log.error("Unhandled error: %s", err, exc_info=err)


def build_app() -> Application:
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
    return app


def main():
    app = build_app()
    log.info("Bot starting. Default bans: %s | admins exempt: %s", ", ".join(sorted(DEFAULT_BANS)), EXEMPT_ADMINS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
