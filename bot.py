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
import re
import anthropic
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
NADIA_CHAT_ID    = int(os.environ["NADIA_CHAT_ID"])
NJUPPA_CHAT_ID   = int(os.environ["NJUPPA_CHAT_ID"])
NJUPPA_THREAD_ID = int(os.environ.get("NJUPPA_THREAD_ID", "0")) or None
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
PROMPT_FILE      = "prompt.txt"
ADMIN_ID         = NADIA_CHAT_ID  # Админ = Надя
AUTH_FILE        = "authorized_users.txt"
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
- Tartin — 330
- Hleb sa semenkama — 380
- Raženi — 320
- Cimet — 330
- Sir — 300
- Mak — 300
- Brioš karamel — 300
- Brioš zemička — 280
- Babka čoko — 330
- Babka sa karamelom — 310
- Brioš hleb — 450

Скидка: 60% (платится 40%)

⚠️ ЖЁСТКИЕ ПРАВИЛА

1. Остатки — СВЯТОЕ
- Нельзя использовать больше, чем есть
- Нужно использовать максимум остатков
- В конце ОБЯЗАТЕЛЬНО показать остатки

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
ВАЖНО: используй ТОЛЬКО обычный текст и эмодзи. БЕЗ markdown, БЕЗ звёздочек, БЕЗ решёток, БЕЗ HTML тегов.

Формат СТРОГО такой:

🌸 НЮППА НА [ДАТА] 🌸

<b>#1 Название корзины</b>

✨ Короткое описание

- Позиция
- Позиция ×2

Ukupno: XXXX RSD → XXXX RSD (60%)
<b>Količina: X korpe</b>

_____

<b>#2 Название корзины</b>

и т.д.

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


def strip_html(text: str) -> str:
    """Убирает HTML теги из текста"""
    return re.sub(r'<[^>]+>', '', text)


def clean_markdown(text: str) -> str:
    """Убирает Markdown форматирование для читаемого вывода в Telegram"""
    # Убираем ** жирный **
    text = re.sub(r'[*][*](.+?)[*][*]', r'\1', text)
    # Убираем * курсив *
    text = re.sub(r'[*](.+?)[*]', r'\1', text)
    # Убираем ``` код ```
    text = re.sub(r'`{3}[\w]*', '', text)
    # Заменяем ### заголовки
    text = re.sub(r'^#{3} (.+)$', r'👉 \1', text, flags=re.MULTILINE)
    # Заменяем ## заголовки
    text = re.sub(r'^## (.+)$', r'\n📌 \1\n', text, flags=re.MULTILINE)
    # Заменяем # заголовки
    text = re.sub(r'^# (.+)$', r'\n📊 \1\n', text, flags=re.MULTILINE)
    # Заменяем --- на разделитель
    text = re.sub(r'^---+$', '─────────────', text, flags=re.MULTILINE)
    # Убираем строки таблиц markdown с |---|
    text = re.sub(r'^[|][-| ]+[|]$', '', text, flags=re.MULTILINE)
    # Чистим лишние пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_inventory(report_text: str) -> dict:
    """Парсит остатки из отчёта бариста"""
    import re
    inventory = {}
    # Маппинг названий из отчёта на стандартные
    name_map = {
        'cimet': 'Cimet', 'sir': 'Sir',
        'brioš karamel': 'Brioš karamel', 'brioš karame': 'Brioš karamel',
        'brioš zemička': 'Brioš zemička', 'brioš zemica': 'Brioš zemička',
        'babka čoko': 'Babka čoko', 'babka coko': 'Babka čoko',
        'babka sa karamelom': 'Babka sa karamelom',
        'mak': 'Mak', 'tartin': 'Tartin',
        'hleb sa semenkama': 'Hleb sa semenkama',
        'brios (hleb)': 'Brios (Hleb)', 'brios hleb': 'Brios (Hleb)',
        'raženi': 'Raženi', 'razeni': 'Raženi',
        'fokača': 'Fokača', 'fokaca': 'Fokača',
        'banana': 'Banana', 'čokokeks': 'Čokokeks', 'cokokeks': 'Čokokeks',
    }
    lines = report_text.lower().split('\n')
    for line in lines:
        line = line.strip().rstrip(',')
        for key, standard in name_map.items():
            pattern = rf'{re.escape(key)}[:\s]+([\d]+)'
            m = re.search(pattern, line)
            if m:
                inventory[standard] = int(m.group(1))
                break
    return inventory


def check_inventory_in_baskets(baskets_text: str, inventory: dict) -> list:
    """Проверяет не превышены ли остатки в корзинах. Возвращает список ошибок."""
    import re
    errors = []
    used = {}

    name_map = {
        'cimet': 'Cimet', 'sir': 'Sir',
        'brioš karamel': 'Brioš karamel',
        'brioš zemička': 'Brioš zemička',
        'babka čoko': 'Babka čoko',
        'babka sa karamelom': 'Babka sa karamelom',
        'mak': 'Mak', 'tartin': 'Tartin',
        'hleb sa semenkama': 'Hleb sa semenkama',
        'brios (hleb)': 'Brios (Hleb)', 'brios hleb': 'Brios (Hleb)',
        'raženi': 'Raženi', 'razeni': 'Raženi',
        'fokača': 'Fokača', 'fokaca': 'Fokača',
        'banana': 'Banana', 'čokokeks': 'Čokokeks',
    }

    lines = baskets_text.lower().split('\n')
    for line in lines:
        line = line.strip()
        for key, standard in name_map.items():
            # Ищем "Название ×N" или "Название x N"
            pattern_mult = rf'{re.escape(key)}[^\n]*[×x](\d+)'
            m = re.search(pattern_mult, line)
            if m:
                used[standard] = used.get(standard, 0) + int(m.group(1))
                break
            # Ищем просто "Название" без множителя = 1 штука
            pattern_single = rf'^[•\-]\s*{re.escape(key)}(?:\s|$)'
            if re.search(pattern_single, line):
                used[standard] = used.get(standard, 0) + 1
                break

    for product, count in used.items():
        available = inventory.get(product, 0)
        if count > available:
            errors.append(f"❌ {product}: использовано {count}, а в остатке только {available}")

    return errors


def save_sales_data(date_str: str, baskets_text: str, total: int, sold: int, unsold_numbers: str, notes: str):
    logger.info(f"Saving sales data: {date_str}, {sold}/{total}")
    sheet = get_sheet()
    logger.info("Got sheet successfully")
    try:
        date = datetime.strptime(f"{date_str}.{datetime.now().year}", "%d.%m.%Y")
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
        strip_html(baskets_text)[:500],
        notes
    ])
    logger.info("Row appended successfully!")


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

def load_authorized() -> set:
    """Загружает список авторизованных пользователей"""
    if not os.path.exists(AUTH_FILE):
        # Админ всегда авторизован
        return {ADMIN_ID}
    with open(AUTH_FILE, "r") as f:
        ids = set()
        for line in f.read().splitlines():
            if line.strip():
                try:
                    ids.add(int(line.strip()))
                except ValueError:
                    pass
        ids.add(ADMIN_ID)
        return ids


def save_authorized(user_ids: set):
    """Сохраняет список авторизованных пользователей"""
    with open(AUTH_FILE, "w") as f:
        f.write("\n".join(str(uid) for uid in user_ids))


def is_authorized(user_id: int) -> bool:
    return user_id in load_authorized()


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
        messages=[{"role": "user", "content": (
            f"{prompt}\n\nОстатки из отчёта:\n\n{report_text}\n\n"
            f"Составь корзины. ВАЖНО по математике:\n"
            f"- Считай точно: если Tartin 10 и ты кладёшь по 1 в 6 корзин = использовано 6, остаток 4\n"
            f"- Перед финальными остатками пересчитай каждую позицию вручную\n"
            f"- Никогда не пиши что позиция использована если остаток > 0"
        )}]
    )
    return response.content[0].text


def fix_baskets(current_baskets: str, fix_request: str) -> str:
    prompt = load_prompt()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": (
            f"{prompt}\n\n"
            f"Вот уже готовые корзины которые нужно ИСПРАВИТЬ (не пересобирать заново!):\n\n{current_baskets}\n\n"
            f"Что нужно изменить: {fix_request}\n\n"
            f"ВАЖНО:\n"
            f"- Не пересобирай корзины заново с нуля\n"
            f"- Внеси только запрошенные изменения\n"
            f"- Сохрани все остальные корзины без изменений\n"
            f"- Пересчитай остатки точно: вычти из каждой позиции столько штук сколько использовано во ВСЕХ корзинах\n"
            f"- Верни полный текст всех корзин с исправлениями"
        )}]
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

Сделай анализ по этим пунктам:
1. Какие корзины продаются лучше/хуже
2. Зависимость от дня недели
3. Средний процент продаж
4. Конкретные рекомендации

ВАЖНО по оформлению:
- Используй ТОЛЬКО простой текст и эмодзи
- БЕЗ markdown: никаких **, ##, --, [], ||
- Разделяй блоки линией из символов: ─────────────
- Заголовки блоков через эмодзи: 📊 Продажи по корзинам
- Списки через эмодзи: ✅ хорошо, ❌ плохо, 👉 рекомендация
- Цифры и факты выделяй caps: ТАРТИНЫ — ХИТ
- Тон: простой, конкретный, дружелюбный"""
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
    return clean_markdown(response.content[0].text)


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
    baskets_clean = strip_html(baskets)
    preview = f"📋 Предпросмотр корзин:\n\n{baskets_clean}"
    if len(preview) > 4000:
        preview = preview[:4000] + "\n\n(обрезано для предпросмотра)"
    await context.bot.send_message(
        chat_id=NADIA_CHAT_ID,
        text=preview,
        reply_markup=get_preview_keyboard()
    )


# ── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def check_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет авторизацию и отправляет запрос если нет доступа. Возвращает True если авторизован."""
    message = update.effective_message
    user_id = message.chat.id
    logger.info(f"check_auth: user_id={user_id}, authorized={is_authorized(user_id)}, all={load_authorized()}")
    user_name = message.chat.first_name or ""
    user_username = message.chat.username or ""

    if is_authorized(user_id):
        return True

    pending = context.bot_data.get("pending_auth", set())
    if user_id not in pending:
        pending.add(user_id)
        context.bot_data["pending_auth"] = pending

        await message.reply_text(
            "👋 Привет! Твой запрос на доступ отправлен администратору.\n"
            "Ожидай подтверждения."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Разрешить", callback_data=f"auth_approve_{user_id}"),
                InlineKeyboardButton("❌ Отказать", callback_data=f"auth_deny_{user_id}"),
            ]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔐 Запрос на доступ:\n\n"
                 f"👤 Имя: {user_name}\n"
                 f"🔗 Username: @{user_username}\n"
                 f"🆔 ID: {user_id}\n\n"
                 f"Разрешить доступ к боту?",
            reply_markup=keyboard
        )
    else:
        await message.reply_text("⏳ Твой запрос уже отправлен. Ожидай подтверждения.")
    return False


async def cmd_getprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context):
        return
    
    prompt = load_prompt()
    if len(prompt) > 4000:
        prompt = prompt[:4000] + "\n\n(обрезано)"
    await update.message.reply_text(f"📄 Текущий промт:\n\n{prompt}\n\nЧтобы изменить — напиши /setprompt")


async def cmd_setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context):
        return
    
    context.user_data["state"] = WAITING_FOR_NEW_PROMPT
    await update.message.reply_text(
        "📝 Отправь новый промт следующим сообщением.\n\n"
        "Можешь скопировать текущий через /getprompt, отредактировать и прислать.\n\n"
        "Для отмены — /cancel"
    )


async def cmd_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Записать данные о продажах за день"""
    if not await check_auth(update, context):
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
    if not await check_auth(update, context):
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
    if not await check_auth(update, context):
        return
    
    context.user_data["state"] = WAITING_FOR_REBUILD
    await update.message.reply_text(
        "🔄 Что продали внеурочно? Напиши в формате:\n\n"
        "Cimet 2, Mak 1\n\n"
        "Бот пересоберёт корзины с учётом новых остатков.\n\n"
        "Для отмены — /cancel"
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context):
        return
    
    context.user_data["state"] = None
    await update.message.reply_text("🚫 Отменено.")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список авторизованных пользователей"""
    if update.effective_chat.id != ADMIN_ID:
        return
    authorized = load_authorized()
    text = f"👥 Авторизованных пользователей: {len(authorized)}\n\n"
    for uid in authorized:
        text += f"• {uid}\n"
    await update.message.reply_text(text)


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить пользователя вручную: /adduser 123456789"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /adduser 123456789")
        return
    try:
        uid = int(args[0])
        authorized = load_authorized()
        authorized.add(uid)
        save_authorized(authorized)
        await update.message.reply_text(f"✅ Пользователь {uid} добавлен!")
        try:
            await context.bot.send_message(chat_id=uid, text="✅ Тебе разрешён доступ к боту! Можешь пересылать отчёты.\n\n/help — список команд")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отозвать доступ: /revoke 123456789"""
    if update.effective_chat.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /revoke 123456789")
        return
    try:
        uid = int(args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Нельзя отозвать доступ у администратора.")
            return
        authorized = load_authorized()
        authorized.discard(uid)
        save_authorized(authorized)
        await update.message.reply_text(f"✅ Доступ пользователя {uid} отозван.")
        try:
            await context.bot.send_message(chat_id=uid, text="⚠️ Твой доступ к боту был отозван.")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Неверный ID пользователя.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие"""
    user_id = update.effective_chat.id
    logger.info(f"START from user_id={user_id}, authorized={is_authorized(user_id)}")
    if not await check_auth(update, context):
        return
    await update.message.reply_text(
        "👋 Привет! Я NjuppaHelperBot.\n\n"
        "Перешли мне отчёт с остатками — составлю корзины.\n\n"
        "/help — список всех команд"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await update.message.reply_text(
        "🤖 Команды бота:\n\n"
        "Перешли отчёт с остатками — бот составит корзины\n\n"
        "/rebuild — экстренная пересборка если продали внеурочно\n"
        "/sales — записать данные о продажах за день\n"
        "/analytics — аналитика и рекомендации по продажам\n\n"
        "/getprompt — посмотреть текущий промт\n"
        "/setprompt — изменить промт\n"
        "/cancel — отменить текущее действие\n"
        "/help — эта справка\n\n"
        "👑 Только для администратора:\n"
        "/users — список авторизованных\n"
        "/revoke [ID] — отозвать доступ"
    )


# ── ОСНОВНОЙ ОБРАБОТЧИК ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    # Только авторизованные пользователи в личке
    if message.chat.type != "private":
        return

    if not await check_auth(update, context):
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
            try:
                await asyncio.to_thread(save_sales_data, date_str, baskets, total, sold, unsold, notes)
                logger.info("Sales data saved OK")
            except Exception as sheet_err:
                logger.error(f"Sheet error: {sheet_err}", exc_info=True)
                await message.reply_text(f"⚠️ Данные приняты, но ошибка записи в таблицу: {sheet_err}")
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
        if not current_baskets:
            await message.reply_text("❌ Не нашла корзины для исправления. Перешли отчёт заново.")
            context.user_data["state"] = None
            return
        status_msg = await message.reply_text("⏳ Исправляю, подожди секунду...")
        try:
            new_baskets = await asyncio.wait_for(
                asyncio.to_thread(fix_baskets, current_baskets, fix_request),
                timeout=120
            )
        except asyncio.TimeoutError:
            await status_msg.edit_text("❌ Превышено время ожидания. Попробуй ещё раз.")
            context.user_data["state"] = None
            return
        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
            context.user_data["state"] = None
            return
        context.bot_data[f"baskets_{NADIA_CHAT_ID}"] = new_baskets
        context.user_data["state"] = None
        await status_msg.delete()
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

    # Проверяем остатки
    inventory = parse_inventory(report)
    if inventory:
        errors = check_inventory_in_baskets(baskets, inventory)
        if errors:
            error_text = "\n".join(errors)
            baskets += f"\n\n⚠️ Обнаружены ошибки в остатках:\n{error_text}\n\nРекомендую нажать ✏️ Исправить."

    context.bot_data[f"baskets_{NADIA_CHAT_ID}"] = baskets
    context.bot_data[f"inventory_{NADIA_CHAT_ID}"] = inventory
    await send_preview(context, baskets)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    baskets = context.bot_data.get(f"baskets_{NADIA_CHAT_ID}")

    if query.data.startswith("auth_approve_"):
        requesting_user_id = int(query.data.split("_")[-1])
        authorized = load_authorized()
        authorized.add(requesting_user_id)
        save_authorized(authorized)

        # Убираем из pending
        pending = context.bot_data.get("pending_auth", set())
        pending.discard(requesting_user_id)
        context.bot_data["pending_auth"] = pending

        await query.edit_message_text(f"✅ Пользователь {requesting_user_id} авторизован!")
        await context.bot.send_message(
            chat_id=requesting_user_id,
            text="✅ Тебе разрешён доступ к боту! Можешь пересылать отчёты с остатками.\n\n/help — список команд"
        )
        return

    elif query.data.startswith("auth_deny_"):
        requesting_user_id = int(query.data.split("_")[-1])

        # Убираем из pending
        pending = context.bot_data.get("pending_auth", set())
        pending.discard(requesting_user_id)
        context.bot_data["pending_auth"] = pending

        await query.edit_message_text(f"❌ Пользователь {requesting_user_id} отклонён.")
        await context.bot.send_message(
            chat_id=requesting_user_id,
            text="❌ Твой запрос на доступ отклонён."
        )
        return

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
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Напиши что нужно исправить:\n\n"
                 "Например: «убери одну корзину с тартином»"
        )
        return

    elif query.data == "cancel":
        context.bot_data.pop(f"baskets_{NADIA_CHAT_ID}", None)
        context.user_data["state"] = None
        await query.edit_message_text("🚫 Отменено.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("getprompt", cmd_getprompt))
    app.add_handler(CommandHandler("setprompt", cmd_setprompt))
    app.add_handler(CommandHandler("rebuild", cmd_rebuild))
    app.add_handler(CommandHandler("sales", cmd_sales))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
