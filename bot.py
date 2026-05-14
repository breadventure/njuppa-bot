import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
NADIA_CHAT_ID    = int(os.environ["NADIA_CHAT_ID"])
NJUPPA_CHAT_ID   = int(os.environ["NJUPPA_CHAT_ID"])
NJUPPA_THREAD_ID = int(os.environ.get("NJUPPA_THREAD_ID", "0")) or None
# ─────────────────────────────────────────────────────────────────────────────

WAITING_FOR_FIX = "waiting_for_fix"

NADYA_PROMPT = """🔧 ПРОМТ ДЛЯ СОСТАВЛЕНИЯ КОРЗИН (NJUPPA)
Задача:
Собрать корзины из остатков выпечки и хлеба с точным соблюдением остатков, корректным расчётом цен и логикой продаж.

📦 ВХОДНЫЕ ДАННЫЕ
Цены (фиксированные):
• Tartin — 330
• Hleb sa semenkama — 380
• Raženi — 320
• Cimet — 330
• Sir — 300
• Mak — 300
• Brioš karamel — 300
• Brioš zemička — 280
• Babka čoko — 330
• Babka sa karamelom — 310
• Brioš hleb — 450

Скидка: 60% (платится 40%)

⚠️ ЖЁСТКИЕ ПРАВИЛА

1. Остатки — СВЯТОЕ
• Нельзя использовать больше, чем есть
• Нужно использовать максимум остатков
• В конце ОБЯЗАТЕЛЬНО показать остатки

2. Минимальная цена корзины: 1000 RSD до скидки

3. НЕЛЬЗЯ:
❌ добавлять позиции, которых нет
❌ "додумывать" булки
❌ превышать остатки
❌ делать корзины <1000

4. ЛОГИКА СБОРКИ
Приоритет:
1. Сначала сложные/редкие позиции (семечки, ржаной)
2. Потом: корзины с хлебом, затем булки
3. Баланс: чередовать сладкие / сладко-солёные / хлебные

5. СТРУКТУРА КОРЗИНЫ
Формат:
🧺 Korpa #X — Название

✨ Короткое описание

• Позиция
• Позиция ×2

Ukupno: XXXX RSD → XXXX RSD (60%)
Količina: X korpe

6. ПРОВЕРКА В КОНЦЕ (ОБЯЗАТЕЛЬНО)
✔ проверку цен
✔ проверку остатков
✔ показать остатки после

Тон: простой, дружелюбный, без пафоса, честный.

Если не хватает позиций — СРАЗУ написать об этом, не придумывать.
"""


def extract_report_text(message) -> str | None:
    text = message.text or message.caption or ""
    if "Količina po vrstama" in text or "Cimet:" in text or "Datum:" in text:
        return text
    return None


def generate_baskets(report_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"{NADYA_PROMPT}\n\nОстатки из отчёта:\n\n{report_text}\n\nСоставь корзины."
            }
        ]
    )
    return response.content[0].text


def fix_baskets(current_baskets: str, fix_request: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{NADYA_PROMPT}\n\n"
                    f"Вот текущий вариант корзин:\n\n{current_baskets}\n\n"
                    f"Нужно исправить следующее: {fix_request}\n\n"
                    f"Верни полный обновлённый текст корзин."
                )
            }
        ]
    )
    return response.content[0].text


def get_preview_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать в Njuppa", callback_data="publish"),
            InlineKeyboardButton("✏️ Исправить", callback_data="fix"),
        ],
        [
            InlineKeyboardButton("❌ Отменить", callback_data="cancel"),
        ]
    ])


async def send_preview(context, baskets: str):
    preview = f"📋 Предпросмотр корзин:\n\n{baskets}"
    if len(preview) > 4000:
        preview = preview[:4000] + "\n\n(обрезано для предпросмотра)"

    await context.bot.send_message(
        chat_id=NADIA_CHAT_ID,
        text=preview,
        reply_markup=get_preview_keyboard()
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    # Отвечаем ТОЛЬКО на личные сообщения от Нади
    if message.chat.type != "private" or message.chat.id != NADIA_CHAT_ID:
        return


    # Если ждём правки — обрабатываем как инструкцию по исправлению
    if context.user_data.get("state") == WAITING_FOR_FIX:
        fix_request = message.text or ""
        current_baskets = context.bot_data.get(f"baskets_{NADIA_CHAT_ID}", "")

        await message.reply_text("⏳ Исправляю, подожди секунду...")

        try:
            new_baskets = await asyncio.to_thread(fix_baskets, current_baskets, fix_request)
        except Exception as e:
            await message.reply_text(f"❌ Ошибка: {e}")
            return

        context.bot_data[f"baskets_{NADIA_CHAT_ID}"] = new_baskets
        context.user_data["state"] = None
        await send_preview(context, new_baskets)
        return

    # Обычный режим — ищем отчёт
    report = extract_report_text(message)

    if not report:
        await message.reply_text(
            "Не похоже на отчёт по остаткам 🤔\n"
            "Перешли мне сообщение с остатками от бариста."
        )
        return

    await message.reply_text("⏳ Считаю корзины, подожди секунду...")

    try:
        baskets = await asyncio.to_thread(generate_baskets, report)
    except Exception as e:
        await message.reply_text(f"❌ Ошибка при генерации: {e}")
        return

    context.bot_data[f"baskets_{NADIA_CHAT_ID}"] = baskets
    await send_preview(context, baskets)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    baskets = context.bot_data.get(f"baskets_{NADIA_CHAT_ID}")

    if query.data == "publish":
        if not baskets:
            await query.edit_message_text("❌ Не нашла корзины для публикации.")
            return
        try:
            await context.bot.send_message(
                chat_id=NJUPPA_CHAT_ID,
                text=baskets,
                message_thread_id=NJUPPA_THREAD_ID
            )
            await query.edit_message_text("✅ Опубликовано в Njuppa!")
            context.bot_data.pop(f"baskets_{NADIA_CHAT_ID}", None)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")

    elif query.data == "fix":
        context.user_data["state"] = WAITING_FOR_FIX
        await query.edit_message_text(
            "✏️ Напиши что нужно исправить:\n\n"
            "Например: «убери одну корзину с тартином» или «добавь больше сладких»"
        )

    elif query.data == "cancel":
        context.bot_data.pop(f"baskets_{NADIA_CHAT_ID}", None)
        context.user_data["state"] = None
        await query.edit_message_text("🚫 Отменено. Корзины не опубликованы.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
