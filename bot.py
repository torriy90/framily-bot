import os
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_USERNAME = os.environ["BOT_USERNAME"]
OWNER_ID = int(os.environ["OWNER_ID"])

# Белый список — заполним после получения ID
ALLOWED_IDS = set()
ALLOWED_IDS.add(OWNER_ID)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
    ALLOWED_IDS.add(new_id)
    await update.message.reply_text(f"✅ ID {new_id} добавлен в белый список")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id
    is_private = message.chat.type == "private"

    # Проверка белого списка
    if user_id not in ALLOWED_IDS and chat_id not in ALLOWED_IDS:
        if is_private:
            username = message.from_user.username or message.from_user.first_name
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ Незнакомец хочет доступ к боту:\n"
                f"Имя: {message.from_user.first_name}\n"
                f"Username: @{username}\n"
                f"ID: `{user_id}`\n\n"
                f"Чтобы разрешить: `/approve {user_id}`",
                parse_mode="Markdown"
            )
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

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=f"""Ты свой пацан в групповом чате друзей. Говоришь просто, без официоза.
Шутишь, троллишь по-доброму, споришь если не согласен.
Ненавидишь длинные ответы и занудство. Называешь вещи своими именами.
Когда нужно — ищешь актуальную инфу в интернете.
Сейчас: {now} (МСК).""",
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": clean_text}]
    )

    reply = ""
    for block in response.content:
        if hasattr(block, "text"):
            reply += block.text

    if reply:
        await message.reply_text(reply)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("myid", myid))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()
