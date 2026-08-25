import asyncio
import hashlib, logging, os, re
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from database import create_or_update_user, get_active_quest, set_active_quest, update_active_quest_text, complete_active_quest
from prompts import SYSTEM_PROMPT

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not TOKEN:
    raise ValueError("Нет TELEGRAM_BOT_TOKEN в .env")
if not GEMINI_API_KEY:
    raise ValueError("Нет GEMINI_API_KEY в .env")

MODEL = "gemini-3.6-flash"
TEACHER_ID = 328761045
REWARD = 25

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.WARNING,
)

# Наш логгер можно оставить информативным.
log = logging.getLogger("questificator")
log.setLevel(logging.INFO)

# Не показываем технические HTTP-запросы, потому что Telegram URL содержит токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)

client = genai.Client(api_key=GEMINI_API_KEY)

def clean(text):
    text = (text or "").strip().replace("```","").replace("`","")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    return text.strip()

def chunks(text, limit=3800):
    if len(text) <= limit:
        return [text]
    result, current = [], ""
    for p in text.split("\n"):
        candidate = f"{current}\n{p}".strip() if current else p
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                result.append(current)
            while len(p) > limit:
                result.append(p[:limit]); p = p[limit:]
            current = p
    if current:
        result.append(current)
    return result

async def send(message, text, reply_markup=None):
    parts = chunks(clean(text))
    for i, part in enumerate(parts):
        await message.reply_text(part, reply_markup=reply_markup if i == len(parts)-1 else None)

def quest_buttons(qid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Сделать эпичнее", callback_data=f"epic|{qid}")],
        [InlineKeyboardButton("✅ Завершить квест", callback_data=f"complete|{qid}")],
        [InlineKeyboardButton("⚔️ Новый квест", callback_data="new")]
    ])

def start_buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ Создать квест", callback_data="new")]])

def fallback(task, epic=False):
    if epic:
        return f"""🔥 ЭПИЧЕСКИЙ РЕЖИМ

Квест: Легендарное испытание «{task}»

Легенда:
Судьба мира неожиданно зависит от вполне обычного дела. Герою предстоит победить Прокрастинацию и вернуть порядок.

Испытания:
1. Подготовить всё необходимое и начать.
2. Выполнить основную часть без побочных миссий.
3. Проверить результат и довести дело до состояния «готово».

Награда:
25 XP и право торжественно объявить победу."""
    return f"""⚔️ Квест: Операция «{task}»

Легенда:
Перед героем появилась задача, которую пора превратить в короткую миссию.

Задания:
1. Подготовиться и убрать всё, что мешает начать.
2. Выполнить основную часть дела.
3. Проверить результат и поставить финальную точку.

Награда:
25 XP и спокойствие героя."""

async def ai(prompt):
    """
    Запрос к Gemini с автоматическими повторами.
    Помогает пережить временные 503, DNS-сбои и сетевые тайм-ауты.
    """
    delays = (2, 5, 10)
    last_error = None

    for attempt, delay in enumerate(delays, start=1):
        try:
            response = await client.aio.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.85,
                    max_output_tokens=1800,
                ),
            )
            if not response.text:
                raise RuntimeError("Gemini вернул пустой ответ")
            return clean(response.text)

        except Exception as exc:
            last_error = exc
            message = str(exc).lower()

            # Ошибки ключа/доступа не имеет смысла повторять.
            if (
                "401" in message
                or "403" in message
                or "api key" in message
                or "permission_denied" in message
                or "unauthenticated" in message
            ):
                raise

            log.warning(
                "Gemini временно недоступен, попытка %s/3. Повтор через %s сек.",
                attempt,
                delay,
            )

            if attempt < len(delays):
                await asyncio.sleep(delay)

    raise last_error

async def generate(task):
    return await ai(
        "Преврати реальную задачу пользователя в короткий игровой квест.\n\n"
        f"Задача: {task}\n\n"
        "Дай название, короткую легенду, 3–6 конкретных шагов и награду. "
        "Все действия должны быть реальными и безопасными."
    )

async def make_epic(text):
    return await ai(
        "Сделай этот квест более эпичным и смешным в RPG-стиле, "
        "но сохрани реальные выполнимые действия и не раздувай объём.\n\n" + text
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    u = create_or_update_user(tg.id, tg.first_name or "Герой")
    await send(update.message,
        f"🧙 Добро пожаловать в Квестификатор ⚔️\n\n"
        f"Герой: {u['name']}\n⭐ Уровень: {u['level']}\n"
        f"✨ Опыт: {u['xp']} XP\n🏆 Выполнено квестов: {u['quests_completed']}\n\n"
        "Напиши обычное дело, а я превращу его в приключение.",
        start_buttons())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update.message,
        "Как пользоваться:\n\n"
        "1. Напиши обычную задачу.\n"
        "2. ИИ создаст квест.\n"
        "3. Можно сделать его эпичнее.\n"
        "4. После реального выполнения нажми «✅ Завершить квест».\n\n"
        "/start — начать\n/quest — новый квест\n/profile — профиль\n/id — твой Telegram ID\n/help — помощь")

async def quest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update.message, "⚔️ Напиши задачу, которую превратить в квест.")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send(update.message, f"Твой Telegram ID: {update.effective_user.id}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    u = create_or_update_user(tg.id, tg.first_name or "Герой")
    progress = u["xp"] % 100
    achievements = ", ".join(u.get("achievements", [])[-3:]) or "пока нет"
    await send(update.message,
        f"🧙 Профиль героя\n\nИмя: {u['name']}\n⭐ Уровень: {u['level']}\n"
        f"✨ Всего XP: {u['xp']}\n📈 До следующего уровня: {100-progress} XP\n"
        f"🏆 Квестов выполнено: {u['quests_completed']}\n🎖 Достижения: {achievements}")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    create_or_update_user(tg.id, tg.first_name or "Герой")
    task = (update.message.text or "").strip()
    if not task:
        return
    await update.message.chat.send_action("typing")
    try:
        text = await generate(task)
    except Exception as e:
        log.exception("Gemini error: %s", e)
        if tg.id == TEACHER_ID:
            text = fallback(task)
        else:
            await send(update.message, "Сейчас ИИ временно недоступен. Попробуй ещё раз чуть позже.")
            return
    q = set_active_quest(tg.id, task, text, REWARD)
    await send(update.message, text, quest_buttons(q["id"]))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = update.effective_user
    create_or_update_user(tg.id, tg.first_name or "Герой")

    if q.data == "new":
        await send(q.message, "⚔️ Напиши новую задачу.")
        return

    action, _, qid = q.data.partition("|")
    quest = get_active_quest(tg.id)
    if not quest or quest.get("id") != qid:
        await send(q.message, "Этот квест уже не активен. Создай новый квест ⚔️")
        return

    if action == "epic":
        if quest.get("completed"):
            await send(q.message, "Этот квест уже завершён 🏆")
            return
        await q.message.chat.send_action("typing")
        try:
            text = await make_epic(quest["text"])
        except Exception as e:
            log.exception("Epic error: %s", e)
            if tg.id == TEACHER_ID:
                text = fallback(quest["task"], True)
            else:
                await send(q.message, "Не получилось усилить квест. Попробуй ещё раз.")
                return
        update_active_quest_text(tg.id, qid, text)
        await send(q.message, text, quest_buttons(qid))
        return

    if action == "complete":
        result = complete_active_quest(tg.id, qid)
        if result["status"] == "already_completed":
            await send(q.message, "Этот квест уже был засчитан ✅")
            return
        if result["status"] != "completed":
            await send(q.message, "Не удалось засчитать квест. Создай новый.")
            return
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        u = result["user"]
        level_up = f"\n\n🎊 Новый уровень: {u['level']}!" if result["level_up"] else ""
        await send(q.message,
            f"🎉 Квест завершён!\n\n⭐ +{result['reward']} XP\n"
            f"✨ Всего XP: {u['xp']}\n🏆 Выполнено квестов: {u['quests_completed']}{level_up}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Telegram error: %s", context.error)

def main():
    # Увеличиваем сетевые тайм-ауты Telegram.
    # Это важно при нестабильном соединении: стандартные тайм-ауты библиотеки довольно короткие.
    bot_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    updates_request = HTTPXRequest(
        connection_pool_size=2,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .request(bot_request)
        .get_updates_request(updates_request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quest", quest_cmd))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(error_handler)

    if os.getenv("RENDER", "").lower() == "true":
        # Render автоматически задаёт RENDER_EXTERNAL_HOSTNAME для Web Service.
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        legacy_url = os.getenv("RENDER_EXTERNAL_URL")

        if legacy_url:
            base_url = legacy_url.rstrip("/")
        elif hostname:
            base_url = f"https://{hostname}"
        else:
            raise ValueError(
                "На Render не найден RENDER_EXTERNAL_HOSTNAME."
            )

        path = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
        secret = hashlib.sha256(
            (TOKEN + "-questificator").encode()
        ).hexdigest()[:32]

        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "10000")),
            url_path=path,
            webhook_url=f"{base_url}/{path}",
            secret_token=secret,
            drop_pending_updates=False,
        )
    else:
        app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
