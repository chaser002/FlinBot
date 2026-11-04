"""
Telegram-бот техподдержки: тикеты, 2FA, восстановление пароля.
Библиотека: aiogram 3.x. Настройки берутся из переменных окружения.
"""
import asyncio
import html
import io
import logging
import os
import re
import secrets
import sqlite3
import time

import pyotp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8868354676:AAFF3u0w7y_k80X9EphALrwb1n5uY_qjc3k")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-1004390723972"))  # группа операторов
PROJECT_NAME = os.getenv("PROJECT_NAME", "FlinRP")
SITE_URL = os.getenv("SITE_URL", "https://flin-rp.online/#")
FORUM_URL = os.getenv("FORUM_URL", "https://flin-rp.online/forum/")
SHOP_URL = os.getenv("SHOP_URL", "https://flin-rp.online/#donate")
BANNER_PATH = os.getenv("BANNER_PATH", "banner.jpg")  # картинка над меню
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Темы тикетов: можно менять и добавлять свои
TICKET_CATEGORIES = [
    "🔐 Проблема с аккаунтом",
    "💳 Донат / оплата",
    "🐞 Баг / ошибка",
    "❓ Другое",
]
STATUS_TITLES = {"open": "🟡 Новый", "progress": "🔵 В работе", "closed": "🟢 Закрыт"}

# ───────────────────────── ТЕКСТЫ ─────────────────────────
T = {
    "start": (
        "Привет, <b>{name}</b>!\n\n"
        "Я — виртуальный помощник проекта <b>{project}</b>.\n\n"
        "Для начала выбери нужное действие:"
    ),
    "ask_nick": "Введите игровой ник аккаунта:",
    "bad_nick": "Ник может содержать только латинские буквы, цифры и символы _ - . (3–24 символа). Попробуйте ещё раз:",
    "not_owner": (
        "Этот аккаунт привязан к другому Telegram или не найден.\n"
        "Если это ваш аккаунт, создайте тикет в службе поддержки."
    ),
    "2fa_already": (
        "Двухфакторная защита для этого аккаунта уже включена.\n"
        "Чтобы отключить её, создайте тикет в службе поддержки."
    ),
    "2fa_setup": (
        "1. Установите приложение-аутентификатор (Google Authenticator, Authy и т. п.).\n"
        "2. Отсканируйте QR-код или введите ключ вручную:\n<code>{secret}</code>\n"
        "3. Отправьте сюда 6-значный код из приложения.\n\n"
        "Сохраните ключ в надёжном месте — он понадобится при смене телефона."
    ),
    "2fa_bad_code": "Неверный код. Осталось попыток: {left}.",
    "2fa_ok": "✅ Двухфакторная защита подключена.",
    "too_many": "Слишком много неверных попыток. Начните заново через меню.",
    "ask_code": "Для подтверждения отправьте 6-значный код из приложения-аутентификатора:",
    "reset_denied": (
        "Не удалось подтвердить аккаунт. Он должен быть привязан к этому Telegram.\n"
        "Если нужна помощь, создайте тикет в службе поддержки."
    ),
    "reset_limit": "Слишком много запросов. Попробуйте позже или создайте тикет.",
    "reset_ok": (
        "✅ Пароль сброшен.\n\nНовый временный пароль:\n<tg-spoiler>{password}</tg-spoiler>\n\n"
        "Сообщение удалится через минуту. Войдите и сразу смените пароль."
    ),
    # тикеты
    "support_off": "Служба поддержки временно недоступна.",
    "support_menu": "Выберите тему обращения:",
    "ticket_exists": (
        "У вас уже есть открытый тикет <b>#{id}</b>.\n"
        "Просто напишите сообщение сюда, оно будет добавлено в тикет и передано оператору."
    ),
    "ticket_ask": "Тема: <b>{category}</b>\n\nОпишите проблему одним сообщением. Можно приложить скриншот.",
    "ticket_created": (
        "✅ Тикет <b>#{id}</b> создан.\n"
        "Оператор ответит прямо здесь. Новые сообщения будут добавляться в этот тикет."
    ),
    "ticket_failed": "Не удалось создать тикет. Попробуйте позже.",
    "ticket_taken": "🔵 Ваш тикет <b>#{id}</b> принят в работу.",
    "ticket_closed_user": "🟢 Тикет <b>#{id}</b> закрыт. Если проблема осталась, создайте новый.",
    "operator_reply": "👨‍💼 <b>Ответ по тикету #{id}:</b>",
    "my_tickets_empty": "У вас пока нет тикетов.",
    "flood": "Слишком много сообщений. Подождите минуту.",
}

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS accounts (
        nickname TEXT PRIMARY KEY COLLATE NOCASE,
        tg_id INTEGER,
        totp_secret TEXT,
        totp_enabled INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        category TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        assignee TEXT,
        card_msg_id INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ticket_relay (
        chat_msg_id INTEGER PRIMARY KEY,
        ticket_id INTEGER NOT NULL
    );
    """
)


# ───────────────── ИНТЕГРАЦИЯ С ИГРОВЫМ СЕРВЕРОМ ─────────────────
class Backend:
    """
    ДЕМО-РЕАЛИЗАЦИЯ на локальной SQLite. Перед запуском для игроков замените
    методы на обращения к вашей игровой БД или API.

    ВАЖНО: link_owner в демо привязывает ник к первому, кто его назвал.
    В боевой версии привязка должна подтверждаться владельцем аккаунта
    (например, одноразовый код, который игрок вводит в игре или на сайте).
    """

    def get_owner(self, nick):
        """Telegram ID, привязанный к аккаунту, или None."""
        row = db.execute("SELECT tg_id FROM accounts WHERE nickname=?", (nick,)).fetchone()
        return row["tg_id"] if row else None

    def link_owner(self, nick, tg_id):
        """Привязать аккаунт к Telegram. Вернуть True при успехе."""
        db.execute("INSERT OR IGNORE INTO accounts(nickname) VALUES (?)", (nick,))
        db.execute("UPDATE accounts SET tg_id=? WHERE nickname=? AND tg_id IS NULL", (tg_id, nick))
        db.commit()
        return self.get_owner(nick) == tg_id

    def get_totp(self, nick):
        """(секрет, включена ли 2FA)."""
        row = db.execute("SELECT totp_secret, totp_enabled FROM accounts WHERE nickname=?", (nick,)).fetchone()
        return (row["totp_secret"], bool(row["totp_enabled"])) if row else (None, False)

    def save_totp(self, nick, secret, enabled):
        db.execute("UPDATE accounts SET totp_secret=?, totp_enabled=? WHERE nickname=?", (secret, int(enabled), nick))
        db.commit()

    def set_password(self, nick, new_password):
        """Установить новый пароль в игре. В демо только пишет в лог (без пароля)."""
        logging.info("DEMO: пароль для %s сброшен (замените Backend.set_password)", nick)


backend = Backend()

# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ ─────────────────────────
NICK_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,24}$")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
reset_log = {}  # user_id -> список времён сбросов
msg_times = {}  # user_id -> времена сообщений (антифлуд)
background = set()


def main_menu():
    rows = [
        [InlineKeyboardButton(text="🔐 Подключить двухфакторную защиту", callback_data="2fa")],
        [InlineKeyboardButton(text="👨‍💼 Связь со службой поддержки", callback_data="support")],
        [InlineKeyboardButton(text="🔑 Восстановить пароль", callback_data="reset")],
    ]
    links = [
        InlineKeyboardButton(text=title, url=url)
        for title, url in (("🌐 Сайт", SITE_URL), ("📙 Форум", FORUM_URL), ("🛒 Магазин", SHOP_URL))
        if url
    ]
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])


def categories_kb():
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"cat:{i}")]
        for i, title in enumerate(TICKET_CATEGORIES)
    ]
    rows.append([InlineKeyboardButton(text="📋 Мои тикеты", callback_data="mytickets")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_ticket_kb(tid):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Закрыть тикет", callback_data=f"u_close:{tid}")]]
    )


def ops_kb(tid, status):
    if status == "closed":
        return None
    row = []
    if status == "open":
        row.append(InlineKeyboardButton(text="🙋 Взять в работу", callback_data=f"t_take:{tid}"))
    row.append(InlineKeyboardButton(text="✅ Закрыть", callback_data=f"t_close:{tid}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def send_menu(bot: Bot, chat_id: int, name: str):
    caption = T["start"].format(name=html.escape(name), project=html.escape(PROJECT_NAME))
    if os.path.exists(BANNER_PATH):
        await bot.send_photo(chat_id, FSInputFile(BANNER_PATH), caption=caption, reply_markup=main_menu())
    else:
        await bot.send_message(chat_id, caption, reply_markup=main_menu())


def make_qr(uri):
    try:
        import qrcode
    except ImportError:
        return None
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    return buf.getvalue()


async def delete_later(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


def flooding(uid, limit=15, window=60):
    now = time.time()
    times = [t for t in msg_times.get(uid, []) if now - t < window]
    times.append(now)
    msg_times[uid] = times
    return len(times) > limit


# ───────────────────────── ТИКЕТЫ: БАЗА ─────────────────────────
def get_ticket(tid):
    return db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()


def open_ticket(user_id):
    return db.execute(
        "SELECT * FROM tickets WHERE user_id=? AND status!='closed' ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()


def link_msg(chat_msg_id, tid):
    db.execute("INSERT OR REPLACE INTO ticket_relay VALUES (?, ?)", (chat_msg_id, tid))
    db.commit()


def card_text(t):
    lines = [
        f"🎫 <b>Тикет #{t['id']}</b>",
        f"Категория: {html.escape(t['category'])}",
        f"Игрок: {html.escape(t['user_name'])} (<code>{t['user_id']}</code>)",
        f"Статус: {STATUS_TITLES[t['status']]}",
    ]
    if t["assignee"]:
        lines.append(f"Оператор: {html.escape(t['assignee'])}")
    if t["status"] != "closed":
        lines.append("\nЧтобы ответить игроку, нажмите «Ответить» на любое сообщение этого тикета.")
    return "\n".join(lines)


async def refresh_card(bot: Bot, tid):
    t = get_ticket(tid)
    if not t or not t["card_msg_id"]:
        return
    try:
        await bot.edit_message_text(
            card_text(t),
            chat_id=SUPPORT_CHAT_ID,
            message_id=t["card_msg_id"],
            reply_markup=ops_kb(tid, t["status"]),
        )
    except TelegramBadRequest:
        pass


def person(user):
    if user is None:
        return "Оператор"
    return user.full_name


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class TwoFA(StatesGroup):
    nickname = State()
    code = State()


class Reset(StatesGroup):
    nickname = State()
    code = State()


class Ticket(StatesGroup):
    text = State()


common_router = Router()
user_router = Router()
ops_router = Router()
user_router.message.filter(F.chat.type == "private")


# ───────────────────────── ОБЩЕЕ ─────────────────────────
@common_router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await send_menu(bot, message.chat.id, message.from_user.first_name)


@user_router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    await state.clear()
    await send_menu(bot, call.message.chat.id, call.from_user.first_name)


# ───────────────────────── 2FA ─────────────────────────
@user_router.callback_query(F.data == "2fa")
async def cb_2fa(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(TwoFA.nickname)
    await call.message.answer(T["ask_nick"], reply_markup=cancel_kb())


@user_router.message(TwoFA.nickname, F.text)
async def twofa_nick(message: Message, state: FSMContext):
    nick = message.text.strip()
    if not NICK_RE.match(nick):
        await message.answer(T["bad_nick"], reply_markup=cancel_kb())
        return
    uid = message.from_user.id
    owner = backend.get_owner(nick)
    if owner is None:
        if not backend.link_owner(nick, uid):
            await state.clear()
            await message.answer(T["not_owner"])
            return
    elif owner != uid:
        await state.clear()
        await message.answer(T["not_owner"])
        return

    _, enabled = backend.get_totp(nick)
    if enabled:
        await state.clear()
        await message.answer(T["2fa_already"])
        return

    secret = pyotp.random_base32()
    await state.update_data(nick=nick, secret=secret, tries=0)
    await state.set_state(TwoFA.code)
    uri = pyotp.TOTP(secret).provisioning_uri(name=nick, issuer_name=PROJECT_NAME)
    qr = make_qr(uri)
    text = T["2fa_setup"].format(secret=secret)
    if qr:
        await message.answer_photo(BufferedInputFile(qr, "qr.png"), caption=text, reply_markup=cancel_kb())
    else:
        await message.answer(text, reply_markup=cancel_kb())


@user_router.message(TwoFA.code, F.text)
async def twofa_code(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = message.text.strip().replace(" ", "")
    if pyotp.TOTP(data["secret"]).verify(code, valid_window=1):
        backend.save_totp(data["nick"], data["secret"], True)
        await state.clear()
        await message.answer(T["2fa_ok"])
        await send_menu(bot, message.chat.id, message.from_user.first_name)
        return
    tries = data.get("tries", 0) + 1
    if tries >= 5:
        await state.clear()
        await message.answer(T["too_many"])
        return
    await state.update_data(tries=tries)
    await message.answer(T["2fa_bad_code"].format(left=5 - tries), reply_markup=cancel_kb())


# ───────────────────────── ВОССТАНОВЛЕНИЕ ПАРОЛЯ ─────────────────────────
@user_router.callback_query(F.data == "reset")
async def cb_reset(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Reset.nickname)
    await call.message.answer(T["ask_nick"], reply_markup=cancel_kb())


async def do_reset(message: Message, state: FSMContext, nick: str):
    uid = message.from_user.id
    now = time.time()
    history = [t for t in reset_log.get(uid, []) if now - t < 3600]
    if len(history) >= 3:
        await state.clear()
        await message.answer(T["reset_limit"])
        return
    history.append(now)
    reset_log[uid] = history

    password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(12))
    backend.set_password(nick, password)
    await state.clear()
    sent = await message.answer(T["reset_ok"].format(password=html.escape(password)))
    task = asyncio.create_task(delete_later(sent, 60))
    background.add(task)
    task.add_done_callback(background.discard)


@user_router.message(Reset.nickname, F.text)
async def reset_nick(message: Message, state: FSMContext):
    nick = message.text.strip()
    if not NICK_RE.match(nick):
        await message.answer(T["bad_nick"], reply_markup=cancel_kb())
        return
    if backend.get_owner(nick) != message.from_user.id:
        await state.clear()
        await message.answer(T["reset_denied"])
        return
    _, enabled = backend.get_totp(nick)
    if enabled:
        await state.update_data(nick=nick, tries=0)
        await state.set_state(Reset.code)
        await message.answer(T["ask_code"], reply_markup=cancel_kb())
    else:
        await do_reset(message, state, nick)


@user_router.message(Reset.code, F.text)
async def reset_code(message: Message, state: FSMContext):
    data = await state.get_data()
    secret, _ = backend.get_totp(data["nick"])
    code = message.text.strip().replace(" ", "")
    if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
        await do_reset(message, state, data["nick"])
        return
    tries = data.get("tries", 0) + 1
    if tries >= 5:
        await state.clear()
        await message.answer(T["too_many"])
        return
    await state.update_data(tries=tries)
    await message.answer(T["2fa_bad_code"].format(left=5 - tries), reply_markup=cancel_kb())


# ───────────────────────── ТИКЕТЫ: ИГРОК ─────────────────────────
@user_router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    if not SUPPORT_CHAT_ID:
        await call.message.answer(T["support_off"])
        return
    t = open_ticket(call.from_user.id)
    if t:
        await call.message.answer(T["ticket_exists"].format(id=t["id"]), reply_markup=user_ticket_kb(t["id"]))
        return
    await call.message.answer(T["support_menu"], reply_markup=categories_kb())


@user_router.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if not SUPPORT_CHAT_ID:
        await call.message.answer(T["support_off"])
        return
    t = open_ticket(call.from_user.id)
    if t:
        await call.message.answer(T["ticket_exists"].format(id=t["id"]), reply_markup=user_ticket_kb(t["id"]))
        return
    try:
        category = TICKET_CATEGORIES[int(call.data.split(":")[1])]
    except (ValueError, IndexError):
        return
    await state.set_state(Ticket.text)
    await state.update_data(category=category)
    await call.message.answer(T["ticket_ask"].format(category=html.escape(category)), reply_markup=cancel_kb())


@user_router.message(Ticket.text)
async def ticket_create(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = message.from_user
    if open_ticket(user.id):
        await state.clear()
        return
    name = user.full_name + (f" @{user.username}" if user.username else "")
    cur = db.execute(
        "INSERT INTO tickets(user_id, user_name, category, created_at) VALUES (?, ?, ?, ?)",
        (user.id, name, data.get("category", "Другое"), int(time.time())),
    )
    db.commit()
    tid = cur.lastrowid
    try:
        card = await bot.send_message(
            SUPPORT_CHAT_ID, card_text(get_ticket(tid)), reply_markup=ops_kb(tid, "open")
        )
        link_msg(card.message_id, tid)
        db.execute("UPDATE tickets SET card_msg_id=? WHERE id=?", (card.message_id, tid))
        db.commit()
        forwarded = await message.forward(SUPPORT_CHAT_ID)
        link_msg(forwarded.message_id, tid)
    except TelegramAPIError:
        logging.exception("Не удалось отправить тикет в группу поддержки")
        db.execute("DELETE FROM tickets WHERE id=?", (tid,))
        db.commit()
        await state.clear()
        await message.answer(T["ticket_failed"])
        return
    await state.clear()
    await message.answer(T["ticket_created"].format(id=tid), reply_markup=user_ticket_kb(tid))


@user_router.callback_query(F.data == "mytickets")
async def cb_mytickets(call: CallbackQuery):
    await call.answer()
    rows = db.execute(
        "SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 5", (call.from_user.id,)
    ).fetchall()
    if not rows:
        await call.message.answer(T["my_tickets_empty"])
        return
    lines = [f"#{r['id']} · {html.escape(r['category'])} · {STATUS_TITLES[r['status']]}" for r in rows]
    current = next((r for r in rows if r["status"] != "closed"), None)
    await call.message.answer(
        "📋 <b>Ваши тикеты:</b>\n" + "\n".join(lines),
        reply_markup=user_ticket_kb(current["id"]) if current else None,
    )


@user_router.callback_query(F.data.startswith("u_close:"))
async def cb_user_close(call: CallbackQuery, bot: Bot):
    try:
        tid = int(call.data.split(":")[1])
    except ValueError:
        await call.answer()
        return
    t = get_ticket(tid)
    if not t or t["user_id"] != call.from_user.id:
        await call.answer()
        return
    if t["status"] == "closed":
        await call.answer("Тикет уже закрыт.")
        return
    db.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    db.commit()
    await refresh_card(bot, tid)
    try:
        await bot.send_message(
            SUPPORT_CHAT_ID, f"🟢 Игрок закрыл тикет #{tid}.", reply_to_message_id=t["card_msg_id"]
        )
    except TelegramAPIError:
        pass
    await call.answer("Тикет закрыт.")
    await call.message.answer(T["ticket_closed_user"].format(id=tid))


# ───────────────────────── ТИКЕТЫ: ОПЕРАТОРЫ ─────────────────────────
@ops_router.callback_query(F.data.startswith("t_take:"), F.message.chat.id == SUPPORT_CHAT_ID)
async def cb_take(call: CallbackQuery, bot: Bot):
    try:
        tid = int(call.data.split(":")[1])
    except ValueError:
        await call.answer()
        return
    t = get_ticket(tid)
    if not t or t["status"] == "closed":
        await call.answer("Тикет уже закрыт.", show_alert=True)
        return
    db.execute("UPDATE tickets SET status='progress', assignee=? WHERE id=?", (person(call.from_user), tid))
    db.commit()
    await refresh_card(bot, tid)
    await call.answer("Вы взяли тикет в работу.")
    try:
        await bot.send_message(t["user_id"], T["ticket_taken"].format(id=tid))
    except TelegramAPIError:
        pass


@ops_router.callback_query(F.data.startswith("t_close:"), F.message.chat.id == SUPPORT_CHAT_ID)
async def cb_op_close(call: CallbackQuery, bot: Bot):
    try:
        tid = int(call.data.split(":")[1])
    except ValueError:
        await call.answer()
        return
    t = get_ticket(tid)
    if not t or t["status"] == "closed":
        await call.answer("Тикет уже закрыт.", show_alert=True)
        return
    db.execute(
        "UPDATE tickets SET status='closed', assignee=COALESCE(assignee, ?) WHERE id=?",
        (person(call.from_user), tid),
    )
    db.commit()
    await refresh_card(bot, tid)
    await call.answer("Тикет закрыт.")
    try:
        await bot.send_message(t["user_id"], T["ticket_closed_user"].format(id=tid))
    except TelegramAPIError:
        pass


# Ответ оператора: «Ответить» на карточку или пересланное сообщение тикета
@ops_router.message(F.chat.id == SUPPORT_CHAT_ID, F.reply_to_message)
async def operator_reply(message: Message, bot: Bot):
    row = db.execute(
        "SELECT t.* FROM ticket_relay r JOIN tickets t ON t.id = r.ticket_id WHERE r.chat_msg_id=?",
        (message.reply_to_message.message_id,),
    ).fetchone()
    if not row:
        return
    if row["status"] == "closed":
        await message.reply(f"Тикет #{row['id']} уже закрыт.")
        return
    try:
        await bot.send_message(row["user_id"], T["operator_reply"].format(id=row["id"]))
        await message.copy_to(row["user_id"])
    except TelegramForbiddenError:
        await message.reply("Не удалось доставить: игрок заблокировал бота.")
        return
    if row["status"] == "open":
        db.execute(
            "UPDATE tickets SET status='progress', assignee=? WHERE id=?", (person(message.from_user), row["id"])
        )
        db.commit()
        await refresh_card(bot, row["id"])


# ───────────────────────── СООБЩЕНИЯ ИГРОКА ВНЕ СОСТОЯНИЙ ─────────────────────────
# Если у игрока есть открытый тикет, сообщение добавляется в него. Иначе показываем меню.
@user_router.message(StateFilter(None))
async def user_message(message: Message, bot: Bot):
    t = open_ticket(message.from_user.id)
    if not t or not SUPPORT_CHAT_ID:
        await send_menu(bot, message.chat.id, message.from_user.first_name)
        return
    if flooding(message.from_user.id):
        await message.answer(T["flood"])
        return
    try:
        forwarded = await message.forward(SUPPORT_CHAT_ID)
        link_msg(forwarded.message_id, t["id"])
    except TelegramAPIError:
        logging.exception("Не удалось переслать сообщение в тикет")
        await message.answer(T["ticket_failed"])
        return
    try:
        await message.react([ReactionTypeEmoji(emoji="👍")])
    except TelegramBadRequest:
        pass


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    logging.warning("Backend работает в ДЕМО-режиме. Подключите игровую БД/API перед запуском для игроков.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common_router)
    dp.include_router(ops_router)
    dp.include_router(user_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
