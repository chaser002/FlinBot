"""
Telegram-бот техподдержки: 2FA, связь с оператором, восстановление пароля.
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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
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
)

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8868354676:AAFF3u0w7y_k80X9EphALrwb1n5uY_qjc3k")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "-1004473683223"))  # группа операторов
PROJECT_NAME = os.getenv("PROJECT_NAME", "FlinRP")
SITE_URL = os.getenv("SITE_URL", "https://flin-rp.online/#")
FORUM_URL = os.getenv("FORUM_URL", "https://flin-rp.online/forum/")
SHOP_URL = os.getenv("SHOP_URL", "https://flin-rp.online/#donate")
BANNER_PATH = os.getenv("BANNER_PATH", "banner.jpg")  # картинка над меню
DB_PATH = os.getenv("DB_PATH", "bot.db")

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
        "Если это ваш аккаунт, обратитесь в службу поддержки."
    ),
    "2fa_already": (
        "Двухфакторная защита для этого аккаунта уже включена.\n"
        "Чтобы отключить её, обратитесь в службу поддержки."
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
        "Если нужна помощь, обратитесь в службу поддержки."
    ),
    "reset_limit": "Слишком много запросов. Попробуйте позже или напишите в поддержку.",
    "reset_ok": (
        "✅ Пароль сброшен.\n\nНовый временный пароль:\n<tg-spoiler>{password}</tg-spoiler>\n\n"
        "Сообщение удалится через минуту. Войдите и сразу смените пароль."
    ),
    "support_off": "Служба поддержки временно недоступна.",
    "support_ask": "Опишите проблему одним или несколькими сообщениями. Оператор ответит прямо здесь.",
    "support_sent": "✅ Обращение принято. Ожидайте ответа оператора.",
    "support_end": "Обращение завершено.",
    "operator_reply": "👨‍💼 <b>Ответ службы поддержки:</b>",
    "ticket_header": (
        "🆘 <b>Новое обращение</b>\n"
        "Пользователь: {name} {username}\nID: <code>{uid}</code>\n\n"
        "Чтобы ответить, используйте «Ответить» на любое сообщение ниже."
    ),
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
    CREATE TABLE IF NOT EXISTS relay (
        chat_msg_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL
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


def end_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Завершить обращение", callback_data="end_support")]]
    )


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


def remember(chat_msg_id, user_id):
    db.execute("INSERT OR REPLACE INTO relay VALUES (?, ?)", (chat_msg_id, user_id))
    db.commit()


async def delete_later(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class TwoFA(StatesGroup):
    nickname = State()
    code = State()


class Reset(StatesGroup):
    nickname = State()
    code = State()


class Support(StatesGroup):
    chatting = State()


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


# ───────────────────────── СВЯЗЬ С ОПЕРАТОРОМ ─────────────────────────
@user_router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if not SUPPORT_CHAT_ID:
        await call.message.answer(T["support_off"])
        return
    await state.set_state(Support.chatting)
    await state.update_data(started=False)
    await call.message.answer(T["support_ask"], reply_markup=end_kb())


@user_router.callback_query(F.data == "end_support")
async def cb_end_support(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    data = await state.get_data()
    if data.get("started") and SUPPORT_CHAT_ID:
        await bot.send_message(SUPPORT_CHAT_ID, f"✅ Пользователь <code>{call.from_user.id}</code> завершил обращение.")
    await state.clear()
    await call.message.answer(T["support_end"])
    await send_menu(bot, call.message.chat.id, call.from_user.first_name)


@user_router.message(Support.chatting)
async def support_user_message(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = message.from_user
    first = not data.get("started")
    if first:
        username = f"@{user.username}" if user.username else ""
        header = await bot.send_message(
            SUPPORT_CHAT_ID,
            T["ticket_header"].format(name=html.escape(user.full_name), username=username, uid=user.id),
        )
        remember(header.message_id, user.id)
        await state.update_data(started=True)
    forwarded = await message.forward(SUPPORT_CHAT_ID)
    remember(forwarded.message_id, user.id)
    if first:
        await message.answer(T["support_sent"], reply_markup=end_kb())


# Ответ оператора: «Ответить» на сообщение бота в группе поддержки
@ops_router.message(F.chat.id == SUPPORT_CHAT_ID, F.reply_to_message)
async def operator_reply(message: Message, bot: Bot):
    row = db.execute(
        "SELECT user_id FROM relay WHERE chat_msg_id=?", (message.reply_to_message.message_id,)
    ).fetchone()
    if not row:
        return
    try:
        await bot.send_message(row["user_id"], T["operator_reply"])
        await message.copy_to(row["user_id"])
    except TelegramForbiddenError:
        await message.reply("Не удалось доставить: пользователь заблокировал бота.")


# Всё остальное в личке: показать меню
@user_router.message()
async def fallback(message: Message, bot: Bot):
    await send_menu(bot, message.chat.id, message.from_user.first_name)


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
