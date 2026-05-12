import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_USERNAME = os.environ["BOT_USERNAME"]  # например: framily_claude_bot

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    bot_mentioned = f"@{BOT_USERNAME}" in text
    is_reply_to_bot = (
        message.reply_to_message and
        message.reply_to_message.from_user and
        message.reply_to_message.from_user.username == BOT_USERNAME
    )

    # Реагируем только на упоминание или ответ боту
    if not bot_mentioned and not is_reply_to_bot:
        return

    # Убираем упоминание из текста
    clean_text = text.replace(f"@{BOT_USERNAME}", "").strip()
    if not clean_text:
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system="Ты полезный ассистент в групповом чате друзей. Отвечай коротко и по делу. Можешь шутить. Можно матом, можно жёстко, но умеренно.",
        messages=[{"role": "user", "content": clean_text}]
    )

    await message.reply_text(response.content[0].text)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message))
app.run_polling()
