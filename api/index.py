import asyncio
import hashlib
import os
import random
import re

import httpx
from fastapi import FastAPI, HTTPException, Request
from google import genai
from google.genai import types

from prompts import SYSTEM_PROMPT

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

app = FastAPI(title="Квестификатор")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TEACHER_ID = 328761045
OWNER_ID = 5517738880
ALLOWED_USER_IDS = {OWNER_ID, TEACHER_ID}

DEMO_LOCK_MESSAGE = (
    "🔒 Квестификатор сейчас работает в режиме учебной демонстрации.\n"
    "Генерация квестов доступна автору проекта и преподавателю."
)

MODEL_NAME = "gemini-3.6-flash"
WEBHOOK_SECRET = hashlib.sha256(
    (TELEGRAM_BOT_TOKEN + "-questificator").encode("utf-8")
).hexdigest()[:32]
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def clean_plain_text(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("```", "").replace("`", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    return text.strip()

def split_text(text: str, limit: int = 3800) -> list[str]:
    text = clean_plain_text(text)
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for paragraph in text.split("\n"):
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(paragraph) > limit:
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        parts.append(current)
    return parts

def quest_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "🔥 Сделать эпичнее", "callback_data": "epic"}],
        [{"text": "✅ Завершить квест", "callback_data": "complete"}],
        [{"text": "⚔️ Новый квест", "callback_data": "new"}],
    ]}

def start_keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "⚔️ Создать квест", "callback_data": "new"}]]}

async def telegram_call(method: str, payload: dict | None = None) -> dict:
    last_error = None
    for delay in (0, 2, 5):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{TELEGRAM_API}/{method}", json=payload or {})
                response.raise_for_status()
                data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return data
        except Exception as exc:
            last_error = exc
    raise last_error

async def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    parts = split_text(text)
    for index, part in enumerate(parts):
        payload = {"chat_id": chat_id, "text": part}
        if reply_markup and index == len(parts) - 1:
            payload["reply_markup"] = reply_markup
        await telegram_call("sendMessage", payload)

async def send_chat_action(chat_id: int) -> None:
    try:
        await telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

async def answer_callback(callback_id: str) -> None:
    try:
        await telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

async def remove_keyboard(chat_id: int, message_id: int) -> None:
    try:
        await telegram_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception:
        pass

async def call_ai(prompt: str) -> str:
    last_error = None
    for delay in (0, 2, 5, 10):
        if delay:
            await asyncio.sleep(delay)
        try:
            response = await ai_client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.85,
                    max_output_tokens=1800,
                ),
            )
            if not response.text:
                raise RuntimeError("Gemini вернул пустой ответ")
            return clean_plain_text(response.text)
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if any(x in message for x in ("401", "403", "api key", "permission_denied", "unauthenticated")):
                raise
    raise last_error

async def generate_quest(task: str) -> str:
    return await call_ai(
        "Преврати следующую реальную задачу пользователя в игровой квест.\n\n"
        f"Задача пользователя: {task}\n\n"
        "Дай название, короткую легенду, 3–6 конкретных шагов и награду. "
        "Используй фирменные эмодзи Квестификатора. Все действия должны быть реальными и безопасными."
    )

async def make_epic(quest_text: str) -> str:
    return await call_ai(
        "Переделай уже созданный квест в более эпичную и смешную RPG-версию. "
        "Сохрани реальные выполнимые действия, используй эмодзи и не делай ответ слишком длинным.\n\n"
        f"Исходный квест:\n{quest_text}"
    )

def fallback_quest(task: str, epic: bool = False) -> str:
    if epic:
        return (
            "🔥 ЭПИЧЕСКИЙ РЕЖИМ\n\n"
            f"⚔️ Легендарное испытание: {task}\n\n"
            "📜 Легенда:\nСудьба мира неожиданно зависит от вполне обычного дела. "
            "Герою предстоит победить Прокрастинацию и вернуть порядок.\n\n"
            "🎯 Испытания:\n1. Подготовить всё необходимое и начать.\n"
            "2. Выполнить основную часть без побочных миссий.\n"
            "3. Проверить результат и довести дело до состояния «готово».\n\n"
            "🏆 Награда:\n25 XP и право торжественно объявить победу."
        )
    return (
        f"⚔️ Квест: Операция «{task}»\n\n"
        "📜 Легенда:\nПеред героем появилась задача, которую пора превратить в короткую миссию.\n\n"
        "🎯 Испытания:\n1. Подготовиться и убрать всё, что мешает начать.\n"
        "2. Выполнить основную часть дела.\n3. Проверить результат и поставить финальную точку.\n\n"
        "🏆 Награда:\n25 XP и спокойствие героя."
    )


COMPLETE_MESSAGES = [
    "🎉 Квест закрыт. Бытовой мини-босс повержен.\n✨ Награда: +1 к легендарности.",
    "✅ Миссия выполнена. Можно торжественно выдохнуть.\n🏆 Награда: титул «Я всё-таки сделал это».",
    "⚔️ Победа засчитана. Рутина отступила на один шаг.\n✨ Награда: +1 к внутреннему спокойствию.",
    "🎉 Готово! Этот квест больше не висит над душой.\n🏆 Награда: право на довольное «ну вот».",
    "✅ Испытание завершено. Мир стал чуточку аккуратнее.\n✨ Награда: +1 к бытовой магии.",
    "🧙 Квест выполнен. Совет героев одобрительно кивает.\n🏆 Награда: Амулет Законченного Дела.",
    "⚔️ Мини-босс повержен без катсцены на сорок минут.\n✨ Награда: +1 к эффективности без фанатизма.",
    "🎊 Задание закрыто. Можно идти жить дальше.\n🏆 Награда: Чистая Совесть редкого качества.",
    "✅ Готово. Ещё одно обычное дело перестало быть проблемой.\n✨ Награда: +1 к ощущению контроля.",
    "🏁 Финиш! Дело сделано, драматическая музыка стихает.\n🏆 Награда: Кубок Маленькой Победы.",
    "🎉 Квест завершён. Прокрастинация сегодня недовольна.\n✨ Награда: +1 к решительности.",
    "✅ Миссия закрыта. Никакого продолжения во втором сезоне.\n🏆 Награда: право забыть об этом деле.",
]


async def mark_quest_as_upgraded(chat_id: int, message_id: int, text: str) -> None:
    """Помечает старую версию как заменённую и убирает её кнопки."""
    marked_text = text.rstrip() + "\n\n🔥 Эта версия усилена. Актуальный квест — ниже."
    try:
        await telegram_call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": marked_text[:4000],
                "reply_markup": {"inline_keyboard": []},
            },
        )
    except Exception:
        # Если Telegram не разрешил редактировать текст, хотя бы убираем кнопки.
        await remove_keyboard(chat_id, message_id)

async def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text == "/start":
        await send_message(chat_id,
            "🧙 Добро пожаловать в Квестификатор ⚔️\n\n"
            "Напиши обычное дело, а я превращу его в RPG-квест с помощью ИИ.\n\n"
            "Например:\n• разобрать шкаф\n• подготовиться к экзамену\n• закончить проект",
            start_keyboard())
        return

    if text in ("/help", "/quest"):
        await send_message(chat_id,
            "⚔️ Напиши любую реальную задачу.\n\n"
            "ИИ превратит её в квест. Потом можно сделать его эпичнее или отметить выполненным.")
        return

    if text == "/id":
        await send_message(chat_id, f"Твой Telegram ID: {user_id}")
        return

    if user_id not in ALLOWED_USER_IDS:
        await send_message(chat_id, DEMO_LOCK_MESSAGE)
        return

    if text == "/profile":
        await send_message(chat_id,
            "🧙 Онлайн-версия Квестификатора работает на Vercel.\n\n"
            "Постоянное хранение общего XP здесь пока отключено, чтобы проект оставался полностью бесплатным. "
            "Каждый выполненный квест всё равно приносит 25 XP.")
        return

    await send_chat_action(chat_id)
    try:
        answer = await generate_quest(text)
    except Exception:
        if user_id == TEACHER_ID:
            answer = fallback_quest(text)
        else:
            await send_message(chat_id, "Сейчас ИИ временно недоступен. Попробуй ещё раз чуть позже.")
            return

    await send_message(chat_id, answer, quest_keyboard())

async def handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    data = callback.get("data")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    user_id = callback.get("from", {}).get("id")

    await answer_callback(callback_id)
    if not chat_id:
        return

    if user_id not in ALLOWED_USER_IDS:
        await send_message(chat_id, DEMO_LOCK_MESSAGE)
        return

    if data == "new":
        await send_message(chat_id, "⚔️ Напиши новую задачу.")
        return

    if data == "complete":
        if message_id:
            await remove_keyboard(chat_id, message_id)
        await send_message(chat_id, random.choice(COMPLETE_MESSAGES))
        return

    if data == "epic":
        quest_text = (message.get("text") or "").strip()
        if not quest_text:
            await send_message(chat_id, "Не удалось прочитать исходный квест. Создай новый.")
            return

        # Старая версия больше не должна оставаться отдельным активным квестом.
        if message_id:
            await mark_quest_as_upgraded(chat_id, message_id, quest_text)

        await send_chat_action(chat_id)
        try:
            epic = await make_epic(quest_text)
        except Exception:
            if user_id == TEACHER_ID:
                epic = fallback_quest(quest_text[:120], epic=True)
            else:
                await send_message(chat_id, "Не получилось усилить квест. Попробуй ещё раз.")
                return

        await send_message(chat_id, epic, quest_keyboard())

@app.get("/api")
async def health():
    return {"ok": True, "service": "QuestMyLifeBot", "message": "Квестификатор работает"}

@app.get("/api/setup")
async def setup_webhook(request: Request):
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    webhook_url = f"{base_url}/api/webhook"
    result = await telegram_call("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return {
        "ok": True,
        "webhook": webhook_url,
        "telegram_description": result.get("description", "Webhook установлен"),
    }

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    incoming_secret = request.headers.get("x-telegram-bot-api-secret-token")
    if incoming_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    try:
        if "message" in update:
            await handle_message(update["message"])
        elif "callback_query" in update:
            await handle_callback(update["callback_query"])
    except Exception:
        pass
    return {"ok": True}
