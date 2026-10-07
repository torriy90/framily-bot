import os
import json
import base64
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

MODEL = "claude-sonnet-5-5"
# Модель отвечает без «размышлений вперёд» (thinking: between_tools), так быстрее и дешевле.
# EFFORT — сколько усилий она вкладывает в ответ: low = быстро и дёшево,
# medium = баланс, high = вдумчивее и дороже. Меняется переменной EFFORT на Railway.
# (с between_tools допустимы только low / medium / high)
EFFORT = os.environ.get("EFFORT", "low").lower()
if EFFORT not in ("low", "medium", "high"):
    EFFORT = "low"

MAX_HISTORY = 40        # сколько последних сообщений помним на каждый чат
DEBOUNCE_SECONDS = 3.0  # ждём столько после последнего сообщения (для пачек пересылок)
TG_LIMIT = 4000
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 7 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

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

pending = {}  # chat_id -> {"parts": [...], "images": [...], "message": Message, "timer": Task}
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


async def download_image(message):
    """Возвращает (bytes, media_type), "too_big", "unsupported" или None, если картинки нет."""
    if message.photo:
        tg_file = await message.photo[-1].get_file()
        media_type = "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        media_type = message.document.mime_type
        if media_type not in IMAGE_TYPES:
            return "unsupported"
        if message.document.file_size and message.document.file_size > MAX_IMAGE_BYTES:
            return "too_big"
        tg_file = await message.document.get_file()
    else:
        return None
    data = bytes(await tg_file.download_as_bytearray())
    if len(data) > MAX_IMAGE_BYTES:
        return "too_big"
    return data, media_type


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

Изображения:
- Пользователь может присылать скриншоты и фото. Читай текст на них, описывай, что видно, и отвечай на подпись или вопрос к картинке.
- Если подписи нет, коротко опиши, что на изображении, а если это скриншот с текстом — перескажи его суть и спроси, что с ним сделать.
- Картинки из прошлых сообщений ты уже не видишь, помнишь только пометку, что они были. Если нужно вернуться к картинке, попроси прислать её снова.

Форматирование — только Telegram Markdown:
- Жирный: *текст*
- Курсив: _текст_
- Код: `текст`
- Никаких ## заголовков
- Никаких таблиц — замени на список с дефисами
- Никаких --- разделителей"""


def call_api(api_msgs):
    kwargs = dict(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt(),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=api_msgs,
    )
    try:
        return client.messages.create(
            **kwargs,
            extra_body={
                "thinking": {"type": "between_tools"},
                "output_config": {"effort": EFFORT},
            },
        )
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise  # дело не в параметрах, повтор не поможет
        # Если API не принял настройки thinking/effort — пробуем без них (будет медленнее, но ответит)
        logging.warning("Запрос с thinking/effort отклонён, повторяю без них: %s", e)
        return client.messages.create(**kwargs)


OWNER_ALERT_COOLDOWN = 3600  # не чаще раза в час, чтобы не заспамить владельца
last_owner_alert = {}


def classify_api_error(exc):
    """Возвращает (код проблемы, текст пользователю, текст владельцу или None)."""
    text = str(exc).lower()
    if "credit balance" in text:
        return (
            "billing",
            "Бот временно недоступен: на счёте закончились деньги. Хозяину уже сообщил 🙏",
            "⚠️ На балансе Anthropic закончились деньги, бот не отвечает. "
            "Пополни: console.anthropic.com → Plans & Billing. После пополнения бот заработает сам.",
        )
    if isinstance(exc, anthropic.AuthenticationError) or isinstance(exc, anthropic.PermissionDeniedError):
        return (
            "auth",
            "Бот временно недоступен: проблема с доступом к API. Хозяину уже сообщил 🙏",
            "⚠️ Anthropic отклонил ключ API (ошибка доступа). Проверь ANTHROPIC_API_KEY в Railway "
            "и состояние аккаунта в Console.",
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ("rate", "Слишком много запросов сразу, подожди минутку и повтори 🙏", None)
    return ("other", "Что-то пошло не так на моей стороне, попробуй ещё раз 🙏", None)


async def alert_owner(bot, code, text):
    now = datetime.now().timestamp()
    if now - last_owner_alert.get(code, 0) < OWNER_ALERT_COOLDOWN:
        return
    last_owner_alert[code] = now
    try:
        await bot.send_message(OWNER_ID, text)
    except Exception:
        logging.exception("Не удалось отправить уведомление владельцу")


async def process(chat_id, parts, images, message):
    key = str(chat_id)
    user_text = "\n\n".join(parts).strip()
    if not user_text and images:
        user_text = "Пользователь прислал изображение без подписи."
    # В историю пишем только текст: сами картинки не храним
    stored_text = user_text
    if images:
        stored_text += f"\n[приложено изображений: {len(images)}]"

    async with get_lock(chat_id):
        msgs = history.setdefault(key, [])
        msgs.append({"role": "user", "content": stored_text})
        if len(msgs) > MAX_HISTORY:
            del msgs[: len(msgs) - MAX_HISTORY]
        # история обязана начинаться с сообщения пользователя
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)

        api_msgs = list(msgs)
        if images:
            blocks = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
                for data, media_type in images
            ]
            blocks.append({"type": "text", "text": user_text})
            api_msgs[-1] = {"role": "user", "content": blocks}

        try:
            response = await asyncio.to_thread(call_api, api_msgs)
        except Exception as exc:
            logging.exception("Anthropic API error")
            msgs.pop()  # не оставляем «висящее» сообщение в истории
            code, user_text_err, owner_text = classify_api_error(exc)
            await message.reply_text(user_text_err)
            if owner_text:
                await alert_owner(message.get_bot(), code, owner_text)
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
    await process(chat_id, entry["parts"], entry["images"], entry["message"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
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

    # Текст сообщения или подпись к картинке
    text = message.text or message.caption or ""
    bot_mentioned = f"@{BOT_USERNAME}" in text
    is_reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.username == BOT_USERNAME
    )

    if not is_private and not bot_mentioned and not is_reply_to_bot:
        return

    clean_text = text.replace(f"@{BOT_USERNAME}", "").strip()

    image = None
    if message.photo or message.document:
        result = await download_image(message)
        if result == "too_big":
            await message.reply_text("Картинка слишком большая, пришли поменьше (до 7 МБ).")
            return
        if result == "unsupported":
            await message.reply_text("Этот формат картинки я не читаю, пришли JPG, PNG, WEBP или GIF.")
            return
        image = result

    if not clean_text and not image:
        return

    part = build_part(message, clean_text) if clean_text else None

    # Копим сообщения, пришедшие подряд (пачка пересылок, альбом), и отвечаем один раз на всё
    entry = pending.get(chat_id)
    if entry:
        entry["message"] = message
        entry["timer"].cancel()
    else:
        entry = {"parts": [], "images": [], "message": message}
        pending[chat_id] = entry
    if part:
        entry["parts"].append(part)
    if image and len(entry["images"]) < MAX_IMAGES:
        entry["images"].append(image)
    entry["timer"] = asyncio.create_task(flush(chat_id, context))


app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("revoke", revoke))
app.add_handler(CommandHandler("clear", clear))
app.add_handler(
    MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
        handle_message,
    )
)
app.run_polling()
