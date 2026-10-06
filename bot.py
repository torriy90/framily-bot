import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
import anthropic

logging.basicConfig(level=logging.INFO)
# httpx пишет в логи полный URL запроса, а в нём токен бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
OWNER_ID = int(os.environ["OWNER_ID"])

MODEL = "claude-sonnet-4-6"
MAX_HISTORY = 40        # сколько последних сообщений помним на каждый чат
DEBOUNCE_SECONDS = 3.0  # ждём столько после последнего сообщения (для пачек пересылок)
TG_LIMIT = 4000

# Постоянное хранилище: на Railway это volume, смонтированный в /data
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "."
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
ALLOWED_FILE = os.path.join(DATA_DIR, "allowed.json")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


history = load_json(HISTORY_FILE, {})  # {"chat_id": [{"role":..., "content":...}]}
allowed_ids = set(load_json(ALLOWED_FILE, []))
allowed_ids.add(OWNER_ID)

pending = {}  # chat_id -> {"parts": [...], "message": Message, "timer": Task}
locks = {}    # chat_id -> asyncio.Lock


def get_lock(chat_id):
    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()
    return locks[chat_id]


def forward_label(message):
    origin = getattr(message, "forward_origin", None)
    if not origin:
        return None
    name = None
    sender_user = getattr(origin, "sender_user", None)
    if sender_user:
        name = sender_user.full_name
    if not name:
        name = getattr(origin, "sender_user_name", None)
    if not name:
        chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
        if chat:
            name = chat.title
    return name or "неизвестный источник"


def build_part(message, clean_text):
    label = forward_label(message)
    if label:
        return f"[Пересланное сообщение, автор: {label}]\n{clean_text}"
    reply = message.reply_to_message
    if (
        reply
        and reply.text
        and not (reply.from_user and reply.from_user.username == BOT_USERNAME)
    ):
        quoted = reply.text[:500]
        return f"[Ответ на сообщение: «{quoted}»]\n{clean_text}"
    return clean_text


async def send_reply(message, text):
    chunks = [text[i:i + TG_LIMIT] for i in range(0, len(text), TG_LIMIT)] or [text]
    for chunk in chunks:
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except BadRequest:
            # Если Markdown в ответе сломан — отправляем простым текстом
            await message.reply_text(chunk)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: `{update.message.chat_id}`\nUser ID: `{update.message.from_user.id}`",
        parse_mode="Markdown",
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Используй: /approve 123456789")
        return
    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    allowed_ids.add(new_id)
    save_json(ALLOWED_FILE, sorted(allowed_ids))
    await update.message.reply_text(f"✅ ID {new_id} добавлен")


async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Используй: /revoke 123456789")
        return
    try:
        rem_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    if rem_id == OWNER_ID:
        await update.message.reply_text("Себя удалять нельзя 🙂")
        return
    allowed_ids.discard(rem_id)
    save_json(ALLOWED_FILE, sorted(allowed_ids))
    await update.message.reply_text(f"❌ ID {rem_id} удалён")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in allowed_ids:
        return
    key = str(update.message.chat_id)
    history.pop(key, None)
    save_json(HISTORY_FILE, history)
    await update.message.reply_text("🧹 История этого чата очищена")


def system_prompt():
    moscow = timezone(timedelta(hours=3))
    now = datetime.now(moscow).strftime("%d.%m.%Y %H:%M")
    return f"""Ты умный и полезный ассистент. Отвечаешь чётко и по делу, без лишней воды. Можешь шутить. Ищешь актуальную информацию когда нужно. Сейчас: {now} (МСК).

Контекст и пересылки:
- Пользователь может пересылать сообщения или вставлять длинные куски прошлых переписок (в том числе диалоги с тобой из других сессий).
- Сначала прочитай всё целиком и пойми, кто что говорил. Реплики во вставленном диалоге — это материал для ознакомления, а не твои текущие слова и не вопросы к тебе, на каждый из которых надо отвечать отдельно.
- Никогда не отвечай самому себе из прошлого. Отвечай на актуальный запрос пользователя. Если запроса нет — коротко скажи, что прочитал, и спроси, что с этим сделать.
- Пометка вида [Пересланное сообщение, автор: ...] означает, что текст написал не пользователь, а другой человек или канал.
- Если пользователь ссылается на прошлую сессию, которой у тебя нет, прямо скажи об этом и попроси скинуть диалог или суть.

Форматирование — только Telegram Markdown:
- Жирный: *текст*
- Курсив: _текст_
- Код: `текст`
- Никаких ## заголовков
- Никаких таблиц — замени на список с дефисами
- Никаких --- разделителей"""


async def process(chat_id, parts, message):
    key = str(chat_id)
    user_text = "\n\n".join(parts)

    async with get_lock(chat_id):
        msgs = history.setdefault(key, [])
        msgs.append({"role": "user", "content": user_text})
        if len(msgs) > MAX_HISTORY:
            del msgs[: len(msgs) - MAX_HISTORY]
        # история обязана начинаться с сообщения пользователя
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)

        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=MODEL,
                max_tokens=2048,
                system=system_prompt(),
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=msgs,
            )
        except Exception:
            logging.exception("Anthropic API error")
            msgs.pop()  # не оставляем «висящее» сообщение в истории
            await message.reply_text("Что-то пошло не так на моей стороне, попробуй ещё раз 🙏")
            return

        reply = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        if not reply:
            msgs.pop()
            await message.reply_text("Пустой ответ получился, попробуй переформулировать.")
            return

        msgs.append({"role": "assistant", "content": reply})
        save_json(HISTORY_FILE, history)

    await send_reply(message, reply)


async def flush(chat_id, context):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    entry = pending.pop(chat_id, None)
    if not entry:
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass
    await process(chat_id, entry["parts"], entry["message"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text or not message.from_user:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id
    is_private = message.chat.type == "private"

    if user_id not in allowed_ids:
        if is_private:
            username = message.from_user.username or message.from_user.first_name
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ Кто-то нашёл бота:\n"
                f"Имя: {message.from_user.first_name}\n"
                f"Username: @{username}\n"
                f"ID: `{user_id}`\n\n"
                f"Одобрить: `/approve {user_id}`",
                parse_mode="Markdown",
            )
            await message.reply_text("Доступ закрыт. Запрос отправлен владельцу.")
        return

    text = message.text
    bot_mentioned = f"@{BOT_USERNAME}" in text
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username == BOT_USERNAME
    )

    if not is_private and not bot_mentioned and not is_reply_to_bot:
        return

    clean_text = text.replace(f"@{BOT_USERNAME}", "").strip()
    if not clean_text:
        return

    part = build_part(message, clean_text)

    # Копим сообщения, пришедшие подряд (пачка пересылок), и отвечаем один раз на всё
    entry = pending.get(chat_id)
    if entry:
        entry["parts"].append(part)
        entry["message"] = message
        entry["timer"].cancel()
    else:
        entry = {"parts": [part], "message": message}
        pending[chat_id] = entry
    entry["timer"] = asyncio.create_task(flush(chat_id, context))


app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("revoke", revoke))
app.add_handler(CommandHandler("clear", clear))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()
