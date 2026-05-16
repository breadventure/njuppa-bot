import os
import json
import asyncio
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    CommandHandler, filters, ContextTypes
)
import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
NADIA_CHAT_ID    = int(os.environ["NADIA_CHAT_ID"])
NJUPPA_CHAT_ID   = int(os.environ["NJUPPA_CHAT_ID"])
NJUPPA_THREAD_ID = int(os.environ.get("NJUPPA_THREAD_ID", "0")) or None
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
PROMPT_FILE      = "prompt.txt"
# ─────────────────────────────────────────────────────────────────────────────

WAITING_FOR_FIX        = "waiting_for_fix"
WAITING_FOR_NEW_PROMPT = "waiting_for_new_prompt"
WAITING_FOR_SALES      = "waiting_for_sales"
WAITING_FOR_REBUILD    = "waiting_for_rebuild"

DEFAULT_PROMPT = """🔧 ПРОМТ ДЛЯ СОСТАВЛЕНИЯ КОРЗИН (NJUPPA)
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


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────

def get_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

    # Лист для данных
    try:
        sheet = spreadsheet.worksheet("Sales")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Sales", rows=1000, cols=10)
        sheet.append_row(["Дата", "День недели", "Корзин выставлено", "Корзин продано", "Не продано (номера)", "Состав корзин", "Примечания"])
    return sheet


def save_sales_data(date_str: str, baskets_text: str, total: int, sold: int, unsold_numbers: str, notes: str):
    def _save():
        sheet = get_sheet()
        try:
            date = datetime.strptime(date_str, "%d.%m")
            date = date.replace(year=datetime.now().year)
        except Exception:
            date = datetime.now()

        weekdays = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        weekday = weekdays[date.weekday()]

        sheet.append_row([
            date.strftime("%d.%m.%Y"),
            weekday,
            total,
            sold,
            unsold_numbers,
            baskets_text[:500],  # Состав корзин (обрезаем чтобы не переполнить)
            notes
        ])
    return _save


def get_analytics_data() -> str:
    sheet = get_sheet()
    records = sheet.get_all_records()
    if not records:
        return "Данных пока нет."
    return json.dumps(records, ensure_ascii=False, indent=2)


# ── ПРОМТ ─────────────────────────────────────────────────────────────────────

def load_prompt() -> str:
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return DEFAULT_PROMPT


def save_prompt(text: str):
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


# ── CLAUDE ────────────────────────────────────────────────────────────────────

def extract_report_text(message) -> str | None:
    text = message.text or message.caption or ""
    if "Količina po vrstama" in text or "Cimet:" in text or "Datum:" in text:
        return text
    return None


def generate_baskets(report_text: str) -> str:
    prompt = load_prompt()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": f"{prompt}\n\nОстатки из отчёта:\n\n{report_text}\n\nСоставь корзины."}]
    )
    return response.content[0].text


def fix_baskets(current_baskets: str, fix_request: str) -> str:
    prompt = load_prompt()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": f"{prompt}\n\nВот текущий вариант корзин:\n\n{current_baskets}\n\nНужно исправить: {fix_request}\n\nВерни полный обновлённый текст корзин."}]
    )
    return response.content[0].text


def analyze_sales(analytics_data: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""Ты аналитик пекарни. Проанализируй данные о продажах корзин Njuppa.

Данные:
{analytics_data}

Сделай анализ:
1. Какие корзины продаются лучше/хуже
2. Есть ли зависимость от дня недели
3. Средний процент продаж
4. Конкретные рекомендации по составу корзин на будущее

Тон: простой, конкретный, с цифрами."""
        }]
    )
    return response.content[0].text


def rebuild_baskets(current_baskets: str, sold_items: str) -> str:
    prompt = load_prompt()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": (
            f"{prompt}\n\n"
            f"Вот текущие корзины которые уже опубликованы:\n\n{current_baskets}\n\n"
            f"Внеурочно продали следующие позиции из остатков: {sold_items}\n\n"
            f"Пересобери корзины с учётом того что этих позиций теперь меньше. "
            f"Если корзина больше не может быть собрана — убери её. "
            f"Верни полный обновлённый текст корзин."
        )}]
    )
    return response.content[0].text


# ── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────────

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


# ── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def cmd_getprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    prompt = load_prompt()
    if len(prompt) > 4000:
        prompt = prompt[:4000] + "\n\n(обрезано)"
    await update.message.reply_text(f"📄 Текущий промт:\n\n{prompt}\n\nЧтобы изменить — напиши /setprompt")


async def cmd_setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    context.user_data["state"] = WAITING_FOR_NEW_PROMPT
    await update.message.reply_text(
        "📝 Отправь новый промт следующим сообщением.\n\n"
        "Можешь скопировать текущий через /getprompt, отредактировать и прислать.\n\n"
        "Для отмены — /cancel"
    )


async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Записать данные о продажах за день"""
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    context.user_data["state"] = WAITING_FOR_SALES
    await update.message.reply_text(
        "📊 Введи данные о продажах в формате:\n\n"
        "Продано: 6/7\n"
        "Не продано: #3\n"
        "Примечания: одну оплатили напрямую\n\n"
        "Для отмены — /cancel"
    )


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить аналитику продаж"""
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    await update.message.reply_text("⏳ Анализирую данные, подожди...")
    try:
        data = await asyncio.to_thread(get_analytics_data)
        if data == "Данных пока нет.":
            await update.message.reply_text("📊 Данных пока нет. Сначала добавь продажи через /sales")
            return
        analysis = await asyncio.to_thread(analyze_sales, data)
        if len(analysis) > 4000:
            analysis = analysis[:4000] + "\n\n(обрезано)"
        await update.message.reply_text(f"📊 Аналитика продаж:\n\n{analysis}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")



async def cmd_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экстренная пересборка корзин"""
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    context.user_data["state"] = WAITING_FOR_REBUILD
    await update.message.reply_text(
        "🔄 Что продали внеурочно? Напиши в формате:\n\n"
        "Cimet 2, Mak 1\n\n"
        "Бот пересоберёт корзины с учётом новых остатков.\n\n"
        "Для отмены — /cancel"
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    context.user_data["state"] = None
    await update.message.reply_text("🚫 Отменено.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != NADIA_CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 Команды бота:\n\n"
        "Перешли отчёт с остатками — бот составит корзины\n\n"
        "/rebuild — экстренная пересборка если продали внеурочно\n"
        "/sales — записать данные о продажах за день\n"
        "/analytics — аналитика и рекомендации по продажам\n\n"
        "/getprompt — посмотреть текущий промт\n"
        "/setprompt — изменить промт\n"
        "/cancel — отменить текущее действие\n"
        "/help — эта справка"
    )


# ── ОСНОВНОЙ ОБРАБОТЧИК ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if message.chat.type != "private" or message.chat.id != NADIA_CHAT_ID:
        return

    state = context.user_data.get("state")

    # Ждём новый промт
    if state == WAITING_FOR_NEW_PROMPT:
        new_prompt = message.text or ""
        if not new_prompt.strip():
            await message.reply_text("❌ Промт не может быть пустым.")
            return
        save_prompt(new_prompt)
        context.user_data["state"] = None
        await message.reply_text("✅ Промт сохранён!")
        return

    # Ждём данные о продажах
    if state == WAITING_FOR_SALES:
        text = message.text or ""
        lines = text.strip().split("\n")

        sold = total = 0
        unsold = ""
        notes = ""
        date_str = datetime.now().strftime("%d.%m")

        for line in lines:
            line = line.strip()
            if line.lower().startswith("продано:"):
                parts = line.split(":")[-1].strip()
                try:
                    sold, total = map(int, parts.split("/"))
                except Exception:
                    pass
            elif line.lower().startswith("не продано:"):
                unsold = line.split(":")[-1].strip()
            elif line.lower().startswith("примечания:"):
                notes = line.split(":")[-1].strip()
            elif line.lower().startswith("дата:"):
                date_str = line.split(":")[-1].strip()

        baskets = context.bot_data.get(f"baskets_{NADIA_CHAT_ID}", "нет данных")

        try:
            await asyncio.to_thread(save_sales_data(date_str, baskets, total, sold, unsold, notes))
            context.user_data["state"] = None
            await message.reply_text(
                f"✅ Данные сохранены!\n\n"
                f"📅 Дата: {date_str}\n"
                f"🧺 Продано: {sold}/{total}\n"
                f"❌ Не продано: {unsold or 'все продано'}\n"
                f"📝 Примечания: {notes or '—'}\n\n"
                f"Посмотреть аналитику: /analytics"
            )
        except Exception as e:
            await message.reply_text(f"❌ Ошибка сохранения: {e}")
        return


    # Ждём данные о внеурочной продаже
    if state == WAITING_FOR_REBUILD:
        sold_items = message.text or ""
        current_baskets = context.bot_data.get(f"baskets_{NADIA_CHAT_ID}", "")
        if not current_baskets:
            await message.reply_text("❌ Нет текущих корзин для пересборки. Сначала составь корзины.")
            context.user_data["state"] = None
            return
        await message.reply_text("⏳ Пересобираю корзины, подожди секунду...")
        try:
            new_baskets = await asyncio.to_thread(rebuild_baskets, current_baskets, sold_items)
        except Exception as e:
            await message.reply_text(f"❌ Ошибка: {e}")
            return
        context.bot_data[f"baskets_{NADIA_CHAT_ID}"] = new_baskets
        context.user_data["state"] = None
        await send_preview(context, new_baskets)
        return

    # Ждём исправление корзин
    if state == WAITING_FOR_FIX:
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
            "Перешли мне сообщение с остатками от бариста.\n\n"
            "Нужна помощь? /help"
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
            await query.edit_message_text(
                "✅ Опубликовано в Njuppa!\n\n"
                "Не забудь в конце дня записать продажи: /sales"
            )
            context.bot_data.pop(f"baskets_{NADIA_CHAT_ID}", None)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")

    elif query.data == "fix":
        context.user_data["state"] = WAITING_FOR_FIX
        await query.edit_message_text(
            "✏️ Напиши что нужно исправить:\n\n"
            "Например: «убери одну корзину с тартином»"
        )

    elif query.data == "cancel":
        context.bot_data.pop(f"baskets_{NADIA_CHAT_ID}", None)
        context.user_data["state"] = None
        await query.edit_message_text("🚫 Отменено.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("getprompt", cmd_getprompt))
    app.add_handler(CommandHandler("setprompt", cmd_setprompt))
    app.add_handler(CommandHandler("rebuild", cmd_rebuild))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
