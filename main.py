# ==================================================
# CONFIG & ENV
# ==================================================

from dotenv import load_dotenv
from pathlib import Path
import os
from datetime import datetime
from openai import OpenAI
import re
import random
import json


ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
ROUTER_DEBUG = os.getenv("ROUTER_DEBUG", "0") == "1"
AI_ENABLED = os.getenv("AI_ENABLED", "0") == "1"
AI_DRY_RUN = os.getenv("AI_DRY_RUN", "0") == "1"
AI_TEST_NO_CACHE = os.getenv("AI_TEST_NO_CACHE", "0") == "1"
AI_TEST_MAX_CALLS_PER_USER = int(os.getenv("AI_TEST_MAX_CALLS_PER_USER", "1"))

AI_TEST_CALLS = {}  # user_id -> int

def ai_mode() -> str:
    # "off" - AI не вызываем
    # "dry_run" - AI вызываем, но пользователю не показываем результат (только лог)
    # "live" - AI вызываем и используем результат
    if not AI_ENABLED:
        return "off"
    if AI_DRY_RUN:
        return "dry_run"
    return "live"


print("DEBUG ENV")
print("BOT_TOKEN =", "SET" if BOT_TOKEN else None)
print("GOOGLE_SHEET_ID =", repr(GOOGLE_SHEET_ID))
print("GOOGLE_SERVICE_ACCOUNT_JSON =", "SET" if GOOGLE_SERVICE_ACCOUNT_JSON else None)
print("ROUTER_DEBUG =", ROUTER_DEBUG)
print("AI_ENABLED =", AI_ENABLED)
print("AI_DRY_RUN =", AI_DRY_RUN)
print("-" * 50)


# ==================================================
# TELEGRAM IMPORTS
# ==================================================

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

# ==================================================
# GOOGLE SHEETS CLIENT
# ==================================================

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

def get_sheets_client():
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None

    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    except Exception as e:
        print("Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON:", e)
        return None

    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()

SHEETS = get_sheets_client()

# ==================================================
# LOAD CONTEXTS (ROUTER KEYWORDS)
# ==================================================

def load_router_keywords():
    keywords = {}

    if not SHEETS:
        print("Sheets client not available, router disabled")
        return keywords

    try:
        result = SHEETS.values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="contexts!A:B",
        ).execute()

        rows = result.get("values", [])

        for row in rows[1:]:
            if len(row) < 2:
                continue

            project = row[0].strip().upper()
            keyword = row[1].strip().lower()

            if not project or not keyword:
                continue

            keywords.setdefault(project, []).append(keyword)

        print(f"Loaded router keywords: {keywords}")
        return keywords

    except Exception as e:
        print(f"Failed to load router keywords: {e}")
        return {}

# ==================================================
# LOAD RESPONSES
# ==================================================

def load_responses():
    responses = {}

    if not SHEETS:
        print("Sheets client not available, responses disabled")
        return responses

    try:
        result = SHEETS.values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="responses!A:B",
        ).execute()

        rows = result.get("values", [])

        for row in rows[1:]:
            if len(row) < 2:
                continue

            key = row[0].strip()
            text = row[1]

            if not key or not text:
                continue

            responses.setdefault(key, []).append(text)

        print(f"Loaded responses: {list(responses.keys())}")
        return responses

    except Exception as e:
        print(f"Failed to load responses: {e}")
        return {}

# ==================================================
# USER LOGGING
# ==================================================

def log_user(update):
    if not SHEETS:
        return

    user = update.effective_user
    if not user:
        return

    telegram_id = str(user.id)
    first_name = user.first_name or ""
    username = user.username or ""
    now = datetime.utcnow().isoformat(timespec="seconds")

    try:
        result = SHEETS.values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="users!A:A",
        ).execute()

        rows = result.get("values", [])
        ids = [row[0] for row in rows[1:] if row]

        if telegram_id in ids:
            row_index = ids.index(telegram_id) + 2
            SHEETS.values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range=f"users!E{row_index}",
                valueInputOption="RAW",
                body={"values": [[now]]},
            ).execute()
        else:
            SHEETS.values().append(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="users!A:E",
                valueInputOption="RAW",
                body={
                    "values": [[
                        telegram_id,
                        first_name,
                        username,
                        now,
                        now
                    ]]
                },
            ).execute()

    except Exception as e:
        print(f"User log failed: {e}")


# ==================================================
# MESSAGE LOGGING
# ==================================================

def log_message(update, project: str):
    if not SHEETS:
        return

    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    telegram_id = str(user.id)
    username = user.username or ""
    text = message.text or ""
    timestamp = datetime.utcnow().isoformat(timespec="seconds")

    try:
        SHEETS.values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="messages!A:E",
            valueInputOption="RAW",
            body={
                "values": [[
                    timestamp,
                    telegram_id,
                    username,
                    text,
                    project
                ]]
            },
        ).execute()

    except Exception as e:
        print(f"Message log failed: {e}")

# ==================================================
# UNKNOWN CACHE (ANTI-AI SPAM)
# ==================================================

UNKNOWN_CACHE = set()

# ==================================================
# INIT DATA
# ==================================================

ROUTER_KEYWORDS = load_router_keywords()
RESPONSES = load_responses()

# ==================================================
# PRE_INTENTS
# ==================================================

def is_exam_question(text: str) -> bool:
    return any(p in text for p in [
        "экзамен",
        "сдать экзамен",
        "как сдать",
        "как проходит экзамен",
        "экзамен пдд"
    ])

def is_how_it_works(text: str) -> bool:
    return any(p in text for p in [
        "как это работает",
        "как работает",
        "как устроено",
        "как проходит обучение"
    ])

def is_choose_questions(text: str) -> bool:
    return any(p in text for p in [
        "можно выбрать",
        "выбирать вопросы",
        "самому выбирать",
        "выбор вопросов"
    ])

def is_general_help(text: str) -> bool:
    return (
        len(text) > 50
        and any(p in text for p in [
            "подскаж",
            "расскаж",
            "помог",
            "объясн"
        ])
    )

def is_greeting(text: str) -> bool:
    return text in [
        "привет", "здравствуйте", "добрый день", "hello", "hi"
    ]


def is_what_is(text: str) -> bool:
    return text in [
        "что это", "что это такое", "что за бот", "что за сервис"
    ]


def is_how_start(text: str) -> bool:
    return any(p in text for p in [
        "как начать", "с чего начать", "как пользоваться", "что делать сначала"
    ])


def is_where_study(text: str) -> bool:
    return any(p in text for p in [
        "где учиться", "где обучение", "где тренажер", "где экзамен"
    ])


def is_commands_problem(text: str) -> bool:
    return any(p in text for p in [
        "не работает", "команда", "/start", "/learn", "/exam"
    ])


def is_free_question(text: str) -> bool:
    return "бесплат" in text


def is_price_question(text: str) -> bool:
    return any(p in text for p in [
        "цена", "стоимость", "платно", "подписка", "сколько стоит"
    ])


def is_language_question(text: str) -> bool:
    return any(p in text for p in [
        "язык", "русск", "корейск", "английск"
    ])


def is_dont_understand(text: str) -> bool:
    return any(p in text for p in [
        "не понял", "не понимаю", "ничего не понятно", "запутался"
    ])


# ==================================================
# ROUTER
# ==================================================

def score_projects(text: str):
    scores = {}
    matches = {}

    if not text or not ROUTER_KEYWORDS:
        return scores, matches

    text_l = text.lower()

    for project, keywords in ROUTER_KEYWORDS.items():
        score = 0
        hit = []

        for kw in keywords:
            if kw and kw in text_l:
                score += 1
                hit.append(kw)

        scores[project] = score
        matches[project] = hit

    return scores, matches

def detect_project(text: str) -> str:
    scores, _ = score_projects(text)

    if not scores:
        return "UNKNOWN"

    sorted_projects = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_project, best_score = sorted_projects[0]

    if best_score < 2:
        return "UNKNOWN"

    if len(sorted_projects) > 1:
        second_score = sorted_projects[1][1]
        if best_score - second_score < 1:
            return "UNKNOWN"

    return best_project

# ==================================================
# RESPONSE RESOLVER
# ==================================================

def get_response(key: str, fallback: str = "…") -> str:
    variants = RESPONSES.get(key)
    if not variants:
        print(f"[WARN] Missing response for key: {key}")
        return fallback
    return random.choice(variants)

# ==================================================
# TEXT FILTERS (BEFORE AI)
# ==================================================

def is_garbage(text: str) -> bool:
    text = text.strip().lower()

    if len(text) < 4:
        return True

    if text.isalpha() and len(set(text)) <= 3:
        return True

    return False


# ==================================================
# INTENTS (FAQ BEFORE ROUTER/AI)
# ==================================================

def normalize_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.replace("ё", "е")
    t = re.sub(r"[^\w\s/]", "", t)  # сохраняем / для команд типа /start
    t = re.sub(r"\s+", " ", t)
    return t

def cache_key_soft(raw_text: str) -> str:
    t = (raw_text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t

# ВАЖНО:
# ключи тут это keys из responses!A:B
# то есть ты НЕ пишешь код под каждый key
# ты просто добавляешь новые ключи в responses и паттерны тут
INTENT_PATTERNS = [
    # приветствия
    
    ("CAN_CHOOSE_QUESTIONS", [
        "можно выбрать вопросы", "можно выбирать вопросы", "самому выбирать вопросы",
        "выбор вопросов", "можно по номеру", "перейти по номеру", "как перейти к вопросу",
        "goto", "/goto",
    ]),

    ("COMMANDS_IN_TRAINER_ONLY", [
        "/start", "/learn", "/drill", "/exam", "/goto", "команда", "не работает команда",
        "не работает /start", "не работает /learn", "не работает /exam", "не работает /drill",
        "где /learn", "как включить /exam", "start", "learn", "drill", "exam", "goto",
    ]),

    ("GREETING", [
        "привет", "здравствуйте", "добрый день", "добрый вечер", "доброе утро",
        "хай", "hello", "hi", "yo", "прив",
    ]),

    # что это / что за бот
    ("WHAT_IS_PDD", [
        "что это", "что за бот", "что ты такое", "что это такое", "что вы такое",
        "что за сервис", "что за тренажер", "что за тренажёр", "расскажи", "расскажи пожалуйста", "объясни",
    ]),

    # что внутри / какие режимы
    ("WHAT_INSIDE", [
        "что внутри", "что есть", "что умеет", "какие функции", "какие режимы",
        "что доступно", "что входит",
    ]),

    # как начать
    ("HOW_START", [
    "как начать",
    "с чего начать",
    "как пользоваться",
    "как пользоваться ботом",
    "как учиться",
    "как готовиться",
    "куда заходить",
    "куда идти",
    "где начинать",
    ]),

    # как учить
    ("HOW_TO_LEARN", [
        "как правильно учить", "как лучше учить", "как учить", "как запоминать",
        "как готовиться к экзамену", "как выучить", "как быстрее выучить", 
        "как учить", "как учиться", "где учить",
        "где учиться", "где обучение", "где учиться"
    ]),

    # бесплатное
    ("FREE_AVAILABLE", [
        "бесплатно", "что бесплатно", "есть бесплатно", "сколько бесплатных",
        "бесплатные вопросы", "фри", "free",
    ]),

    # интенсив / drill
    ("WHAT_IS_DRILL", [
        "drill", "интенсив", "тренировка", "что такое интенсив", "что такое drill",
    ]),

    # экзамен
    ("WHAT_IS_EXAM", [
        "exam", "экзамен", "пробный экзамен", "тест", "что такое экзамен", "что такое exam",
    ]),

    # как проходит экзамен
    ("HOW_EXAM_WORKS", [
        "как проходит экзамен", "как сдавать", "как сдавать экзамен",
        "сколько вопросов", "сколько в экзамене", "сколько вопросов в exam",
        "сколько нужно набрать", "сколько баллов", "проходной", "проходной балл",
    ]),

    # язык экзамена
    ("LANGUAGE_QUESTION", [
        "на каком языке", "язык", "корейский", "английский", "русский",
        "можно на русском", "экзамен на русском",
    ]),

    # цена/оплата
    ("PRICE_INFO", [
    "цена", "стоимость", "сколько стоит", "платно", "это платно",
    "сколько стоит доступ", "почем",
    "прайс", "тариф",
    "подписка", "сколько подписка",
    "платно", "это платно", "платный",
    ]),
    
    ("PAYMENT_INFO", [
        "оплата", "как оплатить", "как купить", "как оплатить доступ",
        "как купить подписку", "как получить доступ", "как платить", "почем", "как оформить?"
    ]),

    # связь
    ("CONTACT_DEV", [
        "контакт", "связаться", "поддержка", "разработчик", "админ",
        "куда писать", "как написать", "@", "телеграм",
    ]),
]


def detect_intent(text: str) -> str | None:
    t = normalize_text(text)
    if not t:
        return None

    # 👇 КРИТИЧНО: одиночные сообщения
    for key, patterns in INTENT_PATTERNS:
        if t in patterns:
            return key

    for key, patterns in INTENT_PATTERNS:
        for p in patterns:
            p = normalize_text(p)
            if not p:
                continue

            # точное или частичное совпадение по корню
            if p in t or (len(p) >= 4 and p[:-1] in t):
                return key

    return None



# ==================================================
# AI FALLBACK
# IMPORTANT:
# AI is used ONLY for UNKNOWN cases
# AFTER all filters and cache checks
# ==================================================

def ai_detect_intent(text: str) -> str | None:
    if not AI_ENABLED:
        return None

    # DRY RUN: проверяем, что дошли до ИИ
    if AI_DRY_RUN:
        print("AI DRY RUN")
        print("AI would be called with text:")
        print(repr(text))
        print("-" * 50)
        return "__DRY_RUN__"

    # Реальный вызов ИИ (ТОЛЬКО если дойдем сюда)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("AI enabled, but OPENAI_API_KEY is missing")
        return None

    try:
        client = OpenAI(api_key=api_key)

        available_keys = [
            "GREETING",
            "WHAT_IS_PDD",
            "WHAT_INSIDE",
            "HOW_START",
            "HOW_TO_LEARN",
            "FREE_AVAILABLE",
            "WHAT_IS_DRILL",
            "WHAT_IS_EXAM",
            "HOW_EXAM_WORKS",
            "LANGUAGE_QUESTION",
            "PRICE_INFO",
            "PAYMENT_INFO",
            "CONTACT_DEV",
            "COMMANDS_IN_TRAINER_ONLY",
            "CAN_CHOOSE_QUESTIONS",
            "UNKNOWN",
        ]

        prompt = (
            "Ты классификатор интентов для поддержки тренажера ПДД.\n"
            "Твоя задача: выбрать один ключ из списка.\n"
            "Если сообщение не про ПДД или тренажер, верни not_pdd.\n\n"
            f"Ключи: {', '.join(available_keys)}\n\n"
            f"Сообщение пользователя: \"{text}\"\n\n"
            "Ответь строго одним словом: ключ или not_pdd."
        )

        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )

        answer = (resp.output_text or "").strip()

        if not answer:
            return None

        if answer.lower().startswith("not_pdd"):
            return None

        return answer

    except Exception as e:
        print(f"AI error: {e}")
        return None


# ==================================================
# AGENTS
# ==================================================

async def pdd_agent(update, context):
    await update.message.reply_text(
        get_response("PDD_ACK", "ПДД: вопрос принят.")
    )

def looks_like_question(text: str) -> bool:
    return any(k in text for k in ["как", "что", "где", "когда", "почему", "можно"])

async def unknown_agent(update, context, raw_text: str):
    user = update.effective_user
    if not user:
        return

    mode = ai_mode()
    text_norm = normalize_text(raw_text)

    # 1) мусор - сразу fallback, без AI
    if is_garbage(text_norm):
        await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
        return

    # 2) не похоже на вопрос - тоже без AI
    if not looks_like_question(text_norm):
        await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
        return

    # 3) кеш: в тест-режиме можно полностью игнорировать
    key = (user.id, cache_key_soft(raw_text))

    if not AI_TEST_NO_CACHE:
        if key in UNKNOWN_CACHE:
            if ROUTER_DEBUG:
                print("UNKNOWN CACHE HIT:", key)
            await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
            return

    # добавляем в кеш один раз, только после прохождения фильтров
    UNKNOWN_CACHE.add(key)

    # 4) если AI выключен, сразу fallback
    if mode == "off":
        await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
        return

    # 5) тестовый лимит вызовов AI на юзера (защита баланса)
    if AI_TEST_MAX_CALLS_PER_USER > 0:
        calls = AI_TEST_CALLS.get(user.id, 0)
        if calls >= AI_TEST_MAX_CALLS_PER_USER:
            await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
            return

    # 6) вызываем AI ровно один раз
    if len(raw_text.strip()) <= 10:
        await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
        return

    AI_TEST_CALLS[user.id] = AI_TEST_CALLS.get(user.id, 0) + 1

    ai_key = ai_detect_intent(raw_text)

    # логируем факт вызова
    if ROUTER_DEBUG:
        print("AI CALLED:", {"mode": mode, "user": user.id, "text": raw_text, "ai_key": ai_key})

    # 7) DRY RUN: AI вызвали, но пользователю не показываем результат
    if mode == "dry_run":
        log_message(update, "AI_DRY_RUN")
        await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))
        return

    # 8) live: если AI вернул ключ из RESPONSES, отвечаем по нему
    if ai_key and ai_key in RESPONSES:
        await update.message.reply_text(get_response(ai_key, get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста.")))
        log_message(update, f"AI_INTENT:{ai_key}")
        return

    # 9) иначе fallback
    await update.message.reply_text(get_response("UNKNOWN", "Я не до конца понял вопрос. Уточните, пожалуйста."))


# ==================================================
# DISPATCHER
# ==================================================

async def on_message(update, context):
    log_user(update)

    # исходный текст пользователя (ВАЖНО для AI)
    raw_text = update.message.text or ""
    # нормализованный текст (для интентов и роутера)
    text = normalize_text(raw_text)

    # ==================================================
    # 1) FAQ / INTENTS (раньше роутера и AI)
    # ==================================================
    intent_key = detect_intent(raw_text)
    if intent_key:
        reply_text = get_response(intent_key, "")
        if not reply_text or not reply_text.strip():
            reply_text = get_response(
                "UNKNOWN",
                "Я не до конца понял вопрос. Уточните, пожалуйста."
            )

        await update.message.reply_text(reply_text)
        log_message(update, f"INTENT:{intent_key}")
        return

    # ==================================================
    # 2) PROJECT ROUTER (PDD / UNKNOWN)
    # ==================================================
    scores, matches = score_projects(text)
    project = detect_project(text)

    log_message(update, project)

    if ROUTER_DEBUG:
        print("ROUTER DEBUG")
        print("raw_text:", repr(raw_text))
        print("normalized:", repr(text))
        print("scores:", scores)
        print("matches:", matches)
        print("chosen:", project)
        print("-" * 50)

    # ==================================================
    # 3) AGENTS
    # ==================================================
    if project == "PDD":
        await pdd_agent(update, context)
        return

    # UNKNOWN — последний шанс (внутри: фильтры + AI)
    if project == "UNKNOWN":
        await unknown_agent(update, context, raw_text)
        return

# ==================================================
# COMMANDS
# ==================================================

async def start(update, context):
    log_user(update)
    await update.message.reply_text(
        get_response("GREETING", "Привет.")
    )

# ==================================================
# ENTRY POINT
# ==================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not found in .env")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
