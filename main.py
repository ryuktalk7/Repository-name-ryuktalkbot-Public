import os
import re
import itertools
from datetime import datetime, timedelta
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ── Chat state ────────────────────────────────────────────────────────────────
waiting_users     = []
active_chats      = {}
user_genders      = {}
chat_start_times  = {}

# ── Moderation ────────────────────────────────────────────────────────────────
report_counts  = {}
ban_until      = {}
reported_by    = {}

# ── Reputation ────────────────────────────────────────────────────────────────
reputation_scores = {}

# ── Media ─────────────────────────────────────────────────────────────────────
awaiting_mode    = {}
pending_media    = {}
pending_view_once = {}
_media_counter   = itertools.count(1)

# ── Message log (for cleanup) ─────────────────────────────────────────────────
message_log = []

# ── Wait-time tracking (for matching priority) ────────────────────────────────
waiting_since         = {}  # user_id -> datetime           when they joined the queue

# ── Partner gender filter (set by /chatX commands) ────────────────────────────
partner_gender_filter = {}  # user_id -> gender str         one-time partner preference

# ── Anti-spam / fake-user detection ──────────────────────────────────────────
message_timestamps  = {}   # user_id -> [datetime, ...]   flood detection
session_link_counts = {}   # user_id -> int               links in current chat
short_chat_log      = {}   # user_id -> [datetime, ...]   timestamps of short chat ends
spam_scores         = {}   # user_id -> int               accumulated score
match_cooldown_until = {}  # user_id -> datetime          /find blocked until
match_limited       = set()  # only matched with other limited users
suspected_fake      = set()  # deprioritised in matching

URL_PATTERN = re.compile(r'(https?://|www\.|t\.me/)\S+', re.IGNORECASE)

# ── Genders / compatibility ───────────────────────────────────────────────────
GENDERS = ["Male", "Female", "Gay", "Lesbian", "Bisexual"]

COMPATIBLE = {
    "Male":     {"Female", "Bisexual"},
    "Female":   {"Male", "Bisexual"},
    "Gay":      {"Gay", "Bisexual"},
    "Lesbian":  {"Lesbian", "Bisexual"},
    "Bisexual": {"Male", "Female", "Gay", "Lesbian", "Bisexual"},
}

# ── Tunable constants ─────────────────────────────────────────────────────────
BAN_THRESHOLD           = 25
BAN_DURATION            = timedelta(hours=24)
RATING_DELAY            = timedelta(minutes=5)
RATING_INTERVAL         = timedelta(minutes=1)
VIEW_ONCE_TTL           = timedelta(seconds=10)
MESSAGE_MAX_AGE         = timedelta(days=3)

FLOOD_MSG_COUNT         = 5               # messages …
FLOOD_WINDOW            = timedelta(seconds=10)  # … per this window = flood

LINK_SPAM_THRESHOLD     = 3               # links per chat session

SHORT_CHAT_DURATION     = timedelta(seconds=60)  # chat shorter than this counts
SHORT_CHAT_COUNT        = 5               # this many short chats …
SHORT_CHAT_WINDOW       = timedelta(minutes=30)  # … in this window = signal

HIGH_REPORT_THRESHOLD   = 5              # reports (before ban) → spam signal
SPAM_COOLDOWN_THRESHOLD = 5              # spam score → match cooldown
SPAM_LIMITED_THRESHOLD  = 10             # spam score → match-limited pool
MATCH_COOLDOWN_DURATION = timedelta(minutes=5)

FAKE_REPORT_THRESHOLD      = 3           # reports …
FAKE_SHORT_CHAT_THRESHOLD  = 3           # … + short chats → suspected fake

QUESTIONS = [
    "Is the user friendly?",
    "Do you trust this user?",
    "Is the user funny?",
    "Is the user respectful?",
    "Would you chat again with this user?",
]


# ── Helper: gender compatibility ──────────────────────────────────────────────
def are_compatible(g1, g2):
    return g2 in COMPATIBLE.get(g1, set()) and g1 in COMPATIBLE.get(g2, set())


# ── Helper: ban checks ────────────────────────────────────────────────────────
def is_banned(user_id):
    if user_id not in ban_until:
        return False
    if datetime.now() < ban_until[user_id]:
        return True
    del ban_until[user_id]
    return False


def ban_user(user_id):
    ban_until[user_id] = datetime.now() + BAN_DURATION
    report_counts[user_id] = 0


# ── Helper: spam detection ────────────────────────────────────────────────────
def add_spam_signal(user_id: int, amount: int = 1) -> None:
    """Increment spam score and apply penalties."""
    spam_scores[user_id] = spam_scores.get(user_id, 0) + amount
    score = spam_scores[user_id]
    if score >= SPAM_COOLDOWN_THRESHOLD:
        match_cooldown_until[user_id] = datetime.now() + MATCH_COOLDOWN_DURATION
    if score >= SPAM_LIMITED_THRESHOLD:
        match_limited.add(user_id)
    if reputation_scores.get(user_id, 0) > 0:
        reputation_scores[user_id] -= 1


def check_flood(user_id: int) -> bool:
    """Return True if user is flooding messages."""
    now = datetime.now()
    cutoff = now - FLOOD_WINDOW
    times = [t for t in message_timestamps.get(user_id, []) if t > cutoff]
    times.append(now)
    message_timestamps[user_id] = times
    return len(times) >= FLOOD_MSG_COUNT


def check_links(user_id: int, text: str) -> bool:
    """Return True if user has hit the link-spam threshold this session."""
    if URL_PATTERN.search(text):
        session_link_counts[user_id] = session_link_counts.get(user_id, 0) + 1
        return session_link_counts[user_id] >= LINK_SPAM_THRESHOLD
    return False


def record_short_chat(user_id: int, duration: timedelta) -> bool:
    """Track chat duration; return True if the user has many short chats."""
    if duration >= SHORT_CHAT_DURATION:
        return False
    now = datetime.now()
    cutoff = now - SHORT_CHAT_WINDOW
    times = [t for t in short_chat_log.get(user_id, []) if t > cutoff]
    times.append(now)
    short_chat_log[user_id] = times
    return len(times) >= SHORT_CHAT_COUNT


def check_fake_gender(user_id: int) -> None:
    """Flag user as suspected fake based on reports + short-chat behaviour."""
    if (report_counts.get(user_id, 0) >= FAKE_REPORT_THRESHOLD and
            len(short_chat_log.get(user_id, [])) >= FAKE_SHORT_CHAT_THRESHOLD):
        suspected_fake.add(user_id)


def close_chat(user_id: int, partner_id: int) -> None:
    """Shared teardown called by skip/stop; runs spam/fake checks."""
    start = chat_start_times.pop(user_id, None)
    chat_start_times.pop(partner_id, None)
    session_link_counts.pop(user_id, None)
    session_link_counts.pop(partner_id, None)

    if start:
        duration = datetime.now() - start
        for uid in (user_id, partner_id):
            if record_short_chat(uid, duration):
                add_spam_signal(uid, 2)
            check_fake_gender(uid)


# ── Helper: partner selection ─────────────────────────────────────────────────
def find_best_partner(user_id: int, my_gender: str):
    """
    Priority order (lower score = better match):

    1. Compatible gender preferences     — 0 pts if compatible, 1000 pts if not
    2. Similar reputation scores         — 5 pts per point of difference
    3. Longest waiting time              — up to 30 pts advantage for long waiters
    4. If no ideal match exists, the above still picks the least-bad candidate
       (effectively a smart random fallback).

    Hard rule: match_limited users only match with other match_limited users.
    Soft rule: suspected-fake users are softly separated (5 pt mismatch penalty).
    """
    user_is_limited = user_id in match_limited
    my_rep          = reputation_scores.get(user_id, 0)
    now             = datetime.now()

    gender_filter = partner_gender_filter.get(user_id)

    eligible = [
        c for c in waiting_users
        if (c in match_limited) == user_is_limited
        and (gender_filter is None or user_genders.get(c) == gender_filter)
    ]
    if not eligible:
        return None

    def candidate_score(cid):
        # 1. Gender compatibility (primary)
        cg = user_genders.get(cid)
        gender_ok      = bool(cg and are_compatible(my_gender, cg))
        gender_penalty = 0 if gender_ok else 1000

        # 2. Reputation similarity (secondary — smaller diff is better)
        rep_diff    = abs(my_rep - reputation_scores.get(cid, 0))
        rep_penalty = rep_diff * 5

        # 3. Waiting time (tertiary — longer wait lowers the score)
        waited_secs  = (now - waiting_since.get(cid, now)).total_seconds()
        wait_penalty = max(0.0, 300.0 - waited_secs) / 10.0   # 0–30 pts

        # 4. Soft fake-status separation
        fake_penalty = 5 if (cid in suspected_fake) != (user_id in suspected_fake) else 0

        return gender_penalty + rep_penalty + wait_penalty + fake_penalty

    return min(eligible, key=candidate_score)


# ── Helper: message logging ───────────────────────────────────────────────────
def log_message(chat_id, message_id):
    message_log.append({"chat_id": chat_id, "message_id": message_id, "sent_at": datetime.now()})


def rep_keyboard(q_index, partner_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Yes", callback_data=f"rep_{q_index}_yes_{partner_id}"),
        InlineKeyboardButton("👎 No",  callback_data=f"rep_{q_index}_no_{partner_id}"),
    ]])


def _fallback_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Chat Male",    callback_data="fallback_Male"),
            InlineKeyboardButton("Chat Female",  callback_data="fallback_Female"),
        ],
        [
            InlineKeyboardButton("Chat Gay",     callback_data="fallback_Gay"),
            InlineKeyboardButton("Chat Lesbian", callback_data="fallback_Lesbian"),
        ],
        [InlineKeyboardButton("🎲 Random Chat",  callback_data="fallback_random")],
    ])


def _action_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Next",    callback_data="action_next"),
        InlineKeyboardButton("🛑 Stop",    callback_data="action_stop"),
        InlineKeyboardButton("⚠️ Report",  callback_data="action_report"),
    ]])


async def _connect_partners(user_id: int, partner: int, context: ContextTypes.DEFAULT_TYPE):
    """Wire two users into an active chat and schedule reputation questions."""
    waiting_users.remove(partner)
    waiting_since.pop(partner, None)
    waiting_since.pop(user_id, None)
    partner_gender_filter.pop(user_id, None)
    active_chats[user_id] = partner
    active_chats[partner] = user_id
    chat_start_times[user_id] = chat_start_times[partner] = datetime.now()

    kb = _action_keyboard()
    await context.bot.send_message(user_id,  "Partner found! Say hi.", reply_markup=kb)
    await context.bot.send_message(partner,  "Partner found! Say hi.", reply_markup=kb)

    context.job_queue.run_once(send_reputation_question, when=RATING_DELAY,
                               data={"user_id": user_id, "partner_id": partner, "q_index": 0},
                               name=f"rep_{user_id}_0")
    context.job_queue.run_once(send_reputation_question, when=RATING_DELAY,
                               data={"user_id": partner, "partner_id": user_id, "q_index": 0},
                               name=f"rep_{partner}_0")


# ── Helper: media send ────────────────────────────────────────────────────────
async def _send_media(bot, chat_id, media_type, file_id):
    if media_type == "photo":
        return await bot.send_photo(chat_id=chat_id, photo=file_id)
    elif media_type == "video":
        return await bot.send_video(chat_id=chat_id, video=file_id)
    elif media_type == "voice":
        return await bot.send_voice(chat_id=chat_id, voice=file_id)
    elif media_type == "sticker":
        return await bot.send_sticker(chat_id=chat_id, sticker=file_id)


# ── Background jobs ───────────────────────────────────────────────────────────
async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE):
    cutoff = datetime.now() - MESSAGE_MAX_AGE
    to_delete = [m for m in message_log if m["sent_at"] < cutoff]
    keep = [m for m in message_log if m["sent_at"] >= cutoff]
    message_log.clear()
    message_log.extend(keep)

    notified = set()
    for entry in to_delete:
        try:
            await context.bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
        except Exception:
            pass
        notified.add(entry["chat_id"])

    for chat_id in notified:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Your previous chats were automatically cleared.\nStart fresh conversations.",
            )
        except Exception:
            pass


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass


async def send_reputation_question(context: ContextTypes.DEFAULT_TYPE):
    """Send one rating question and schedule the next one a minute later."""
    data       = context.job.data
    user_id    = data["user_id"]
    partner_id = data["partner_id"]
    q_index    = data["q_index"]

    if q_index == 0:
        await context.bot.send_message(
            chat_id=user_id,
            text="⭐ Time to rate your chat partner! A new question will appear every minute:",
        )

    await context.bot.send_message(
        chat_id=user_id,
        text=f"{q_index + 1}. {QUESTIONS[q_index]}",
        reply_markup=rep_keyboard(q_index, partner_id),
    )

    if q_index + 1 < len(QUESTIONS):
        context.job_queue.run_once(
            send_reputation_question,
            when=RATING_INTERVAL,
            data={"user_id": user_id, "partner_id": partner_id, "q_index": q_index + 1},
            name=f"rep_{user_id}_{q_index + 1}",
        )


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("stop",        "Stop the current chat"),
        BotCommand("chat",        "Start a chat"),
        BotCommand("chatmale",    "Start a chat with a male"),
        BotCommand("chatfemale",  "Start a chat with a female"),
        BotCommand("chatlesbian", "Start a chat with a lesbian"),
        BotCommand("chatgay",     "Start a chat with a gay"),
        BotCommand("next",        "End current chat and find a new one"),
        BotCommand("help",        "Get help"),
    ])
    application.job_queue.run_repeating(cleanup_old_messages, interval=timedelta(days=1), first=timedelta(days=3))


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("START", callback_data="start_chat"),
    ]])
    await update.message.reply_text(
        "🌐 Chatty Random Chat Bot\n"
        "\n"
        "Connect with strangers from around the world.\n"
        "\n"
        "Features:\n"
        "• Start exciting conversations\n"
        "• Meet new people\n"
        "• Discover surprising topics\n"
        "• Share ideas and perspectives\n"
        "• Have fun and laugh together\n"
        "\n"
        "Rules:\n"
        "• Be respectful and kind\n"
        "• No inappropriate content\n"
        "• Don't share personal information\n"
        "• Users must be at least 18 years old\n"
        "\n"
        "Press START to begin chatting.",
        reply_markup=keyboard,
    )


async def start_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Male",    callback_data="gender_Male"),
         InlineKeyboardButton("Female",  callback_data="gender_Female")],
        [InlineKeyboardButton("Gay",     callback_data="gender_Gay"),
         InlineKeyboardButton("Lesbian", callback_data="gender_Lesbian")],
    ])
    await query.edit_message_text("Select your gender:", reply_markup=keyboard)


async def gender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gender = query.data.replace("gender_", "")
    user_genders[query.from_user.id] = gender
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Male",      callback_data="pref_Male"),
         InlineKeyboardButton("Female",    callback_data="pref_Female")],
        [InlineKeyboardButton("Gay",       callback_data="pref_Gay"),
         InlineKeyboardButton("Lesbian",   callback_data="pref_Lesbian")],
        [InlineKeyboardButton("🎲 Random", callback_data="pref_random")],
    ])
    await query.edit_message_text("Who do you want to chat with?", reply_markup=keyboard)


async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if is_banned(user_id):
        await update.message.reply_text("You are temporarily banned for 24 hours.")
        return

    # Match cooldown (spam penalty)
    if user_id in match_cooldown_until:
        if datetime.now() < match_cooldown_until[user_id]:
            remaining = int((match_cooldown_until[user_id] - datetime.now()).total_seconds() / 60) + 1
            await update.message.reply_text(
                f"⚠️ Your matching is limited due to suspicious activity.\n"
                f"Please wait {remaining} more minute(s)."
            )
            return
        del match_cooldown_until[user_id]

    if user_id not in user_genders:
        await update.message.reply_text("Please set your gender first using /start.")
        return

    if user_id in active_chats:
        await update.message.reply_text("You are already chatting.")
        return

    if user_id in waiting_users:
        await update.message.reply_text("Waiting for a partner...")
        return

    my_gender = user_genders[user_id]
    partner = find_best_partner(user_id, my_gender)

    if partner is not None:
        await _connect_partners(user_id, partner, context)
    else:
        waiting_users.append(user_id)
        waiting_since[user_id] = datetime.now()
        await update.message.reply_text("🔍 Searching for a partner...")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in active_chats:
        await update.message.reply_text("You are not chatting.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data="next_yes"),
        InlineKeyboardButton("No",  callback_data="next_no"),
    ]])
    await update.message.reply_text(
        "Do you really want to skip this person?",
        reply_markup=keyboard,
    )


async def next_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "next_no":
        await query.edit_message_text("Continuing the chat.")
        return

    # ── Yes: end current chat ──────────────────────────────────────────────
    if user_id not in active_chats:
        await query.edit_message_text("You are no longer in a chat.")
        return

    partner = active_chats.pop(user_id)
    active_chats.pop(partner, None)
    reported_by.pop(user_id, None)
    close_chat(user_id, partner)

    await _notify_partner_left(context.bot, partner, user_id)

    # ── Auto-find a new partner ────────────────────────────────────────────
    if is_banned(user_id):
        await query.edit_message_text("You are temporarily banned. Chat ended.")
        return

    my_gender = user_genders.get(user_id)
    if not my_gender:
        await query.edit_message_text("Chat ended. Use /find to search again.")
        return

    new_partner = find_best_partner(user_id, my_gender)

    if new_partner is not None:
        await query.edit_message_text("Connecting...")
        await _connect_partners(user_id, new_partner, context)
    else:
        waiting_users.append(user_id)
        waiting_since[user_id] = datetime.now()
        await query.edit_message_text("🔍 Searching for a partner...")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in active_chats:
        partner = active_chats.pop(user_id)
        active_chats.pop(partner, None)
        reported_by.pop(user_id, None)
        close_chat(user_id, partner)
        await _notify_partner_left(context.bot, partner, user_id)

    await update.message.reply_text("Chat stopped.")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in active_chats:
        await update.message.reply_text("You can only report someone while in a chat.")
        return

    partner_id = active_chats[user_id]
    already_reported = reported_by.get(user_id, set())

    if partner_id in already_reported:
        await update.message.reply_text("You have already reported this partner.")
        return

    already_reported.add(partner_id)
    reported_by[user_id] = already_reported
    report_counts[partner_id] = report_counts.get(partner_id, 0) + 1
    await update.message.reply_text("Partner reported.")

    # High report rate → spam signal (before reaching ban threshold)
    if report_counts[partner_id] == HIGH_REPORT_THRESHOLD:
        add_spam_signal(partner_id, 2)

    check_fake_gender(partner_id)

    if report_counts[partner_id] >= BAN_THRESHOLD:
        ban_user(partner_id)
        if partner_id in active_chats:
            other = active_chats.pop(partner_id)
            active_chats.pop(other, None)
        if partner_id in waiting_users:
            waiting_users.remove(partner_id)
        await context.bot.send_message(partner_id, "You have been temporarily banned for 24 hours due to multiple reports.")


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text(f"⭐ Your reputation score: {reputation_scores.get(user_id, 0)}")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in active_chats:
        await update.message.reply_text("Use /find to start chatting.")
        return

    partner_id = active_chats[user_id]

    if update.message.photo:
        media_type, file_id = "photo", update.message.photo[-1].file_id
    elif update.message.video:
        media_type, file_id = "video", update.message.video.file_id
    elif update.message.voice:
        media_type, file_id = "voice", update.message.voice.file_id
    elif update.message.sticker:
        media_type, file_id = "sticker", update.message.sticker.file_id
    else:
        return

    media_id = str(next(_media_counter))
    awaiting_mode[media_id] = {"sender_id": user_id, "recipient_id": partner_id,
                                "media_type": media_type, "file_id": file_id}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Send normally",  callback_data=f"sendmode_normal_{media_id}"),
        InlineKeyboardButton("👁 One-time view", callback_data=f"sendmode_once_{media_id}"),
    ]])
    await update.message.reply_text("How do you want to send this media?", reply_markup=keyboard)


async def sendmode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, mode, media_id = query.data.split("_", 2)

    media = awaiting_mode.pop(media_id, None)
    if media is None:
        await query.edit_message_text("This media request has expired.")
        return

    is_one_time = (mode == "once")
    pending_media[media_id] = {**media, "is_one_time": is_one_time}
    mode_label = "one-time view" if is_one_time else "normal"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"media_accept_{media_id}"),
        InlineKeyboardButton("❌ Deny",   callback_data=f"media_deny_{media_id}"),
    ]])
    await query.edit_message_text(f"Sent as {mode_label}. Waiting for partner to accept...")
    await context.bot.send_message(
        chat_id=media["recipient_id"],
        text=f"📎 User wants to send {'one-time view ' if is_one_time else ''}media.",
        reply_markup=keyboard,
    )


async def media_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action, media_id = query.data.split("_", 2)

    media = pending_media.pop(media_id, None)
    if media is None:
        await query.edit_message_text("This media request has expired.")
        return

    if action == "deny":
        await query.edit_message_text("❌ Media denied.")
        return

    recipient_id = media["recipient_id"]
    media_type   = media["media_type"]
    file_id      = media["file_id"]
    is_one_time  = media.get("is_one_time", False)

    if not is_one_time:
        msg = await _send_media(context.bot, recipient_id, media_type, file_id)
        if msg:
            log_message(recipient_id, msg.message_id)
        await query.edit_message_text("✅ Media accepted.")
    else:
        pending_view_once[media_id] = {"recipient_id": recipient_id, "media_type": media_type, "file_id": file_id}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("👁 View", callback_data=f"viewonce_{media_id}")]])
        await query.edit_message_text(
            "👁 You have a one-time view media.\nTap View to open it — it will disappear after 10 seconds.",
            reply_markup=keyboard,
        )


async def viewonce_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    media_id = query.data[len("viewonce_"):]

    media = pending_view_once.pop(media_id, None)
    if media is None:
        await query.edit_message_text("This media has already been viewed or has expired.")
        return

    await query.edit_message_text("👁 Viewed. This media will disappear shortly.")
    msg = await _send_media(context.bot, media["recipient_id"], media["media_type"], media["file_id"])
    if msg:
        context.job_queue.run_once(delete_message_job, when=VIEW_ONCE_TTL,
                                   data={"chat_id": media["recipient_id"], "message_id": msg.message_id})


async def rep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    q_index, answer, partner_id = int(parts[1]), parts[2], int(parts[3])

    if answer == "yes":
        reputation_scores[partner_id] = reputation_scores.get(partner_id, 0) + 1

    label = "👍 Yes" if answer == "yes" else "👎 No"
    await query.edit_message_text(f"{q_index + 1}. {QUESTIONS[q_index]}\nYour answer: {label}")


async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in active_chats:
        await update.message.reply_text("Use /find to start chatting.")
        return

    text = update.message.text
    partner = active_chats[user_id]

    # Flood check (silent penalty)
    if check_flood(user_id):
        add_spam_signal(user_id)

    # Link-spam check (silent penalty)
    if check_links(user_id, text):
        add_spam_signal(user_id, 2)

    msg = await context.bot.send_message(partner, text)
    log_message(partner, msg.message_id)


async def _notify_partner_left(bot, notified_user_id: int, leaving_partner_id: int):
    """Send 'partner left' message with a one-tap report button."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Report User ⚠️", callback_data=f"postreport_{leaving_partner_id}"),
    ]])
    await bot.send_message(
        chat_id=notified_user_id,
        text="Your partner left the chat. 😔\n/next — to start a new chat.",
        reply_markup=keyboard,
    )


async def postreport_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id    = query.from_user.id
    partner_id = int(query.data[len("postreport_"):])

    already_reported = reported_by.get(user_id, set())
    if partner_id in already_reported:
        await query.edit_message_text(
            "Your partner left the chat. 😔\n/next — to start a new chat.\n\n✅ Already reported."
        )
        return

    already_reported.add(partner_id)
    reported_by[user_id] = already_reported
    report_counts[partner_id] = report_counts.get(partner_id, 0) + 1

    if report_counts[partner_id] == HIGH_REPORT_THRESHOLD:
        add_spam_signal(partner_id, 2)

    check_fake_gender(partner_id)

    if report_counts[partner_id] >= BAN_THRESHOLD:
        ban_user(partner_id)
        if partner_id in active_chats:
            other = active_chats.pop(partner_id)
            active_chats.pop(other, None)
        if partner_id in waiting_users:
            waiting_users.remove(partner_id)
        await context.bot.send_message(
            partner_id,
            "You have been temporarily banned for 24 hours due to multiple reports.",
        )

    await query.edit_message_text(
        "Your partner left the chat. 😔\n/next — to start a new chat.\n\n✅ Partner reported."
    )


async def partner_pref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3 of setup: user picks who they want to chat with, then auto-search."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    choice  = query.data[len("pref_"):]   # "Male" | "Female" | "Gay" | "Lesbian" | "random"

    if choice == "random":
        partner_gender_filter.pop(user_id, None)
    else:
        partner_gender_filter[user_id] = choice

    if is_banned(user_id):
        await query.edit_message_text("You are temporarily banned for 24 hours.")
        return
    if user_id in match_cooldown_until and datetime.now() < match_cooldown_until[user_id]:
        remaining = int((match_cooldown_until[user_id] - datetime.now()).total_seconds() / 60) + 1
        await query.edit_message_text(
            f"⚠️ Matching limited due to suspicious activity. Wait {remaining} more minute(s)."
        )
        return
    if user_id in active_chats:
        await query.edit_message_text("You are already chatting.")
        return
    if user_id in waiting_users:
        await query.edit_message_text("🔍 Already searching for a partner...")
        return

    my_gender = user_genders.get(user_id)
    if not my_gender:
        await query.edit_message_text("Please use /start to set up your profile first.")
        return

    partner = find_best_partner(user_id, my_gender)
    if partner is not None:
        await query.edit_message_text("Connecting...")
        await _connect_partners(user_id, partner, context)
    else:
        waiting_users.append(user_id)
        waiting_since[user_id] = datetime.now()
        await query.edit_message_text("🔍 Searching for a partner...")


async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the ⏭ Next / 🛑 Stop / ⚠️ Report buttons shown during a chat."""
    query  = update.callback_query
    user_id = query.from_user.id
    action  = query.data   # "action_next" | "action_stop" | "action_report"

    if action == "action_next":
        if user_id not in active_chats:
            await query.answer("You are not in an active chat.", show_alert=True)
            return
        await query.answer()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes", callback_data="next_yes"),
            InlineKeyboardButton("No",  callback_data="next_no"),
        ]])
        await context.bot.send_message(
            chat_id=user_id,
            text="Do you really want to skip this person?",
            reply_markup=keyboard,
        )

    elif action == "action_stop":
        if user_id not in active_chats:
            await query.answer("You are not in an active chat.", show_alert=True)
            return
        await query.answer()
        partner = active_chats.pop(user_id)
        active_chats.pop(partner, None)
        reported_by.pop(user_id, None)
        close_chat(user_id, partner)
        await _notify_partner_left(context.bot, partner, user_id)
        await context.bot.send_message(user_id, "Chat stopped.")

    elif action == "action_report":
        if user_id not in active_chats:
            await query.answer("You are not in an active chat.", show_alert=True)
            return
        partner_id = active_chats[user_id]
        already_reported = reported_by.get(user_id, set())
        if partner_id in already_reported:
            await query.answer("You have already reported this partner.", show_alert=True)
            return
        already_reported.add(partner_id)
        reported_by[user_id] = already_reported
        report_counts[partner_id] = report_counts.get(partner_id, 0) + 1
        if report_counts[partner_id] == HIGH_REPORT_THRESHOLD:
            add_spam_signal(partner_id, 2)
        check_fake_gender(partner_id)
        if report_counts[partner_id] >= BAN_THRESHOLD:
            ban_user(partner_id)
            if partner_id in active_chats:
                other = active_chats.pop(partner_id)
                active_chats.pop(other, None)
            if partner_id in waiting_users:
                waiting_users.remove(partner_id)
            await context.bot.send_message(
                partner_id,
                "You have been temporarily banned for 24 hours due to multiple reports.",
            )
        await query.answer("Partner reported. ✅", show_alert=True)


async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await find(update, context)


def _chat_with_gender(gender: str):
    """Factory — returns a handler that filters by partner gender.
    Shows smart fallback buttons if nobody of that gender is waiting."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id

        if is_banned(user_id):
            await update.message.reply_text("You are temporarily banned for 24 hours.")
            return
        if user_id in match_cooldown_until:
            if datetime.now() < match_cooldown_until[user_id]:
                remaining = int((match_cooldown_until[user_id] - datetime.now()).total_seconds() / 60) + 1
                await update.message.reply_text(
                    f"⚠️ Your matching is limited due to suspicious activity.\n"
                    f"Please wait {remaining} more minute(s)."
                )
                return
            del match_cooldown_until[user_id]
        if user_id not in user_genders:
            await update.message.reply_text("Please set your gender first using /start.")
            return
        if user_id in active_chats:
            await update.message.reply_text("You are already chatting.")
            return
        if user_id in waiting_users:
            await update.message.reply_text("Waiting for a partner...")
            return

        partner_gender_filter[user_id] = gender
        partner = find_best_partner(user_id, user_genders[user_id])

        if partner is not None:
            await _connect_partners(user_id, partner, context)
        else:
            partner_gender_filter.pop(user_id, None)
            await update.message.reply_text(
                "No matching user found right now. Try another option.",
                reply_markup=_fallback_keyboard(),
            )
    return handler


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    choice = query.data[len("fallback_"):]   # "Male" | "Female" | "Gay" | "Lesbian" | "random"

    if choice == "random":
        partner_gender_filter.pop(user_id, None)
    else:
        partner_gender_filter[user_id] = choice

    if is_banned(user_id):
        await query.edit_message_text("You are temporarily banned for 24 hours.")
        return
    if user_id in active_chats:
        await query.edit_message_text("You are already chatting.")
        return
    if user_id in waiting_users:
        await query.edit_message_text("Still searching for a partner...")
        return

    my_gender = user_genders.get(user_id)
    if not my_gender:
        await query.edit_message_text("Please set your gender first using /start.")
        return

    partner = find_best_partner(user_id, my_gender)

    if partner is not None:
        await query.edit_message_text("Connecting...")
        await _connect_partners(user_id, partner, context)
    else:
        waiting_users.append(user_id)
        waiting_since[user_id] = datetime.now()
        await query.edit_message_text("🔍 Searching for a partner...")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available commands:\n\n"
        "/chat — Start a random chat\n"
        "/chatmale — Find a male partner\n"
        "/chatfemale — Find a female partner\n"
        "/chatlesbian — Find a lesbian partner\n"
        "/chatgay — Find a gay partner\n"
        "/next — Skip to a new partner\n"
        "/stop — End the current chat\n"
        "/report — Report your current partner\n"
        "/score — View your reputation score"
    )


# ── App setup ─────────────────────────────────────────────────────────────────
token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

app = ApplicationBuilder().token(token).post_init(post_init).build()

MEDIA_FILTER = filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Sticker.ALL

app.add_handler(CommandHandler("start",       start))
app.add_handler(CommandHandler("find",        find))
app.add_handler(CommandHandler("chat",        chat_cmd))
app.add_handler(CommandHandler("chatmale",    _chat_with_gender("Male")))
app.add_handler(CommandHandler("chatfemale",  _chat_with_gender("Female")))
app.add_handler(CommandHandler("chatlesbian", _chat_with_gender("Lesbian")))
app.add_handler(CommandHandler("chatgay",     _chat_with_gender("Gay")))
app.add_handler(CommandHandler("next",        next_cmd))
app.add_handler(CommandHandler("stop",        stop))
app.add_handler(CommandHandler("report",      report))
app.add_handler(CommandHandler("score",       score))
app.add_handler(CommandHandler("help",        help_cmd))
app.add_handler(CallbackQueryHandler(start_chat_callback,   pattern=r"^start_chat$"))
app.add_handler(CallbackQueryHandler(gender_callback,       pattern=r"^gender_"))
app.add_handler(CallbackQueryHandler(partner_pref_callback, pattern=r"^pref_"))
app.add_handler(CallbackQueryHandler(action_callback,       pattern=r"^action_"))
app.add_handler(CallbackQueryHandler(next_confirm_callback, pattern=r"^next_"))
app.add_handler(CallbackQueryHandler(fallback_callback,     pattern=r"^fallback_"))
app.add_handler(CallbackQueryHandler(postreport_callback,   pattern=r"^postreport_"))
app.add_handler(CallbackQueryHandler(sendmode_callback,     pattern=r"^sendmode_"))
app.add_handler(CallbackQueryHandler(media_callback,        pattern=r"^media_"))
app.add_handler(CallbackQueryHandler(viewonce_callback,     pattern=r"^viewonce_"))
app.add_handler(CallbackQueryHandler(rep_callback,          pattern=r"^rep_"))
app.add_handler(MessageHandler(MEDIA_FILTER, handle_media))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay))

app.run_polling()
