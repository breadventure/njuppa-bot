import os
import re
import json
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

client = OpenAI(api_key=OPENAI_KEY)

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
• Focaccia — 400
• Focaccia sa pestom i sirom — 450
• Lemon cake (кусок) — 250
• Banana keks (кусок) — 240
• Čoko keks (кусок) — 280

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

4. КАК ЧИТАТЬ ОТЧЁТ
• «Osnovni asortiman» — остаток на витрине к закрытию. Он идёт в корзины.
• «X ohlađen» — охлаждённый хлеб. В корзины НЕ идёт: он остаётся на складе
  и продаётся дальше. Показывай его отдельной строкой в остатках.
• «N целых M кусок» у кексов: в корзины идут ТОЛЬКО куски. Целые кексы —
  склад, они режутся на витрину, в корзину их класть нельзя.
• «не украшенные» у кекса — тоже склад, не в корзину.
• Если у куска написано «свежий, на витрину» — он остаётся на витрине
  назавтра и в корзину НЕ идёт.
• Пустое значение или отсутствие строки — значит ноль.

5. ЛОГИКА СБОРКИ
Приоритет:
1. Сначала сложные/редкие позиции (семечки, ржаной)
2. Потом: корзины с хлебом, затем булки
3. Баланс: чередовать сладкие / сладко-солёные / хлебные

6. СТРУКТУРА КОРЗИНЫ
Формат:
🧺 Korpa #X — Название

✨ Короткое описание

• Позиция
• Позиция ×2

Ukupno: XXXX RSD → XXXX RSD (60%)
Količina: X korpe

7. ПРОВЕРКА В КОНЦЕ (ОБЯЗАТЕЛЬНО)
✔ проверку цен
✔ проверку остатков
✔ показать, что осталось неиспользованным
✔ отдельной строкой — что лежит на складе (охлаждённые, целые кексы)

8. ФОРМАТ ОТВЕТА
Только готовый текст корзин. Не показывай ход рассуждений, черновые
расчёты и размышления. Никакого markdown — ни звёздочек, ни решёток.

Тон: простой, дружелюбный, без пафоса, честный.

Если не хватает позиций — СРАЗУ написать об этом, не придумывать.
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
    return STATE.get("prompt") or DEFAULT_PROMPT


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
    "• телефонный блок 📞 повторяется в КАЖДОЙ корзине дословно\n"
    "• заголовок 🌸 NJUPPA DD.MM 🌸 с датой публикации"
)


def generate_baskets(report_text: str) -> str:
    return ask_ai(get_prompt(),
                  f"Остатки из отчёта:\n\n{report_text}\n\n"
                  f"Составь корзины." + FORMAT_REMINDER)


def fix_baskets(current: str, fix_request: str) -> str:
    return ask_ai(get_prompt(), (
        f"Вот текущий вариант корзин:\n\n{current}\n\n"
        f"Нужно исправить: {fix_request}\n\n"
        f"Верни полный обновлённый текст корзин." + FORMAT_REMINDER
    ))


def rebuild_baskets(current: str, report: str, sold: str) -> str:
    return ask_ai(get_prompt(), (
        f"Исходный отчёт по остаткам был такой:\n\n{report}\n\n"
        f"По нему были собраны корзины:\n\n{current}\n\n"
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
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Пустить как баристу", callback_data=f"approve:{uid}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"deny:{uid}"),
    ]])


async def send_preview(context, chat_id: int, baskets: str, note: str = ""):
    preview = "📋 Предпросмотр корзин:\n\n" + baskets
    if note:
        preview += "\n\n" + note
    if len(preview) > 4000:
        preview = preview[:4000] + "\n\n(обрезано для предпросмотра)"
    await context.bot.send_message(
        chat_id=chat_id, text=preview, reply_markup=preview_keyboard()
    )


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(
        "Привет! Я собираю корзины для Njuppa.\n\n"
        "Отправила Светлане запрос на доступ — как подтвердит, напишу тебе."
    )
    await context.bot.send_message(
        ADMIN_CHAT_ID,
        f"🔐 Запрос доступа\n\n{u.full_name}"
        + (f"\n@{u.username}" if u.username else "")
        + f"\nID: {u.id}",
        reply_markup=access_keyboard(u.id),
    )


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
         "/getprompt — показать правила сборки\n"
         "/cancel — отменить текущее действие")
    if update.effective_user.id == ADMIN_CHAT_ID:
        t += ("\n\nТолько для тебя:\n"
              "/setprompt — изменить правила сборки\n"
              "/users — кто имеет доступ\n"
              "/revoke ID — забрать доступ")
    await update.message.reply_text(t)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if not STATE["users"]:
        await update.message.reply_text("Пока никого. Пусть напишут боту /start.")
        return
    lines = [f"• {user_name(u)} — {u} (бариста)" for u in STATE["users"]]
    await update.message.reply_text("Доступ есть у:\n" + "\n".join(lines))


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
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
    if not is_allowed(update.effective_user.id):
        return
    p = get_prompt()
    for i in range(0, len(p), 3800):
        await update.message.reply_text(p[i:i + 3800])


async def cmd_setprompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Правила меняет только Светлана.")
        return
    context.user_data["state"] = WAITING_FOR_PROMPT
    await update.message.reply_text(
        "Пришли новый промт одним сообщением.\n"
        "Совет: сначала /getprompt, скопируй, поправь и пришли обратно."
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = None
    await update.message.reply_text("Ок, отменила.")


# ── ОСНОВНОЙ ОБРАБОТЧИК ──────────────────────────────────────────────────────

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

    if state == WAITING_FOR_PROMPT:
        STATE["prompt"] = message.text or ""
        ok = save_state(STATE)
        context.user_data["state"] = None
        await message.reply_text(
            "✅ Промт сохранён." if ok else
            "⚠️ Промт применён, но сохранить не вышло — слетит при перезапуске.\n"
            "Проверь, что в Railway подключён Volume на /data."
        )
        return

    if state == WAITING_FOR_FIX:
        await message.reply_text("⏳ Исправляю...")
        new = await run_ai(message, fix_baskets, get_baskets(uid), message.text or "")
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
    if not report:
        await message.reply_text(
            "Не похоже на отчёт по остаткам 🤔\n"
            "Перешли сообщение с остатками из чата барист."
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
    if data.startswith(("approve:", "deny:")):
        if query.from_user.id != ADMIN_CHAT_ID:
            return
        action, uid = data.split(":", 1)
        if action == "approve":
            try:
                chat = await context.bot.get_chat(int(uid))
                name = chat.full_name or chat.username or uid
            except Exception:
                name = uid
            STATE["users"][uid] = {"name": name, "role": "barista"}
            ok = save_state(STATE)
            await query.edit_message_text(
                f"✅ Доступ выдан (бариста): {name} ({uid})"
                + ("" if ok else "\n⚠️ Не сохранилось на диск — слетит при перезапуске.")
            )
            try:
                await context.bot.send_message(
                    int(uid),
                    "✅ Доступ открыт!\n\n"
                    "Присылай отчёт по остаткам — соберу корзины.\n"
                    "Кнопки под сборкой твои: можешь править, пересобирать "
                    "и публиковать в Njuppa.\n\n"
                    "/help — что ещё умею"
                )
            except Exception:
                pass
        else:
            await query.edit_message_text(f"❌ Отказано (ID {uid})")
            try:
                await context.bot.send_message(int(uid), "Доступ не открыли.")
            except Exception:
                pass
        return

    # ── корзины: публикует любой, у кого есть доступ
    uid = query.from_user.id
    if not is_allowed(uid):
        return
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
            if uid != ADMIN_CHAT_ID:
                try:
                    await context.bot.send_message(
                        ADMIN_CHAT_ID,
                        f"📤 {user_name(uid)} опубликовала корзины в Njuppa."
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
        save_state(STATE)
        context.user_data["state"] = None
        await query.edit_message_text("🚫 Отменено. Корзины не опубликованы.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in (("start", cmd_start), ("help", cmd_help),
                     ("rebuild", cmd_rebuild), ("getprompt", cmd_getprompt),
                     ("setprompt", cmd_setprompt), ("cancel", cmd_cancel),
                     ("users", cmd_users), ("revoke", cmd_revoke)):
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
