import os
import re
import json
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from openai import OpenAI

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
OPENAI_KEY       = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL     = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "medium")
ADMIN_CHAT_ID    = int(os.environ["NADIA_CHAT_ID"])      # кто утверждает
NJUPPA_CHAT_ID   = int(os.environ["NJUPPA_CHAT_ID"])
NJUPPA_THREAD_ID = int(os.environ.get("NJUPPA_THREAD_ID", "0")) or None

# Railway Volume, смонтированный на /data. Без него настройки слетят при рестарте.
DATA_DIR  = os.environ.get("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "njuppa_state.json")
# ─────────────────────────────────────────────────────────────────────────────

AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "180"))   # секунд на ответ модели

WAITING_FOR_FIX     = "waiting_for_fix"
WAITING_FOR_REBUILD = "waiting_for_rebuild"
WAITING_FOR_PROMPT  = "waiting_for_prompt"
PROMPT_PARTS        = "prompt_parts"

client = OpenAI(api_key=OPENAI_KEY)

DEFAULT_PROMPT = """Ты — ассистент BreadVenture для ежедневной сборки корзин NJUPPA.

По фактическим остаткам продукции:

1. посчитай остатки;
2. собери оптимальные корзины;
3. проверь количество и стоимость;
4. подготовь готовый текст для публикации.

Ничего не придумывай. Используй только переданные остатки и цены ниже.

────────────────────
1. ЦЕНЫ
────────────────────

Выпечка:
Cimet — 330 RSD
Sir — 300 RSD
Brioš karamel — 300 RSD
Brioš zemička — 280 RSD
Babka čoko — 330 RSD
Babka sa karamelom — 310 RSD
Mak — 300 RSD

Хлеб:
Tartin — 330 RSD
Rustični sa semenkama — 380 RSD
Brios hleb — 450 RSD
Raženi hleb — 320 RSD
Focaccia — 210 RSD
Focaccia sa pestom i sirom — 350 RSD

Десерты:
Banana bread (parče) — 350 RSD
Čokokeks (parče) — 450 RSD
Lemon cake (parče) — 300 RSD

Ohlađen / ohlađena не меняет цену.

Если пользователь сообщает новую цену — используй новую.

────────────────────
2. ПРАВИЛА NJUPPA
────────────────────

Скидка — 60%.
Клиент платит 40% полной стоимости.

Формула:
полная стоимость × 0,4 = цена NJUPPA.

Минимальная полная стоимость корзины — 1.000 RSD.
Желательная максимальная — 2.000 RSD.

Не использовать больше продукции, чем есть в остатках.

Пустое поле в отчёте = продукта нет, если пользователь не уточнил обратное.

Не обязательно использовать все остатки. Если свежий продукт разумнее оставить для обычной продажи — предложи это в черновике.

────────────────────
3. КАК СОБИРАТЬ КОРЗИНЫ
────────────────────

Не стремись сделать максимальное количество маленьких корзин.

Лучше меньше корзин, но более наполненных и привлекательных.

Можно:
— класть несколько одинаковых продуктов;
— делать несколько одинаковых корзин;
— делать чисто хлебные корзины;
— объединять 3–4 хлеба в одну корзину.

Для смешанных корзин по возможности:
хлеб + pecivo + десерт.

Не создавай странные комбинации только ради использования всех остатков.

Если одинаковых корзин несколько — делай один блок и указывай Količina.

ВАЖНО: при расчёте остатков умножай состав корзины на Količina.

────────────────────
4. ОХЛАЖДЁННЫЕ ПРОДУКТЫ
────────────────────

Можно рекомендовать для заморозки:

Tartin
Rustični sa semenkama
Raženi hleb
Focaccia

Если вся корзина подходит для заморозки:

❄️ Sve proizvode možete zamrznuti.

Если только часть:

❄️ [название продукта] možete zamrznuti.

НЕ рекомендовать для заморозки:
Brios hleb
Focaccia sa pestom i sirom

Для Focaccia sa pestom i sirom:

🧀 Pesto focacciu ne zamrzavati. Čuvati u kesi ili frižideru. Zagrejati u rerni 160–180°C do 10 min.

Для Banana bread:

🧊 Banana bread čuvati u frižideru 5–7 dana.

────────────────────
5. НАЗВАНИЯ
────────────────────

В составе корзин используй только эти названия:

Cimet
Sir
Brioš karamel
Brioš zemička
Babka čoko
Babka sa karamelom
Mak
Tartin
Rustični sa semenkama
Brios hleb
Raženi hleb
Focaccia
Focaccia sa pestom i sirom
Banana bread (parče)
Čokokeks (parče)
Lemon cake (parče)

Для охлаждённых продуктов добавляй:
(ohlađen) / (ohlađena)

Например:
Tartin (ohlađen)
Focaccia (ohlađena)

Названия самих корзин можешь придумывать:
Tartin Rustični Mix
Hlebni Mix za Zamrzavanje
Slatki Mix
Pecivo Mix
и т. п.

────────────────────
6. ДЕСЕРТЫ
────────────────────

Если указаны куски:

Banana: 1 кусок → Banana bread (parče)
Čokokeks: 2 куска → Čokokeks (parče) ×2
Lemon cake: 1 кусок → Lemon cake (parče)

Если десерт остался ЦЕЛЫМ — не включай его в NJUPPA без отдельного разрешения пользователя.

────────────────────
7. ДАТА
────────────────────

Обычно остатки за день D используются для корзин следующего рабочего дня.

В заголовке ставь дату, НА КОТОРУЮ публикуются корзины.

Если дату нельзя определить — спроси пользователя.

────────────────────
8. ФОРМАТ ОТВЕТА
────────────────────

Сначала дай короткий ЧЕРНОВИК:

— сколько всего продукции;
— какие корзины предлагаешь;
— сколько продуктов будет использовано;
— что останется;
— что предлагаешь оставить вне NJUPPA.

Затем дай ГОТОВЫЙ ТЕКСТ ДЛЯ ПУБЛИКАЦИИ на сербском.

Формат:

🌸 NJUPPA DD.MM 🌸

🧺 #1 Название корзины

Tartin (ohlađen) ×2
Rustični sa semenkama (ohlađen)
Cimet

❄️ Sve proizvode koje je moguće zamrznuti možete zamrznuti.

📞 Ako ne možete da preuzmete porudžbinu na vreme ili imate pitanje, pozovite/pišite nam: +381 62 8844 064.

Ukupno: 1.370 RSD → 548 RSD (60%)
Količina: 3️⃣ korpe

──────────────

Для количества:

Količina: 1️⃣ korpa
Količina: 2️⃣ korpe
Količina: 3️⃣ korpe

ТЕЛЕФОННЫЙ БЛОК ОБЯЗАТЕЛЕН В КАЖДОЙ КОРЗИНЕ. Копируй его дословно,
он уже дан выше, спрашивать его у пользователя НЕ НУЖНО:

📞 Ako ne možete da preuzmete porudžbinu na vreme ili imate pitanje, pozovite/pišite nam: +381 62 8844 064.

────────────────────
9. ПРОВЕРКА ПЕРЕД ОТВЕТОМ
────────────────────

Перед публикацией обязательно проверь:

✓ ни одного продукта не использовано больше, чем есть;
✓ при Količina >1 состав корзины умножен на количество корзин;
✓ каждая корзина стоит минимум 1.000 RSD;
✓ стоимость каждого продукта посчитана правильно;
✓ цена NJUPPA = полная стоимость × 0,4;
✓ целые десерты не использованы без разрешения;
✓ Brios hleb и Focaccia sa pestom i sirom не рекомендованы для заморозки;
✓ указана правильная дата;
✓ в КАЖДОЙ корзине есть телефонный блок с номером +381 62 8844 064;
✓ формат стоимости именно «Ukupno: X RSD → Y RSD (60%)», не «Puna cena»;
✓ баланс сходится: исходные остатки = использовано + осталось.

Если есть ошибка — сначала исправь её, затем выдавай финальный текст.

────────────────────
10. ЕСЛИ ЧЕГО-ТО НЕ ХВАТАЕТ
────────────────────

Не проси прислать текст корзин или телефонный блок — всё нужное уже есть
в этой инструкции и в переданном отчёте. Работай с тем, что дано.
Если данных действительно недостаточно, коротко скажи чего именно не хватает
и всё равно выдай лучший возможный вариант.
"""


# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("users", {})
    s.setdefault("prompt", DEFAULT_PROMPT)

    # миграция со старого формата: раньше корзины и отчёт были общими строками,
    # теперь у каждого свои. Строку выбрасываем, она всё равно за вчера.
    for key in ("baskets", "reports", "report"):
        if not isinstance(s.get(key), dict):
            s.pop(key, None)
    s.setdefault("baskets", {})
    s.setdefault("reports", {})
    return s


def save_state(state: dict) -> bool:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
        return True
    except Exception as e:
        print("⚠️ Не смогла сохранить состояние:", e)
        return False


STATE = load_state()


def is_allowed(uid: int) -> bool:
    return uid == ADMIN_CHAT_ID or str(uid) in STATE["users"]


def user_role(uid) -> str:
    if uid == ADMIN_CHAT_ID or str(uid) == str(ADMIN_CHAT_ID):
        return "manager"
    v = STATE["users"].get(str(uid))
    if isinstance(v, dict):
        return v.get("role") or "barista"
    return "barista"


def is_manager(uid) -> bool:
    return user_role(uid) == "manager"


def managers() -> list:
    out = [ADMIN_CHAT_ID]
    for u, v in STATE["users"].items():
        if isinstance(v, dict) and v.get("role") == "manager" and int(u) != ADMIN_CHAT_ID:
            out.append(int(u))
    return out


def user_name(uid) -> str:
    """Старый формат хранил строку, новый — словарь. Понимаем оба."""
    v = STATE["users"].get(str(uid))
    if isinstance(v, dict):
        return v.get("name") or str(uid)
    return v or str(uid)


def get_baskets(uid):
    return STATE.setdefault("baskets", {}).get(str(uid), "")


def get_report(uid):
    return STATE.setdefault("reports", {}).get(str(uid), "")


def set_baskets(uid, baskets=None, report=None):
    if baskets is not None:
        STATE.setdefault("baskets", {})[str(uid)] = baskets
    if report is not None:
        STATE.setdefault("reports", {})[str(uid)] = report
    save_state(STATE)


def get_prompt() -> str:
    p = STATE.get("prompt")
    return p if p and len(p) > 1000 else DEFAULT_PROMPT


# ── ЧИСТКА И ПРОВЕРКИ ────────────────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = text.replace("```", "").replace("`", "")
    return text.strip()


def parse_inventory(report_text: str) -> dict:
    SKIP = ("datum", "količina po vrstama", "kolicina", "osnovni asortiman",
            "njuppa prodato", "nisu prodati", "broj korpe")
    stock = {}
    for line in report_text.splitlines():
        m = re.match(r"\s*([A-Za-zČĆŠŽĐčćšžđА-Яа-яёЁ][^:]{1,40}?)\s*:\s*(\d+)", line)
        if not m:
            continue
        name = m.group(1).strip().lower()
        if any(s in name for s in SKIP):
            continue
        if "ohlađen" in name or "ohladen" in name:
            continue                      # склад, не в корзины
        stock[name] = stock.get(name, 0) + int(m.group(2))
    return stock


def check_inventory_in_baskets(baskets: str, stock: dict) -> list:
    """Считает позиции ТОЛЬКО в готовом тексте корзин, черновик игнорирует."""
    if not stock:
        return []

    # всё до первой 🧺 — это черновик и рассуждения, их не считаем
    i = baskets.find("🧺")
    if i == -1:
        return []
    body = baskets[i:]

    SKIP_WORDS = ("ukupno", "puna cena", "njuppa cena", "količina", "kolicina",
                  "cena", "ako ne možete", "možete zamrznuti", "zamrzavati",
                  "čuvati", "korpa", "korpe", "всего", "остат", "использ")

    used, qty, pending = {}, 1, {}
    for raw in body.splitlines():
        line = raw.strip().strip("-–— ").strip()
        if not line:
            continue

        low = line.lower()

        # конец блока корзины: фиксируем количество и переносим в общий счёт
        km = re.search(r"koli[čc]ina\s*:\s*\D*(\d+)", low)
        if km:
            qty = int(km.group(1))
            for k, v in pending.items():
                used[k] = used.get(k, 0) + v * qty
            pending, qty = {}, 1
            continue

        # новая корзина: то, что не успели умножить, считаем по одной
        if line.startswith("🧺"):
            for k, v in pending.items():
                used[k] = used.get(k, 0) + v
            pending = {}
            continue

        if any(w in low for w in SKIP_WORDS) or line[0] in "❄️🧀🧊📞✨🌸":
            continue
        if ":" in line or len(line) > 45:
            continue

        m = re.match(r"^(.+?)(?:\s*[×xX*]\s*(\d+))?$", line)
        if not m:
            continue
        name = re.sub(r"\((ohla[đd]en[ao]?)\)", "", m.group(1), flags=re.I)
        name = name.strip(" .,·•").lower()
        if len(name) < 3:
            continue
        pending[name] = pending.get(name, 0) + int(m.group(2) or 1)

    for k, v in pending.items():
        used[k] = used.get(k, 0) + v

    problems = []
    for name, n in used.items():
        have = None
        for k, v in stock.items():
            if k == name or k in name or name in k:
                have = v
                break
        if have is None:
            continue
        if n > have:
            problems.append(f"{name}: разложено {n}, в остатках {have}")
    return problems


# ── ВЫЗОВ МОДЕЛИ ─────────────────────────────────────────────────────────────

def ask_ai(system_prompt: str, user_text: str) -> str:
    """gpt-5.6-terra — рассуждающая модель: effort вместо temperature."""
    try:
        r = client.responses.create(
            model=OPENAI_MODEL,
            instructions=system_prompt,
            input=user_text,
            reasoning={"effort": REASONING_EFFORT},
        )
        return clean_markdown(r.output_text or "")
    except Exception as e:
        print("Responses API не сработал, пробую chat.completions:", e)
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        )
        return clean_markdown(r.choices[0].message.content or "")


FORMAT_REMINDER = (
    "\n\nФормат готового текста соблюдай ТОЧНО, как задано в промте:\n"
    "• строка Ukupno: XXXX RSD → XXXX RSD (60%) — именно так, не «Puna cena»\n"
    "• строка Količina: 1️⃣ korpa / 2️⃣ korpe с цифрой-эмодзи\n"
    "• телефонный блок 📞 с номером +381 62 8844 064 повторяется в КАЖДОЙ корзине\n"
    "• ничего не переспрашивай, работай с тем, что дано\n"
    "• заголовок 🌸 NJUPPA DD.MM 🌸 с датой публикации"
)


def generate_baskets(report_text: str) -> str:
    return ask_ai(get_prompt(),
                  f"Остатки из отчёта:\n\n{report_text}\n\n"
                  f"Составь корзины." + FORMAT_REMINDER)


def fix_baskets(current: str, fix_request: str, report: str = "") -> str:
    head = f"Исходные остатки:\n\n{report}\n\n" if report else ""
    return ask_ai(get_prompt(), (
        head +
        f"Текущий вариант корзин:\n\n{current}\n\n"
        f"Нужно исправить: {fix_request}\n\n"
        f"Верни полный обновлённый текст корзин целиком, "
        f"не переспрашивай." + FORMAT_REMINDER
    ))


def rebuild_baskets(current: str, report: str, sold: str) -> str:
    head = (f"Исходный отчёт по остаткам был такой:\n\n{report}\n\n" if report else
            "Исходного отчёта по остаткам нет. Считай, что доступно ровно то, "
            "что перечислено в корзинах ниже.\n\n")
    return ask_ai(get_prompt(), (
        head +
        f"Текущие корзины:\n\n{current}\n\n"
        f"Но за день часть продалась прямо в пекарне:\n{sold}\n\n"
        f"Пересобери корзины с учётом того, что этих позиций больше нет. "
        f"Если продана целая корзина — её позиции считаются ушедшими и в новый "
        f"расклад не попадают. Нумерацию начни заново с #1. "
        f"Верни полный новый текст корзин." + FORMAT_REMINDER
    ))


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────

def preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать в Njuppa", callback_data="publish")],
        [InlineKeyboardButton("✏️ Исправить", callback_data="fix"),
         InlineKeyboardButton("🔄 Пересобрать", callback_data="rebuild")],
        [InlineKeyboardButton("❌ Отменить", callback_data="cancel")],
    ])


def access_keyboard(uid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Бариста", callback_data=f"approve:{uid}"),
         InlineKeyboardButton("👑 Менеджер", callback_data=f"promote:{uid}")],
        [InlineKeyboardButton("❌ Отказать", callback_data=f"deny:{uid}")],
    ])


async def send_preview(context, chat_id: int, baskets: str, note: str = ""):
    preview = "📋 Предпросмотр корзин:\n\n" + baskets
    if note:
        preview += "\n\n" + note
    if len(preview) > 4000:
        preview = preview[:4000] + "\n\n(обрезано для предпросмотра)"
    msg = await context.bot.send_message(
        chat_id=chat_id, text=preview, reply_markup=preview_keyboard()
    )
    # активным считается только последний предпросмотр
    STATE.setdefault("active_preview", {})[str(chat_id)] = msg.message_id
    save_state(STATE)
    return msg


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(
        "Привет! Я собираю корзины для Njuppa.\n\n"
        "Отправила Светлане запрос на доступ — как подтвердит, напишу тебе."
    )
    text = (f"🔐 Запрос доступа\n\n{u.full_name}"
            + (f"\n@{u.username}" if u.username else "")
            + f"\nID: {u.id}")
    for mid in managers():
        try:
            await context.bot.send_message(mid, text, reply_markup=access_keyboard(u.id))
        except Exception:
            pass


async def run_ai(message, fn, *args):
    """Ждём модель не дольше AI_TIMEOUT. Возвращает текст или None."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=AI_TIMEOUT)
    except asyncio.TimeoutError:
        await message.reply_text(
            f"⏱ Модель не ответила за {AI_TIMEOUT} секунд.\n"
            "Попробуй ещё раз. Если повторится — напиши Светлане."
        )
    except Exception as e:
        await message.reply_text(f"❌ Ошибка: {e}")
    return None


# ── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await request_access(update, context)
        return
    await update.message.reply_text(
        "Привет! Пришли отчёт по остаткам — соберу корзины.\n\n"
        "/rebuild — пересобрать, если что-то продали внеурочно\n"
        "/help — что ещё умею"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    t = ("Что умею:\n\n"
         "📤 Пришли отчёт по остаткам — соберу корзины.\n"
         "Под сборкой кнопки: опубликовать, исправить, пересобрать.\n\n"
         "/rebuild — пересобрать. Продали что-то внеурочно или целую корзину —\n"
         "  напиши что именно, пересоберу остальное.\n"
         "/cancel — отменить текущее действие")
    if is_manager(update.effective_user.id):
        t += ("\n\nДля менеджеров:\n"
              "/getprompt — показать правила сборки\n"
              "/setprompt — изменить правила (файлом .txt или кусками + /done)\n"
              "/resetprompt — вернуть встроенные правила\n"
              "/users — кто имеет доступ\n"
              "/revoke ID — забрать доступ")
    await update.message.reply_text(t)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    if not STATE["users"]:
        await update.message.reply_text("Пока никого. Пусть напишут боту /start.")
        return
    lines = [f"• {user_name(u)} — {u}"
             + (" 👑 менеджер" if user_role(u) == "manager" else " · бариста")
             for u in STATE["users"]]
    await update.message.reply_text("Доступ есть у:\n" + "\n".join(lines))


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Напиши так: /revoke 5525613586")
        return
    uid = context.args[0]
    if uid not in STATE["users"]:
        await update.message.reply_text("Такого ID нет в списке.")
        return
    name = user_name(uid)
    STATE["users"].pop(uid, None)
    save_state(STATE)
    await update.message.reply_text(f"Доступ забрала: {name} ({uid})")


async def cmd_rebuild(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not get_baskets(update.effective_user.id):
        await update.message.reply_text(
            "Нечего пересобирать — корзин ещё не было. Сначала пришли отчёт."
        )
        return
    context.user_data["state"] = WAITING_FOR_REBUILD
    await update.message.reply_text(
        "🔄 Что ушло? Напиши как удобно, например:\n\n"
        "«продана корзина 2, ещё Cimet 2 и Babka čoko 1»\n\n"
        "Указывай только то, что ушло дополнительно — не весь остаток."
    )


async def cmd_getprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        if is_allowed(update.effective_user.id):
            await update.message.reply_text("Правила сборки смотрят и меняют менеджеры.")
        return
    p = get_prompt()
    src = "свой" if STATE.get("prompt") and len(STATE["prompt"]) > 1000 else "встроенный"
    await update.message.reply_text(f"Сейчас работает {src} промт, {len(p)} символов:")
    for i in range(0, len(p), 3800):
        await update.message.reply_text(p[i:i + 3800])


async def cmd_setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        if is_allowed(update.effective_user.id):
            await update.message.reply_text("Правила сборки меняют менеджеры.")
        return
    context.user_data["state"] = WAITING_FOR_PROMPT
    context.user_data[PROMPT_PARTS] = []
    await update.message.reply_text(
        "📝 Жду новый промт. Два способа:\n\n"
        "1️⃣ Файлом — просто пришли .txt с промтом. Длина любая, это надёжнее.\n\n"
        "2️⃣ Сообщениями — присылай кусками, телеграм режет по 4096 символов.\n"
        "Как закончишь, напиши /done.\n\n"
        "Отменить — /cancel"
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        return
    if context.user_data.get("state") != WAITING_FOR_PROMPT:
        await update.message.reply_text("Сейчас нечего заканчивать.")
        return
    parts = context.user_data.get(PROMPT_PARTS) or []
    text = "\n".join(parts).strip()
    context.user_data["state"] = None
    context.user_data[PROMPT_PARTS] = []

    if len(text) < 200:
        await update.message.reply_text(
            "Промт подозрительно короткий, не сохраняю.\n"
            "Если хотела вернуть встроенный — /resetprompt"
        )
        return

    STATE["prompt"] = text
    ok = save_state(STATE)
    await update.message.reply_text(
        f"✅ Промт сохранён, {len(text)} символов, частей: {len(parts)}.\n"
        "Действует со следующей сборки."
        + ("" if ok else "\n⚠️ На диск не записалось, слетит при перезапуске.")
    )


async def cmd_resetprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id):
        if is_allowed(update.effective_user.id):
            await update.message.reply_text("Правила сборки меняют менеджеры.")
        return
    STATE["prompt"] = DEFAULT_PROMPT
    save_state(STATE)
    context.user_data["state"] = None
    await update.message.reply_text(
        f"↩️ Вернула встроенный промт, {len(DEFAULT_PROMPT)} символов."
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = None
    context.user_data[PROMPT_PARTS] = []
    await update.message.reply_text("Ок, отменила.")


# ── ОСНОВНОЙ ОБРАБОТЧИК ──────────────────────────────────────────────────────

def extract_baskets_text(message) -> str | None:
    """Готовые корзины, собранные где-то ещё: пересланные или вставленные."""
    text = message.text or message.caption or ""
    if len(text) < 80:
        return None
    low = text.lower()
    hits = 0
    if "🧺" in text:
        hits += 1
    if "njuppa" in low:
        hits += 1
    if re.search(r"koli[čc]ina\s*:", low):
        hits += 1
    if re.search(r"ukupno\s*:", low):
        hits += 1
    if re.search(r"korp[aei]", low):
        hits += 1
    return text if hits >= 2 else None


def extract_report_text(message) -> str | None:
    text = message.text or message.caption or ""
    markers = ("Količina po vrstama", "Osnovni asortiman", "Cimet:", "Datum:")
    return text if any(m in text for m in markers) else None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    uid = update.effective_user.id

    if message.chat.type != "private":
        return
    if not is_allowed(uid):
        await request_access(update, context)
        return

    state = context.user_data.get("state")

    if state == WAITING_FOR_PROMPT and is_manager(uid):
        # промт файлом — без ограничений по длине
        if message.document:
            try:
                f = await message.document.get_file()
                raw = await f.download_as_bytearray()
                text = bytes(raw).decode("utf-8", errors="replace").strip()
            except Exception as e:
                await message.reply_text(f"❌ Не смогла прочитать файл: {e}")
                return
            if len(text) < 200:
                await message.reply_text("В файле почти пусто, не сохраняю.")
                return
            STATE["prompt"] = text
            ok = save_state(STATE)
            context.user_data["state"] = None
            context.user_data[PROMPT_PARTS] = []
            await message.reply_text(
                f"✅ Промт из файла сохранён, {len(text)} символов."
                + ("" if ok else "\n⚠️ На диск не записалось.")
            )
            return

        parts = context.user_data.setdefault(PROMPT_PARTS, [])
        parts.append(message.text or "")
        total = sum(len(p) for p in parts)
        await message.reply_text(
            f"Принято, часть {len(parts)}, всего {total} символов.\n"
            "Ещё кусок или /done"
        )
        return

    if state == WAITING_FOR_FIX:
        await message.reply_text("⏳ Исправляю...")
        new = await run_ai(message, fix_baskets,
                           get_baskets(uid), message.text or "", get_report(uid))
        if new is None:
            return
        set_baskets(uid, baskets=new)
        context.user_data["state"] = None
        await send_preview(context, uid, new)
        return

    if state == WAITING_FOR_REBUILD:
        await message.reply_text("⏳ Пересобираю...")
        new = await run_ai(message, rebuild_baskets,
                           get_baskets(uid), get_report(uid), message.text or "")
        if new is None:
            return
        set_baskets(uid, baskets=new)
        context.user_data["state"] = None
        await send_preview(context, uid, new, "🔄 Пересборка после продаж в пекарне.")
        return

    report = extract_report_text(message)

    # чужая сборка: взяли в работу, дальше можно править и пересобирать
    if not report:
        outside = extract_baskets_text(message)
        if outside:
            set_baskets(uid, baskets=outside, report="")
            await send_preview(
                context, uid, outside,
                "📥 Взяла в работу готовую сборку.\n"
                "Остатков к ней нет, поэтому при пересборке опирайся на состав корзин: "
                "напиши, что ушло, и я пересчитаю остальное."
            )
            return

        await message.reply_text(
            "Не похоже ни на отчёт по остаткам, ни на готовые корзины 🤔\n\n"
            "Перешли отчёт из чата барист — соберу с нуля.\n"
            "Или пришли готовые корзины — возьму их в работу и смогу править."
        )
        return

    await message.reply_text("⏳ Считаю корзины, это займёт до минуты...")
    baskets = await run_ai(message, generate_baskets, report)
    if baskets is None:
        return

    set_baskets(uid, baskets=baskets, report=report)

    problems = check_inventory_in_baskets(baskets, parse_inventory(report))
    note = "⚠️ Проверка остатков не сошлась:\n• " + "\n• ".join(problems) if problems else ""

    await send_preview(context, uid, baskets, note)


# ── КНОПКИ ───────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # ── доступ
    if data.startswith(("approve:", "promote:", "deny:")):
        if not is_manager(query.from_user.id):
            return
        action, uid = data.split(":", 1)

        if action == "deny":
            await query.edit_message_text(f"❌ Отказано (ID {uid})")
            try:
                await context.bot.send_message(int(uid), "Доступ не открыли.")
            except Exception:
                pass
            return

        role = "manager" if action == "promote" else "barista"
        try:
            chat = await context.bot.get_chat(int(uid))
            name = chat.full_name or chat.username or uid
        except Exception:
            name = user_name(uid)

        STATE["users"][uid] = {"name": name, "role": role}
        ok = save_state(STATE)
        label = "менеджер 👑" if role == "manager" else "бариста"
        await query.edit_message_text(
            f"✅ Доступ выдан ({label}): {name} ({uid})"
            + ("" if ok else "\n⚠️ Не сохранилось на диск — слетит при перезапуске.")
        )
        hello = ("✅ Доступ открыт!\n\n"
                 "Присылай отчёт по остаткам — соберу корзины.\n"
                 "Кнопки под сборкой твои: править, пересобирать, публиковать.\n\n")
        if role == "manager":
            hello += ("Ты менеджер: можешь ещё смотреть и менять правила сборки\n"
                      "(/getprompt, /setprompt) и выдавать доступ другим.\n\n")
        hello += "/help — что ещё умею"
        try:
            await context.bot.send_message(int(uid), hello)
        except Exception:
            pass
        return

    # ── корзины: публикует любой, у кого есть доступ
    uid = query.from_user.id
    if not is_allowed(uid):
        return

    # кнопки старого предпросмотра не работают
    active = STATE.get("active_preview", {}).get(str(uid))
    if active and query.message and query.message.message_id != active:
        await query.answer(
            "Это старый предпросмотр. Работай с последним сообщением.",
            show_alert=True
        )
        return

    # защита от двойного нажатия и от дубля при двух деплоях
    seen = context.bot_data.setdefault("_seen_clicks", {})
    click_id = f"{uid}:{query.message.message_id if query.message else 0}:{data}"
    now = time.time()
    for k, t in list(seen.items()):
        if now - t > 30:
            seen.pop(k, None)
    if click_id in seen:
        return
    seen[click_id] = now

    baskets = get_baskets(uid)

    if data == "publish":
        if not baskets:
            await query.edit_message_text("❌ Не нашла корзины для публикации.")
            return
        try:
            await context.bot.send_message(
                chat_id=NJUPPA_CHAT_ID, text=baskets,
                message_thread_id=NJUPPA_THREAD_ID
            )
            await query.edit_message_text("✅ Опубликовано в Njuppa!")
            STATE.setdefault("active_preview", {}).pop(str(uid), None)
            save_state(STATE)
            for mid in managers():
                if mid == uid:
                    continue
                try:
                    await context.bot.send_message(
                        mid, f"📤 {user_name(uid)} опубликовала корзины в Njuppa."
                    )
                except Exception:
                    pass
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")

    elif data == "fix":
        context.user_data["state"] = WAITING_FOR_FIX
        await query.message.reply_text(
            "✏️ Что исправить?\n"
            "Например: «убери корзину с тартином» или «добавь больше сладких»"
        )

    elif data == "rebuild":
        context.user_data["state"] = WAITING_FOR_REBUILD
        await query.message.reply_text(
            "🔄 Что ушло? Например:\n"
            "«продана корзина 2, ещё Cimet 2 и Babka čoko 1»"
        )

    elif data == "cancel":
        STATE.setdefault("baskets", {}).pop(str(uid), None)
        STATE.setdefault("active_preview", {}).pop(str(uid), None)
        save_state(STATE)
        context.user_data["state"] = None
        await query.edit_message_text("🚫 Отменено. Корзины не опубликованы.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in (("start", cmd_start), ("help", cmd_help),
                     ("rebuild", cmd_rebuild), ("getprompt", cmd_getprompt),
                     ("setprompt", cmd_setprompt), ("cancel", cmd_cancel),
                     ("users", cmd_users), ("revoke", cmd_revoke),
                     ("done", cmd_done), ("resetprompt", cmd_resetprompt)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    if not os.path.isdir(DATA_DIR):
        print(f"⚠️ Нет папки {DATA_DIR} — настройки не переживут перезапуск. "
              f"Подключи Volume в Railway.")
    print(f"🤖 Бот запущен: {OPENAI_MODEL}, effort={REASONING_EFFORT}, "
          f"доступ у {len(STATE['users'])} чел.")
    app.run_polling()


if __name__ == "__main__":
    main()
