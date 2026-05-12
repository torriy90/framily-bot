import os
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_USERNAME = os.environ["BOT_USERNAME"]

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    is_private = message.chat.type == "private"
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

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

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

    # Собираем текстовый ответ
    reply = ""
    for block in response.content:
        if hasattr(block, "text"):
            reply += block.text

    if reply:
        await message.reply_text(reply)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()
