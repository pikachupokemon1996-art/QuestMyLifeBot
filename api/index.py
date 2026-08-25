import asyncio
import hashlib
import os
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


def quest_is_complete(text: str) -> bool:
    required = ("⚔️", "📜", "🎯", "🏆")
    return all(marker in text for marker in required)


def quest_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔥 Эпичнее", "callback_data": "epic"}],
            [{"text": "✅ Готово", "callback_data": "complete"}],
            [{"text": "⚔️ Новое дело", "callback_data": "new"}],
        ]
    }


def start_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "⚔️ Превратить дело в квест", "callback_data": "new"}]
        ]
    }


async def telegram_call(method: str, payload: dict | None = None) -> dict:
    last_error = None

    for delay in (0, 2, 5):
        if delay:
            await asyncio.sleep(delay)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{TELEGRAM_API}/{method}",
                    json=payload or {},
                )
                response.raise_for_status()
                data = response.json()

            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")

            return data

        except Exception as exc:
            last_error = exc

    raise last_error


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": clean_plain_text(text)[:4000],
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    await telegram_call("sendMessage", payload)


async def send_chat_action(chat_id: int) -> None:
    try:
        await telegram_call(
            "sendChatAction",
            {"chat_id": chat_id, "action": "typing"},
        )
    except Exception:
        pass


async def answer_callback(callback_id: str) -> None:
    try:
        await telegram_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
        )
    except Exception:
        pass


async def remove_keyboard(chat_id: int, message_id: int) -> None:
    try:
        await telegram_call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
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
                    temperature=0.75,
                    max_output_tokens=1400,
                ),
            )

            if not response.text:
                raise RuntimeError("Gemini вернул пустой ответ")

            return clean_plain_text(response.text)

        except Exception as exc:
            last_error = exc
            message = str(exc).lower()

            if any(
                marker in message
                for marker in (
                    "401",
                    "403",
                    "api key",
                    "permission_denied",
                    "unauthenticated",
                )
            ):
                raise

    raise last_error


async def generate_quest(task: str) -> str:
    prompt = (
        "Преврати эту обычную задачу в маленький добрый RPG-квест:\n\n"
        f"{task}\n\n"
        "Ответ ОБЯЗАТЕЛЬНО должен содержать все четыре блока: "
        "⚔️ название, 📜 легенда, 🎯 план, 🏆 награда. "
        "Не заканчивай ответ, пока не написал награду. "
        "План — ровно 3 коротких шага. "
        "Весь ответ — примерно 8–12 коротких строк."
    )

    answer = await call_ai(prompt)

    if quest_is_complete(answer):
        return answer

    # Один дополнительный запрос, если модель оборвала ответ.
    answer = await call_ai(
        prompt
        + "\n\nПредыдущая попытка была неполной. "
          "Сейчас верни ЦЕЛЫЙ квест одним сообщением и обязательно закончи блоком 🏆 Награда."
    )

    return answer if quest_is_complete(answer) else fallback_quest(task)


async def make_epic(quest_text: str) -> str:
    prompt = (
        "Сделай этот квест смешнее и эпичнее, но не длиннее исходного. "
        "Не добавляй человеку новых реальных обязанностей. "
        "Меняй только художественную подачу.\n\n"
        "ОБЯЗАТЕЛЬНО сохрани четыре блока: "
        "⚔️ название, 📜 легенда, 🎯 план, 🏆 награда. "
        "План — ровно 3 коротких шага. "
        "Не заканчивай ответ до награды.\n\n"
        f"Исходный квест:\n{quest_text}"
    )

    answer = await call_ai(prompt)

    if quest_is_complete(answer):
        return answer

    answer = await call_ai(
        prompt
        + "\n\nВерни полный вариант заново одним сообщением. "
          "Обязательно закончи блоком 🏆 Награда."
    )

    return answer if quest_is_complete(answer) else quest_text


def fallback_quest(task: str) -> str:
    return (
        f"⚔️ Квест: «{task}»\n\n"
        "📜 Легенда:\n"
        "Обычное дело сегодня решило притвориться мини-боссом. "
        "Плохая новость: оно существует. Хорошая: победить его вполне реально.\n\n"
        "🎯 План:\n"
        "1. Подготовь всё нужное.\n"
        "2. Сделай основную часть дела.\n"
        "3. Оглянись на результат и закрой миссию.\n\n"
        "🏆 Награда:\n"
        "+1 к бытовой легендарности и заслуженное чувство «ну вот и всё» ✨"
    )


async def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if not text:
        return

    if text == "/start":
        await send_message(
            chat_id,
            (
                "🧙 Добро пожаловать в Квестификатор ⚔️\n\n"
                "Напиши любое обычное дело — я превращу его в маленький RPG-квест.\n\n"
                "Например:\n"
                "• разобрать шкаф\n"
                "• помыть посуду\n"
                "• сходить в магазин\n"
                "• заправить кровать"
            ),
            start_keyboard(),
        )
        return

    if text == "/help":
        await send_message(
            chat_id,
            (
                "⚔️ Просто напиши дело обычными словами.\n\n"
                "Я добавлю немного приключения, три коротких шага "
                "и смешную художественную награду."
            ),
        )
        return

    await send_chat_action(chat_id)

    try:
        answer = await generate_quest(text)
    except Exception:
        answer = fallback_quest(text)

    await send_message(chat_id, answer, quest_keyboard())


async def handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    data = callback.get("data")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    await answer_callback(callback_id)

    if not chat_id:
        return

    if data == "new":
        await send_message(chat_id, "⚔️ Напиши новое дело.")
        return

    if data == "complete":
        if message_id:
            await remove_keyboard(chat_id, message_id)

        await send_message(
            chat_id,
            "🎉 Квест закрыт. Мир переживёт этот день.\n✨ +1 к легендарности.",
        )
        return

    if data == "epic":
        quest_text = (message.get("text") or "").strip()

        if not quest_text:
            await send_message(chat_id, "Не вижу исходный квест. Создай новый.")
            return

        await send_chat_action(chat_id)

        try:
            epic = await make_epic(quest_text)
        except Exception:
            await send_message(
                chat_id,
                "🔥 Магия усиления сегодня капризничает. Сам квест всё равно действителен.",
            )
            return

        await send_message(chat_id, epic, quest_keyboard())


@app.get("/api")
async def health():
    return {
        "ok": True,
        "service": "QuestMyLifeBot",
        "message": "Квестификатор работает",
    }


@app.get("/api/setup")
async def setup_webhook(request: Request):
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    webhook_url = f"{base_url}/api/webhook"

    result = await telegram_call(
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        },
    )

    return {
        "ok": True,
        "webhook": webhook_url,
        "telegram_description": result.get(
            "description",
            "Webhook установлен",
        ),
    }


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    incoming_secret = request.headers.get(
        "x-telegram-bot-api-secret-token"
    )

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
