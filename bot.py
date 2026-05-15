import os
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
OWNER_ID = int(os.environ["OWNER_ID"])

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

history = defaultdict(list)
MAX_HISTORY = 20
allowed_ids = set()
allowed_ids.add(OWNER_ID)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: `{update.message.chat_id}`\nUser ID: `{update.message.from_user.id}`",
        parse_mode="Markdown"
    )

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Используй: /approve 123456789")
        return
    new_id = int(context.args[0])
    allowed_ids.add(new_id)
    await update.message.reply_text(f"✅ ID {new_id} добавлен")

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Используй: /revoke 123456789")
        return
    rem_id = int(context.args[0])
    allowed_ids.discard(rem_id)
    await update.message.reply_text(f"❌ ID {rem_id} удалён")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    history[update.message.chat_id].clear()
    await update.message.reply_text("🧹 История очищена")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
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
                parse_mode="Markdown"
            )
            await message.reply_text("Доступ закрыт. Запрос отправлен владельцу.")
        return

    text = message.text
    bot_mentioned = f"@{BOT_USERNAME}" in text
    is_reply_to_bot = (
        message.reply_to_message and
        message.reply_to_message.from_user and
        message.reply_to_message.from_user.username == BOT_USERNAME
    )

    if not is_private and not bot_mentioned and not is_reply_to_bot:
        return

    clean_text = text.replace(f"@{BOT_USERNAME}", "").strip()
    if not clean_text:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    moscow = timezone(timedelta(hours=3))
    now = datetime.now(moscow).strftime("%d.%m.%Y %H:%M")

    history[chat_id].append({"role": "user", "content": clean_text})
    if len(history[chat_id]) > MAX_HISTORY:
        history[chat_id] = history[chat_id][-MAX_HISTORY:]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=f"""Ты умный и полезный ассистент. Отвечаешь чётко и по делу, без лишней воды. Можешь шутить. Ищешь актуальную информацию когда нужно. Сейчас: {now} (МСК).

Форматирование — только Telegram Markdown:
- Жирный: *текст*
- Курсив: _текст_
- Код: `текст`
- Никаких ## заголовков
- Никаких таблиц — замени на список с дефисами
- Никаких --- разделителей""",
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=history[chat_id]
    )

    reply = ""
    for block in response.content:
        if hasattr(block, "text"):
            reply += block.text

    if reply:
        history[chat_id].append({"role": "assistant", "content": reply})
        await message.reply_text(reply, parse_mode="Markdown")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("revoke", revoke))
app.add_handler(CommandHandler("clear", clear))
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()
